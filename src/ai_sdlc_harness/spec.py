"""Implementation of ``ai-sdlc spec``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import REQUIREMENTS_FILENAME, SPEC_FILENAME, TASK_ARTIFACT_FILENAMES, TASK_SLUG_PATTERN
from .files import read_text, resolve_under_root, write_text
from .manifest import load_manifest, sha256_file, write_manifest
from .redact import redact_text
from .requirements import build_requirements_document, render_requirements_yaml, requirements_path
from .task_metadata import extract_task_title


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FINDING_LINE_RE = re.compile(r"^\s*-?\s*(blocker|warning|info):\s+(.+?)\s*$", re.IGNORECASE)
MAX_SECTION_CHARS = 1200
MAX_SECTION_LINES = 40

DISCLAIMER = (
    "This spec is a deterministic Markdown consolidation of task intent. It does not ask interactive questions, "
    "call AI models, generate requirements automatically, generate code, generate tests, run tests, inspect target "
    "code deeply, scan security, validate compliance, enforce packs, execute packs, prove correctness, prove "
    "security, determine approval, or determine release-readiness."
)


@dataclass(frozen=True)
class Finding:
    level: str
    message: str


@dataclass(frozen=True)
class SourceArtifact:
    filename: str
    present: bool
    readable: bool
    text: str
    message: str


@dataclass(frozen=True)
class ExtractedSection:
    filename: str
    heading: str
    text: str
    state: str


@dataclass(frozen=True)
class OptionalReport:
    filename: str
    present: bool
    readable: bool
    text: str
    message: str


@dataclass(frozen=True)
class GeneratedArtifact:
    path: PurePosixPath
    target: Path
    entry: dict[str, Any] | None
    label: str
    content: str


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


def _artifact_path(slug: str, filename: str) -> PurePosixPath:
    return _task_dir_path(slug) / filename


def _spec_path(slug: str) -> PurePosixPath:
    return _artifact_path(slug, SPEC_FILENAME)


def _requirements_path(slug: str) -> PurePosixPath:
    return requirements_path(slug)


def _validate_task_slug(slug: str) -> None:
    if not slug or not slug.strip():
        raise ValueError("Task slug must not be empty.")
    if "\x00" in slug:
        raise ValueError("Task slug contains an unsafe NUL character.")
    if TASK_SLUG_RE.fullmatch(slug) is None:
        raise ValueError("Task slug is unsafe. Use the safe slug created by `ai-sdlc task start`.")


def _load_manifest_entries(root: Path) -> dict[str, dict[str, Any]]:
    manifest_path = resolve_under_root(root, ".harness/manifest.json")
    manifest = load_manifest(manifest_path)
    entries = manifest.get("managed_files")
    if not isinstance(entries, list):
        raise ValueError("manifest managed_files must be a list")
    return {str(entry.get("path")): entry for entry in entries if isinstance(entry, dict)}


def _read_source_artifact(root: Path, slug: str, filename: str) -> SourceArtifact:
    relative = _artifact_path(slug, filename)
    target = resolve_under_root(root, relative)
    if not target.exists():
        return SourceArtifact(filename, present=False, readable=False, text="", message="missing")
    if not target.is_file():
        return SourceArtifact(filename, present=True, readable=False, text="", message="not a regular file")
    try:
        return SourceArtifact(filename, present=True, readable=True, text=read_text(target), message="present")
    except Exception as exc:
        return SourceArtifact(filename, present=True, readable=False, text="", message=f"unreadable: {exc}")


def _read_source_artifacts(root: Path, slug: str) -> list[SourceArtifact]:
    return [_read_source_artifact(root, slug, filename) for filename in TASK_ARTIFACT_FILENAMES]


def _read_preflight(root: Path, slug: str) -> OptionalReport:
    relative = _artifact_path(slug, "preflight.md")
    target = resolve_under_root(root, relative)
    if not target.exists():
        return OptionalReport("preflight.md", present=False, readable=False, text="", message="missing")
    if not target.is_file():
        return OptionalReport("preflight.md", present=True, readable=False, text="", message="not a regular file")
    try:
        return OptionalReport("preflight.md", present=True, readable=True, text=read_text(target), message="present")
    except Exception as exc:
        return OptionalReport("preflight.md", present=True, readable=False, text="", message=f"unreadable: {exc}")


def _normalize_heading(heading: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", heading.lower()).strip()
    return re.sub(r"\s+", " ", normalized)


def _section_text(text: str, headings: tuple[str, ...]) -> tuple[str, str] | None:
    wanted = {_normalize_heading(heading): heading for heading in headings}
    lines = text.splitlines()
    start_index: int | None = None
    selected_heading = ""
    selected_level = 0
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line.strip())
        if not match:
            continue
        heading = _normalize_heading(match.group(2))
        if heading not in wanted:
            continue
        start_index = index + 1
        selected_heading = wanted[heading]
        selected_level = len(match.group(1))
        break

    if start_index is None:
        return None

    end_index = len(lines)
    for index in range(start_index, len(lines)):
        match = HEADING_RE.match(lines[index].strip())
        if match and len(match.group(1)) <= selected_level:
            end_index = index
            break
    return selected_heading, "\n".join(lines[start_index:end_index]).strip()


def _line_is_placeholder(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        return True
    if stripped in {"-", "*", "[]"}:
        return True
    if stripped.startswith(("- [ ]", "* [ ]")):
        return True
    if TODO_RE.search(stripped):
        return True
    return False


def _section_state(text: str) -> str:
    if not text.strip():
        return "empty"
    if not any(not _line_is_placeholder(line) for line in text.splitlines()):
        return "TODO-only"
    return "ready"


def _extract_section(
    artifacts: dict[str, SourceArtifact],
    filename: str,
    heading: str,
    aliases: tuple[str, ...] = (),
) -> ExtractedSection:
    artifact = artifacts[filename]
    if not artifact.present:
        return ExtractedSection(filename, heading, "", "missing source")
    if not artifact.readable:
        return ExtractedSection(filename, heading, "", "unreadable source")
    found = _section_text(artifact.text, (heading, *aliases))
    if found is None:
        return ExtractedSection(filename, heading, "", "missing")
    selected_heading, text = found
    return ExtractedSection(filename, selected_heading, text, _section_state(text))


def _bounded_excerpt(text: str) -> str:
    redacted = redact_text(text)
    lines = redacted.strip().splitlines()
    truncated = False
    if len(lines) > MAX_SECTION_LINES:
        lines = lines[:MAX_SECTION_LINES]
        truncated = True
    excerpt = "\n".join(lines).strip()
    if len(excerpt) > MAX_SECTION_CHARS:
        excerpt = excerpt[:MAX_SECTION_CHARS].rstrip()
        truncated = True
    if truncated:
        excerpt = (excerpt + "\n\n" if excerpt else "") + "[Excerpt truncated; see the source artifact for full content.]"
    return excerpt or "[No excerptable content.]"


def _render_section(section: ExtractedSection) -> list[str]:
    lines = [f"Source: `{section.filename}`", ""]
    if section.state in {"missing source", "unreadable source"}:
        lines.append(f"- warning: source artifact `{section.filename}` is {section.state.replace(' source', '')}.")
        return lines
    if section.state == "missing":
        lines.append(f"- warning: source section `{section.heading}` is missing from `{section.filename}`.")
        return lines
    if section.state == "TODO-only":
        lines.append(f"- warning: source section `{section.heading}` in `{section.filename}` is TODO-only.")
    else:
        lines.append("- Status: ready.")
    lines.extend(["", "```text", _bounded_excerpt(section.text), "```"])
    return lines


def _non_placeholder_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if not _line_is_placeholder(line)]
    return "\n".join(lines).strip()


def _has_open_questions(sections: list[ExtractedSection]) -> bool:
    for section in sections:
        if section.state not in {"ready", "TODO-only"}:
            continue
        lowered = _non_placeholder_text(section.text).lower()
        if not lowered:
            continue
        if any(marker in lowered for marker in ("no open question", "no unresolved question", "none")) and "?" not in lowered:
            continue
        if any(marker in lowered for marker in ("?", "unknown", "unresolved", "open question", "to confirm", "needs confirmation")):
            return True
    return False


def _preflight_findings(preflight: OptionalReport) -> list[Finding]:
    if not preflight.present:
        return [Finding("warning", "preflight.md is missing.")]
    if not preflight.readable:
        return [Finding("warning", f"preflight.md is unreadable: {preflight.message}.")]

    findings: list[Finding] = []
    for line in preflight.text.splitlines():
        match = FINDING_LINE_RE.match(line)
        if not match:
            continue
        findings.append(Finding(match.group(1).lower(), f"preflight.md reported {match.group(1).lower()}: {match.group(2).strip()}"))
    if not findings:
        findings.append(Finding("info", "preflight.md has no blocker, warning, or info findings."))
    return findings


def _source_artifact_findings(artifacts: list[SourceArtifact]) -> list[Finding]:
    findings: list[Finding] = []
    for artifact in artifacts:
        if not artifact.present:
            findings.append(Finding("blocker", f"{artifact.filename} is missing."))
        elif not artifact.readable:
            findings.append(Finding("blocker", f"{artifact.filename} is unreadable."))
    return findings


def _readiness_findings(
    *,
    implementation_boundary: ExtractedSection,
    requirements: ExtractedSection,
    protected_behavior: ExtractedSection,
    architecture_sections: list[ExtractedSection],
    coupling_sections: list[ExtractedSection],
    security: ExtractedSection,
    test_sections: list[ExtractedSection],
    verification_sections: list[ExtractedSection],
    evidence_sections: list[ExtractedSection],
    open_question_sections: list[ExtractedSection],
) -> list[Finding]:
    findings: list[Finding] = []
    if implementation_boundary.state != "ready":
        findings.append(Finding("blocker", f"implementation boundary is {implementation_boundary.state}."))
    if requirements.state != "ready":
        findings.append(Finding("blocker", f"requirements / acceptance criteria are {requirements.state}."))
    if protected_behavior.state != "ready":
        findings.append(Finding("warning", f"protected behavior / non-goals are {protected_behavior.state}."))
    if not any(section.state == "ready" for section in architecture_sections):
        findings.append(Finding("warning", "architecture / interface impact is missing or TODO-only."))
    if not any(section.state == "ready" for section in coupling_sections):
        findings.append(Finding("warning", "coupling / maintainability notes are missing or TODO-only."))
    if security.state != "ready":
        findings.append(Finding("warning", f"security / privacy risk-surface notes are {security.state}."))
    if not any(section.state == "ready" for section in test_sections):
        findings.append(Finding("warning", "test expectations are missing or TODO-only."))
    if not any(section.state == "ready" for section in verification_sections):
        findings.append(Finding("warning", "verification expectations are missing or TODO-only."))
    if not any(section.state == "ready" for section in evidence_sections):
        findings.append(Finding("warning", "evidence expectations are missing or TODO-only."))
    if _has_open_questions(open_question_sections):
        findings.append(Finding("warning", "open questions are present."))
    return findings


def _summary(findings: list[Finding]) -> str:
    blockers = sum(1 for finding in findings if finding.level == "blocker")
    warnings = sum(1 for finding in findings if finding.level == "warning")
    infos = sum(1 for finding in findings if finding.level == "info")
    return f"summary: {blockers} blocker(s), {warnings} warning(s), {infos} info"


def _requirements_summary(requirements_document: dict[str, Any]) -> tuple[int, int]:
    requirements = requirements_document.get("requirements")
    findings = requirements_document.get("findings")
    return (
        len(requirements) if isinstance(requirements, list) else 0,
        len(findings) if isinstance(findings, list) else 0,
    )


def _answer(findings: list[Finding]) -> tuple[str, str]:
    if any(finding.level == "blocker" for finding in findings):
        return "No", "blocker findings must be resolved or explicitly accepted before implementation."
    if any(finding.level == "warning" for finding in findings):
        return "Caution", "no blockers were found, but warning findings remain for human review."
    return "Yes", "implementation intent appears clear enough based on recorded task artifacts."


def _recommended_actions(findings: list[Finding]) -> list[str]:
    messages = [finding.message for finding in findings if finding.level in {"blocker", "warning"}]
    actions: list[str] = []
    if any("implementation boundary" in message for message in messages):
        actions.append("Fill in the implementation boundary and expected change area before generating a workset.")
    if any("requirements / acceptance" in message for message in messages):
        actions.append("Add concrete requirements and observable acceptance criteria.")
    if any("protected behavior" in message for message in messages):
        actions.append("Record protected behavior and non-goals, or state that none apply.")
    if any("architecture" in message or "interface" in message for message in messages):
        actions.append("Record interface impact, compatibility expectations, and architecture implications.")
    if any("coupling" in message or "maintainability" in message for message in messages):
        actions.append("Record coupling and maintainability concerns, or state that none apply.")
    if any("security / privacy" in message for message in messages):
        actions.append("Record security/privacy risk-surface notes, or state that no sensitive surface was identified.")
    if any("test expectations" in message or "verification expectations" in message for message in messages):
        actions.append("Record expected tests, checks, and verification commands.")
    if any("evidence expectations" in message for message in messages):
        actions.append("Record what evidence should be captured after implementation.")
    if any("open questions" in message or "preflight.md reported blocker" in message for message in messages):
        actions.append("Resolve or explicitly carry forward open questions before implementation.")
    if not actions:
        actions.append("Review this spec and generate the agent workset when the task intent is accepted.")
    return actions[:8]


def _render_group(title: str, sections: list[ExtractedSection]) -> list[str]:
    lines = [f"## {title}", ""]
    for index, section in enumerate(sections):
        if len(sections) > 1:
            lines.extend([f"### {section.heading}", ""])
        lines.extend(_render_section(section))
        if index != len(sections) - 1:
            lines.append("")
    lines.append("")
    return lines


def _render_preflight_signals(preflight: OptionalReport, findings: list[Finding]) -> list[str]:
    lines = ["## Preflight Signals", ""]
    if preflight.present and preflight.readable:
        lines.extend(["Source: `preflight.md`", "", "- Status: present and readable.", "", "Findings:"])
    elif preflight.present:
        lines.extend(["Source: `preflight.md`", "", f"- warning: preflight.md is unreadable: {preflight.message}.", "", "Findings:"])
    else:
        lines.extend(["Source: `preflight.md`", "", "- warning: preflight.md is missing.", "", "Findings:"])
    for finding in findings:
        lines.append(f"- {finding.level}: {finding.message}")
    lines.append("")
    return lines


def _render_structured_requirements(slug: str, requirements_document: dict[str, Any]) -> list[str]:
    requirement_count, finding_count = _requirements_summary(requirements_document)
    path_text = _requirements_path(slug).as_posix()
    return [
        "## Structured Requirements",
        "",
        f"- Generated artifact: `{path_text}`",
        "- Model: advisory structured projection from `acceptance.md`.",
        "- Artifact role: advisory projection.",
        "- Authority: non-authoritative.",
        f"- Requirement count: {requirement_count}",
        f"- Finding count: {finding_count}",
        "- Source of truth: user-authored task artifacts and this generated `spec.md`; `requirements.yaml` is not authoritative.",
        "- To change requirements, edit source task artifacts, especially `acceptance.md`, then rerun `ai-sdlc spec --task <slug>`.",
        "",
    ]


def _render_report(
    *,
    slug: str,
    generated_at: str,
    artifacts: list[SourceArtifact],
    preflight: OptionalReport,
    implementation_boundary: ExtractedSection,
    assumptions_open_questions: ExtractedSection,
    requirements: ExtractedSection,
    protected_behavior: ExtractedSection,
    architecture_sections: list[ExtractedSection],
    coupling_sections: list[ExtractedSection],
    security: ExtractedSection,
    test_sections: list[ExtractedSection],
    verification_sections: list[ExtractedSection],
    evidence_sections: list[ExtractedSection],
    findings: list[Finding],
    preflight_findings: list[Finding],
    requirements_document: dict[str, Any],
) -> str:
    counts = {
        "blocker": sum(1 for finding in findings if finding.level == "blocker"),
        "warning": sum(1 for finding in findings if finding.level == "warning"),
        "info": sum(1 for finding in findings if finding.level == "info"),
    }
    by_name = {artifact.filename: artifact for artifact in artifacts}
    answer, reason = _answer(findings)
    lines = [
        "# Specification",
        "",
        f"Task slug: `{slug}`",
        f"Generated: {generated_at}",
        "",
        "## Summary",
        "",
        "- Spec question: Is the implementation intent clear enough to generate a useful workset and guide a coding agent?",
        f"- Implementation-intent answer: {answer}",
        f"- Reason: {reason}",
        f"- Blockers: {counts['blocker']}",
        f"- Warnings: {counts['warning']}",
        f"- Info: {counts['info']}",
        "",
        "## Implementation Intent",
        "",
        f"- Task title: {extract_task_title(by_name['task.md'].text if by_name['task.md'].readable else '', slug)}",
        f"- Task slug: `{slug}`",
        "- Source model: deterministic Markdown consolidation from existing task artifacts and preflight findings.",
        "- Output model: generated Markdown spec plus advisory structured requirements YAML.",
        "",
    ]

    lines.extend(_render_structured_requirements(slug, requirements_document))
    lines.extend(_render_group("Task Boundary", [implementation_boundary]))
    lines.extend(_render_group("Requirements / Acceptance Criteria", [requirements]))
    lines.extend(_render_group("Protected Behavior / Non-Goals", [protected_behavior]))
    lines.extend(_render_group("Architecture / Interface Impact", architecture_sections))
    lines.extend(_render_group("Coupling / Maintainability Notes", coupling_sections))
    lines.extend(_render_group("Security / Privacy Risk-Surface Notes", [security]))
    lines.extend(_render_group("Test Expectations", test_sections))
    lines.extend(_render_group("Verification Expectations", verification_sections))
    lines.extend(_render_group("Evidence Expectations", evidence_sections))
    lines.extend(_render_group("Open Questions", [assumptions_open_questions, architecture_sections[-1]]))
    lines.extend(_render_preflight_signals(preflight, preflight_findings))

    lines.extend(["## Findings", ""])
    for finding in findings:
        lines.append(f"- {finding.level}: {finding.message}")

    lines.extend(["", "## Recommended Next Actions", ""])
    for action in _recommended_actions(findings):
        lines.append(f"- {action}")

    lines.extend(["", "## Disclaimer", "", DISCLAIMER, ""])
    return "\n".join(lines)


def _artifact_blocking_messages(artifact: GeneratedArtifact, *, force: bool) -> list[str]:
    path_text = artifact.path.as_posix()
    if artifact.target.exists() and artifact.entry is None:
        return [
            f"unmanaged existing file {path_text}",
            f"refusing to overwrite unmanaged {artifact.label}",
        ]

    if artifact.target.exists() and artifact.entry is not None:
        expected_hash = artifact.entry.get("sha256")
        hash_algorithm = artifact.entry.get("hash_algorithm")
        hash_clean = hash_algorithm == "sha256" and expected_hash and sha256_file(artifact.target) == expected_hash
        if not hash_clean and not force:
            return [
                f"hash drift detected for {path_text}",
                f"refusing to overwrite manifest-managed {artifact.label} without --force",
            ]
    return []


def run_spec(root: Path, task_slug: str, *, dry_run: bool = False, force: bool = False) -> tuple[int, list[str]]:
    harness_root = root / ".harness"
    if not harness_root.is_dir() or not (harness_root / "config.yaml").is_file() or not (harness_root / "manifest.json").is_file():
        return 1, ["AI SDLC Harness is not initialized. Run `ai-sdlc init` from the project root."]

    try:
        _validate_task_slug(task_slug)
    except ValueError as exc:
        return 2, [str(exc)]

    task_dir = _task_dir_path(task_slug)
    task_target = resolve_under_root(root, task_dir)
    if not task_target.is_dir():
        return 1, [f"Task folder does not exist: {task_dir.as_posix()}. Run `ai-sdlc task start` first."]

    spec_path = _spec_path(task_slug)
    req_path = _requirements_path(task_slug)
    try:
        spec_target = resolve_under_root(root, spec_path)
        req_target = resolve_under_root(root, req_path)
        entries = _load_manifest_entries(root)
    except Exception as exc:
        return 1, [f"Could not read harness manifest or resolve spec paths: {exc}"]

    spec_path_text = spec_path.as_posix()
    req_path_text = req_path.as_posix()

    artifacts = _read_source_artifacts(root, task_slug)
    by_name = {artifact.filename: artifact for artifact in artifacts}
    implementation_boundary = _extract_section(by_name, "task.md", "Implementation Boundary", ("Scope",))
    assumptions_open_questions = _extract_section(by_name, "task.md", "Assumptions And Open Questions", ("Open Questions",))
    requirements = _extract_section(
        by_name,
        "acceptance.md",
        "Requirements And Acceptance Criteria",
        ("Acceptance Criteria",),
    )
    protected_behavior = _extract_section(
        by_name,
        "acceptance.md",
        "Protected Behavior And Non-Goals",
        ("Non-Goals", "Non Goals"),
    )
    architecture_sections = [
        _extract_section(by_name, "architecture-notes.md", "Boundary"),
        _extract_section(by_name, "architecture-notes.md", "Responsibility Change"),
        _extract_section(by_name, "architecture-notes.md", "Existing Patterns To Preserve"),
        _extract_section(by_name, "architecture-notes.md", "Interface And Compatibility Impact"),
        _extract_section(by_name, "architecture-notes.md", "Architecture Hygiene"),
        _extract_section(by_name, "architecture-notes.md", "Trade-Offs And Open Questions", ("Trade Offs And Open Questions",)),
    ]
    coupling_sections = [
        _extract_section(by_name, "coupling-notes.md", "New Or Changed Coupling"),
        _extract_section(by_name, "coupling-notes.md", "Maintainability Sensors"),
    ]
    security = _extract_section(by_name, "architecture-notes.md", "Security And Privacy Risk Surface")
    test_sections = [
        _extract_section(by_name, "test-contract.md", "Characterization Tests"),
        _extract_section(by_name, "test-contract.md", "Desired Behavior Tests", ("Test Intent",)),
        _extract_section(by_name, "test-contract.md", "Regression Tests"),
        _extract_section(by_name, "test-contract.md", "Negative And Edge Cases"),
    ]
    verification_sections = [
        _extract_section(by_name, "verification.md", "Commands And Tests To Run", ("Commands And Tests To Run Later", "Verification Commands")),
        _extract_section(by_name, "verification.md", "Results", ("Verification Results",)),
        _extract_section(by_name, "verification.md", "Not Run / Why", ("Not Run", "Not Run Why")),
        _extract_section(by_name, "verification.md", "Manual Review Notes", ("Manual Verification", "Review Notes")),
    ]
    evidence_sections = [
        _extract_section(by_name, "evidence.md", "Final Evidence", ("Evidence", "Review Evidence")),
        _extract_section(by_name, "evidence.md", "Generated Or Updated Artifacts", ("Changed Artifacts", "Updated Artifacts")),
        _extract_section(by_name, "evidence.md", "Tests And Checks Run", ("Commands", "Tests", "Verification")),
        _extract_section(by_name, "evidence.md", "Results", ("Verification Results",)),
        _extract_section(by_name, "evidence.md", "Known Gaps And Risks", ("Known Gaps", "Risks")),
        _extract_section(by_name, "evidence.md", "References", ("Supporting Artifacts", "Links")),
    ]
    preflight = _read_preflight(root, task_slug)
    copied_preflight_findings = _preflight_findings(preflight)
    requirements_document = build_requirements_document(task_slug, requirements.text if requirements.state in {"ready", "TODO-only"} else "")
    requirements_content = render_requirements_yaml(requirements_document)
    findings = [
        *_source_artifact_findings(artifacts),
        *_readiness_findings(
            implementation_boundary=implementation_boundary,
            requirements=requirements,
            protected_behavior=protected_behavior,
            architecture_sections=architecture_sections,
            coupling_sections=coupling_sections,
            security=security,
            test_sections=test_sections,
            verification_sections=verification_sections,
            evidence_sections=evidence_sections,
            open_question_sections=[assumptions_open_questions, architecture_sections[-1]],
        ),
        *copied_preflight_findings,
        Finding("info", "spec generated deterministic Markdown consolidation."),
        Finding("info", "spec does not call AI models."),
        Finding("info", "spec does not generate requirements automatically."),
        Finding("info", "spec does not validate correctness, security, or compliance."),
        Finding("info", "spec does not run tests."),
        Finding("info", "spec does not enforce packs."),
    ]
    content = _render_report(
        slug=task_slug,
        generated_at=_timestamp(),
        artifacts=artifacts,
        preflight=preflight,
        implementation_boundary=implementation_boundary,
        assumptions_open_questions=assumptions_open_questions,
        requirements=requirements,
        protected_behavior=protected_behavior,
        architecture_sections=architecture_sections,
        coupling_sections=coupling_sections,
        security=security,
        test_sections=test_sections,
        verification_sections=verification_sections,
        evidence_sections=evidence_sections,
        findings=findings,
        preflight_findings=copied_preflight_findings,
        requirements_document=requirements_document,
    )

    generated_artifacts = [
        GeneratedArtifact(spec_path, spec_target, entries.get(spec_path_text), "spec report", content),
        GeneratedArtifact(req_path, req_target, entries.get(req_path_text), "requirements file", requirements_content),
    ]
    for artifact in generated_artifacts:
        blocking = _artifact_blocking_messages(artifact, force=force)
        if blocking:
            return 1, blocking

    messages = [f"spec report: {spec_path_text}", f"requirements file: {req_path_text}", _summary(findings)]
    if dry_run:
        for artifact in generated_artifacts:
            action = "would refresh" if artifact.target.exists() else "would create"
            messages.append(f"{action} file {artifact.path.as_posix()}")
        messages.append("dry run; no files written")
        return 0, messages

    unchanged = [
        artifact.target.exists() and artifact.target.read_text(encoding="utf-8") == artifact.content
        for artifact in generated_artifacts
    ]
    if all(unchanged):
        for artifact in generated_artifacts:
            messages.append(f"skip unchanged file {artifact.path.as_posix()}")
        messages.append("skip existing manifest .harness/manifest.json")
        return 0, messages

    for artifact, is_unchanged in zip(generated_artifacts, unchanged):
        if is_unchanged:
            messages.append(f"skip unchanged file {artifact.path.as_posix()}")
            continue
        action = "refresh" if artifact.target.exists() else "create"
        write_text(artifact.target, artifact.content)
        messages.append(f"{action} file {artifact.path.as_posix()}")

    write_manifest(root, extra_managed_paths=[spec_path, req_path])
    messages.append("refreshed manifest .harness/manifest.json")
    messages.append("root AGENTS.md and CLAUDE.md were not modified")
    messages.append(".harness/generated/agent-instructions.md was not modified")
    messages.append(f".harness/tasks/{task_slug}/generated/agent-workset.md was not modified")
    return 0, messages
