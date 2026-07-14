"""Implementation of ``ai-sdlc generate``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .constants import (
    AGENT_WORKSET_FILENAME,
    REQUIREMENTS_FILENAME,
    SPEC_FILENAME,
    TASK_ARTIFACT_FILENAMES,
    TASK_GENERATED_DIRNAME,
    TASK_SLUG_PATTERN,
)
from .detect import detect_project_signals
from .files import read_text, resolve_under_root, write_text
from .manifest import load_manifest, sha256_file, write_manifest
from .redact import redact_text
from .requirements import RequirementsReadResult, read_requirements
from .task_metadata import extract_task_title


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
ENV_LINE_RE = re.compile(
    r"^\s*(?:export\s+|\$env:)?[A-Z_][A-Z0-9_]{1,}\s*=",
    re.IGNORECASE,
)
FINDING_LINE_RE = re.compile(r"^\s*-?\s*(blocker|warning|info):\s+(.+?)\s*$", re.IGNORECASE)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
MAX_ARTIFACT_EXCERPT_CHARS = 1200
MAX_REPORT_EXCERPT_CHARS = 900
MAX_EXCERPT_LINES = 40


DISCLAIMER = (
    "This workset compiles task-scoped context for implementation. It does not call AI models, "
    "generate code, generate tests, run tests, inspect target code deeply, scan security, validate compliance, "
    "prove correctness, security, or release readiness, enforce packs, or install adapters."
)


@dataclass(frozen=True)
class InputArtifact:
    filename: str
    text: str


@dataclass(frozen=True)
class OptionalReport:
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


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


def _artifact_path(slug: str, filename: str) -> PurePosixPath:
    return _task_dir_path(slug) / filename


def _workset_path(slug: str) -> PurePosixPath:
    return _task_dir_path(slug) / TASK_GENERATED_DIRNAME / AGENT_WORKSET_FILENAME


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


def _read_required_inputs(root: Path, slug: str) -> tuple[list[InputArtifact], list[str]]:
    artifacts: list[InputArtifact] = []
    failures: list[str] = []
    for filename in TASK_ARTIFACT_FILENAMES:
        relative = _artifact_path(slug, filename)
        target = resolve_under_root(root, relative)
        if not target.exists():
            failures.append(f"missing required task input {relative.as_posix()}")
            continue
        if not target.is_file():
            failures.append(f"unreadable required task input {relative.as_posix()}: not a regular file")
            continue
        try:
            artifacts.append(InputArtifact(filename=filename, text=read_text(target)))
        except Exception as exc:
            failures.append(f"unreadable required task input {relative.as_posix()}: {exc}")
    return artifacts, failures


def _read_optional_report(root: Path, slug: str, filename: str) -> OptionalReport:
    relative = _artifact_path(slug, filename)
    target = resolve_under_root(root, relative)
    if not target.exists():
        return OptionalReport(filename, present=False, readable=False, text="", message="not generated")
    if not target.is_file():
        return OptionalReport(filename, present=True, readable=False, text="", message="not a regular file")
    try:
        return OptionalReport(filename, present=True, readable=True, text=read_text(target), message="present")
    except Exception as exc:
        return OptionalReport(filename, present=True, readable=False, text="", message=f"unreadable: {exc}")


def _load_selected_packs(root: Path) -> list[dict[str, Any]]:
    selected_path = resolve_under_root(root, ".harness/packs/selected.yaml")
    try:
        loaded = yaml.safe_load(read_text(selected_path))
    except Exception:
        return []
    if not isinstance(loaded, dict):
        return []
    selected = loaded.get("selected_packs")
    if not isinstance(selected, list):
        return []

    packs: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        pack_id = item.get("id")
        if not pack_id:
            continue
        packs.append(
            {
                "id": str(pack_id),
                "version": str(item.get("version", "unknown")),
                "enabled": bool(item.get("enabled", False)),
            }
        )
    return packs


def _format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _strip_environment_lines(text: str) -> tuple[str, int]:
    kept: list[str] = []
    removed = 0
    for line in text.splitlines():
        if ENV_LINE_RE.match(line):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed


def _bounded_excerpt(text: str, *, max_chars: int) -> str:
    redacted = redact_text(text)
    filtered, removed_env_lines = _strip_environment_lines(redacted)
    lines = filtered.strip().splitlines()
    truncated = False
    if len(lines) > MAX_EXCERPT_LINES:
        lines = lines[:MAX_EXCERPT_LINES]
        truncated = True
    excerpt = "\n".join(lines).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip()
        truncated = True
    notes: list[str] = []
    if removed_env_lines:
        notes.append(f"[{removed_env_lines} environment-style line(s) omitted.]")
    if truncated:
        notes.append("[Excerpt truncated; see the source artifact for full content.]")
    if notes:
        excerpt = (excerpt + "\n\n" if excerpt else "") + "\n".join(notes)
    return excerpt or "[No excerptable content.]"


def _line_is_placeholder(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped in {"-", "*"}:
        return True
    if TODO_RE.search(stripped):
        return True
    return False


def _section_state(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return "missing"
    if all(_line_is_placeholder(line) for line in lines):
        return "todo-only"
    return "ready"


def _section_text(text: str, aliases: tuple[str, ...]) -> tuple[str, str] | None:
    alias_map = {alias.casefold(): alias for alias in aliases}
    lines = text.splitlines()
    start_index: int | None = None
    selected_heading = ""
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line.strip())
        if not match or match.group(1) != "##":
            continue
        heading = match.group(2).strip()
        if heading.casefold() not in alias_map:
            continue
        start_index = index + 1
        selected_heading = alias_map[heading.casefold()]
        break

    if start_index is None:
        return None

    end_index = len(lines)
    for index in range(start_index, len(lines)):
        match = HEADING_RE.match(lines[index].strip())
        if match and match.group(1) == "##":
            end_index = index
            break
    section = "\n".join(lines[start_index:end_index]).strip()
    return selected_heading, section


def _extract_section(artifact: InputArtifact, heading: str, aliases: tuple[str, ...] = ()) -> ExtractedSection:
    found = _section_text(artifact.text, (heading, *aliases))
    if found is None:
        return ExtractedSection(artifact.filename, heading, "", "missing")
    selected_heading, text = found
    return ExtractedSection(artifact.filename, selected_heading, text, _section_state(text))


def _render_extracted_section(section: ExtractedSection) -> list[str]:
    lines = [f"### {section.heading}", "", f"Source: `{section.filename}`"]
    if section.state == "missing":
        lines.append(f"- warning: workset context section `{section.heading}` is missing from `{section.filename}`.")
        lines.append("")
        return lines
    if section.state == "todo-only":
        lines.append(f"- warning: workset context section `{section.heading}` in `{section.filename}` is TODO-only.")
    else:
        lines.append("- Status: ready.")
    lines.extend(
        [
            "",
            "```text",
            _bounded_excerpt(section.text, max_chars=MAX_ARTIFACT_EXCERPT_CHARS),
            "```",
            "",
        ]
    )
    return lines


def _render_section_group(title: str, sections: list[ExtractedSection]) -> list[str]:
    lines = [f"## {title}", ""]
    for section in sections:
        lines.extend(_render_extracted_section(section))
    return lines


def _findings_from_report(report: OptionalReport) -> list[str]:
    if not report.present:
        return [f"warning: {report.filename} has not been generated."]
    if not report.readable:
        return [f"warning: {report.filename} is unreadable: {report.message}."]
    findings: list[str] = []
    for line in report.text.splitlines():
        match = FINDING_LINE_RE.match(line)
        if not match:
            continue
        findings.append(f"{match.group(1).lower()}: {match.group(2)}")
    return findings[:20] if findings else [f"info: {report.filename} has no blocker, warning, or info findings."]


def _render_optional_report(report: OptionalReport) -> list[str]:
    lines = [f"Source: `{report.filename}`", ""]
    if not report.present:
        lines.append(f"- warning: workset context report `{report.filename}` has not been generated.")
        return lines
    if not report.readable:
        lines.append(f"- warning: workset context report `{report.filename}` is unreadable: {report.message}.")
        return lines
    lines.extend(
        [
            "- Status: present and readable.",
            "",
            "```text",
            _bounded_excerpt(report.text, max_chars=MAX_REPORT_EXCERPT_CHARS),
            "```",
        ]
    )
    return lines


def _render_structured_requirements(result: RequirementsReadResult) -> list[str]:
    lines = ["## Structured Requirements", "", f"Source: `{REQUIREMENTS_FILENAME}`", ""]
    if not result.present:
        lines.append("- warning: structured requirements `requirements.yaml` has not been generated.")
        return lines
    if not result.readable:
        lines.append(f"- warning: structured requirements `requirements.yaml` is unreadable: {result.message}.")
        return lines
    if not result.schema_valid:
        lines.append(f"- warning: structured requirements `requirements.yaml` is invalid: {'; '.join(result.problems)}")
        return lines

    lines.extend(
        [
            "- Status: present, readable, and schema valid.",
            "- Artifact role: advisory projection.",
            "- Authority: non-authoritative.",
            "- Edit model: edit source task artifacts and rerun `ai-sdlc spec --task <slug>`.",
            "- Advisory: `requirements.yaml` is a generated projection, not the authoritative source of truth.",
            f"- Requirement count: {result.requirement_count}",
            f"- Finding count: {result.finding_count}",
            "",
            "Requirements:",
            "",
        ]
    )
    if result.requirements:
        for requirement in result.requirements:
            lines.append(f"- `{requirement['id']}` [{requirement['status']}]: {requirement['statement']}")
            lines.append(f"  - acceptance_criteria: {requirement['acceptance_criteria']}")
            lines.append(f"  - verification: {requirement['verification']}")
    else:
        lines.append("- none recorded")

    lines.extend(["", "Findings:", ""])
    if result.findings:
        for finding in result.findings:
            lines.append(f"- {finding['level']}: {finding['message']}")
    else:
        lines.append("- none")
    return lines


def _render_workset(
    *,
    slug: str,
    generated_at: str,
    artifacts: list[InputArtifact],
    spec: OptionalReport,
    structured_requirements: RequirementsReadResult,
    preflight: OptionalReport,
    test_contract_review: OptionalReport,
    selected_packs: list[dict[str, Any]],
    signals: dict[str, Any],
) -> str:
    by_name = {artifact.filename: artifact for artifact in artifacts}
    task_title = extract_task_title(by_name["task.md"].text, slug)
    task_boundary = [
        _extract_section(by_name["task.md"], "Implementation Boundary", ("Scope",)),
        _extract_section(by_name["task.md"], "Assumptions And Open Questions"),
        _extract_section(by_name["acceptance.md"], "Protected Behavior And Non-Goals", ("Non-Goals", "Non Goals")),
    ]
    requirements = [
        _extract_section(
            by_name["acceptance.md"],
            "Requirements And Acceptance Criteria",
            ("Acceptance Criteria",),
        )
    ]
    architecture_coupling = [
        _extract_section(by_name["architecture-notes.md"], "Boundary"),
        _extract_section(by_name["architecture-notes.md"], "Responsibility Change"),
        _extract_section(by_name["architecture-notes.md"], "Existing Patterns To Preserve"),
        _extract_section(by_name["architecture-notes.md"], "Interface And Compatibility Impact"),
        _extract_section(by_name["architecture-notes.md"], "Architecture Hygiene"),
        _extract_section(by_name["architecture-notes.md"], "Trade-Offs And Open Questions"),
        _extract_section(by_name["coupling-notes.md"], "New Or Changed Coupling"),
        _extract_section(by_name["coupling-notes.md"], "Maintainability Sensors"),
    ]
    security_privacy = [
        _extract_section(by_name["architecture-notes.md"], "Security And Privacy Risk Surface"),
    ]
    verification_expectations = [
        _extract_section(
            by_name["verification.md"],
            "Commands And Tests To Run",
            ("Commands And Tests To Run Later", "Verification Commands"),
        ),
        _extract_section(by_name["verification.md"], "Results", ("Verification Results",)),
        _extract_section(by_name["verification.md"], "Manual Review Notes", ("Manual Verification", "Review Notes")),
    ]
    evidence_expectations = [
        _extract_section(by_name["evidence.md"], "Final Evidence", ("Evidence", "Review Evidence")),
        _extract_section(by_name["evidence.md"], "Generated Or Updated Artifacts", ("Changed Artifacts", "Updated Artifacts")),
        _extract_section(by_name["evidence.md"], "Tests And Checks Run", ("Commands", "Tests", "Verification")),
        _extract_section(by_name["evidence.md"], "Results", ("Verification Results",)),
        _extract_section(by_name["evidence.md"], "Known Gaps And Risks", ("Known Gaps", "Risks")),
        _extract_section(by_name["evidence.md"], "References", ("Supporting Artifacts", "Links")),
    ]
    lines = [
        "# Agent Workset",
        "",
        f"Task slug: `{slug}`",
        f"Generated: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Task title: {task_title}",
        f"- Task slug: `{slug}`",
        "- Purpose: pre-implementation context for a human or coding agent before changing code.",
        "- Inputs: task artifacts, `spec.md` when present, `preflight.md`, `test-contract-review.md`, selected optional pack records, and shallow repository signals.",
        "- Post-implementation reports are not implementation inputs: `evidence-report.md` and `validation-report.md` are not consumed.",
        "",
        "## Implementation Contract",
        "",
        "- This is the task-scoped implementation contract compiled from current task artifacts.",
        "- Implement only after blocker findings are resolved or explicitly accepted by the human.",
        "- Treat this as bounded implementation guidance for the selected task only.",
        "- Stay within task scope and preserve documented assumptions.",
        "- Preserve protected areas and non-goals unless the human explicitly changes them.",
        "- Do not silently change public behavior outside task scope.",
        "- Treat interface impact, security/privacy risk-surface notes, and unresolved questions as review prompts.",
        "- Preserve compatibility unless the task artifacts explicitly change it.",
        "- Document implementation deviations and unresolved risks in `evidence.md`.",
        "",
        "Task inputs used:",
        "",
    ]
    for filename in TASK_ARTIFACT_FILENAMES:
        lines.append(f"- `{filename}`: present and readable")

    lines.append("")
    lines.extend(["## Specification", ""])
    lines.extend(_render_optional_report(spec))
    lines.extend(["", "Signals:", ""])
    for finding in _findings_from_report(spec):
        lines.append(f"- {finding}")
    lines.append("")
    lines.extend(_render_structured_requirements(structured_requirements))
    lines.append("")
    lines.extend(_render_section_group("Task Boundary", task_boundary))
    lines.extend(_render_section_group("Requirements / Acceptance Criteria", requirements))
    lines.extend(_render_section_group("Architecture / Coupling Notes", architecture_coupling))
    lines.extend(_render_section_group("Security / Privacy Risk-Surface Notes", security_privacy))
    lines.extend(["## Preflight Signals", ""])
    lines.extend(_render_optional_report(preflight))
    lines.extend(["", "Signals:", ""])
    for finding in _findings_from_report(preflight):
        lines.append(f"- {finding}")
    lines.extend(["", "## Test-Contract Signals", ""])
    lines.extend(_render_optional_report(test_contract_review))
    lines.extend(["", "Signals:", ""])
    for finding in _findings_from_report(test_contract_review):
        lines.append(f"- {finding}")
    lines.append("")
    lines.extend(_render_section_group("Verification Expectations", verification_expectations))
    lines.extend(_render_section_group("Evidence Expectations", evidence_expectations))
    lines.extend(["## Optional Packs", ""])
    if selected_packs:
        lines.append("Selected pack records are listed for context only. The workset does not imply pack enforcement.")
        lines.append("")
        for pack in selected_packs:
            enabled = "yes" if pack["enabled"] else "no"
            lines.append(f"- selected record: {pack['id']} (version: {pack['version']}, enabled: {enabled})")
    else:
        lines.append("No optional packs selected. The foundation workflow still applies.")

    lines.extend(
        [
            "",
            "## Repository Signals",
            "",
            f"- Git repo detected: {'yes' if signals.get('git_repo') else 'no'}",
            f"- Detected languages: {_format_list(signals.get('detected_languages', []))}",
            f"- Detected package managers: {_format_list(signals.get('detected_package_managers', []))}",
            f"- Detected test frameworks: {_format_list(signals.get('detected_test_frameworks', []))}",
            f"- Detected CI: {_format_list(signals.get('detected_ci', []))}",
            f"- Existing agent files: {_format_list(signals.get('existing_agent_files', []))}",
            "",
            "## Implementation Instructions",
            "",
            "- Implement only the task described by the task artifacts.",
            "- Stay within the task boundary.",
            "- Preserve documented assumptions unless the human explicitly changes them.",
            "- Preserve protected areas, non-goals, and existing interfaces unless explicitly changed in the task artifacts.",
            "- Do not silently change public behavior outside task scope.",
            "- Do not silently expand scope.",
            "- Preserve compatibility unless explicitly changed in the task artifacts.",
            "- Treat interface impact, security/privacy risk-surface notes, and unresolved questions as review prompts.",
            "- Implement only after blocker findings are resolved or explicitly accepted by the human.",
            "- Update tests according to the intent in `test-contract.md` and verification expectations.",
            "- Record deviations, commands run, results, not-run rationale, gaps, and references in `evidence.md` and `verification.md`.",
            "",
            "## Human Review Notes",
            "",
            "- [ ] Implementation summary recorded in `evidence.md`.",
            "- [ ] Protected areas, interface impact, and unresolved questions reviewed.",
            "- [ ] Tests updated according to `test-contract.md` intent.",
            "- [ ] Security/privacy risk-surface notes reviewed where applicable.",
            "- [ ] Verification commands and results recorded in `verification.md` or `evidence.md`.",
            "- [ ] Deviations from this workset recorded in `evidence.md`.",
            "- [ ] Unresolved risks or follow-up work recorded in `evidence.md`.",
            "- [ ] Any blocker findings were resolved or explicitly accepted by the human.",
            "",
            "## Disclaimer",
            "",
            DISCLAIMER,
            "",
        ]
    )
    return "\n".join(lines)


def _summary(preflight: OptionalReport, test_contract_review: OptionalReport) -> str:
    missing = [
        report.filename
        for report in (preflight, test_contract_review)
        if not report.present or not report.readable
    ]
    if missing:
        return f"summary: required inputs ready; readiness warning(s): {', '.join(missing)}"
    return "summary: required inputs ready; readiness reports present"


def run_generate(root: Path, task_slug: str, *, dry_run: bool = False, force: bool = False) -> tuple[int, list[str]]:
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

    workset_path = _workset_path(task_slug)
    try:
        workset_target = resolve_under_root(root, workset_path)
        entries = _load_manifest_entries(root)
    except Exception as exc:
        return 1, [f"Could not read harness manifest or resolve agent workset path: {exc}"]

    path_text = workset_path.as_posix()
    entry = entries.get(path_text)
    if workset_target.exists() and entry is None:
        return 1, [
            f"unmanaged existing file {path_text}",
            "refusing to overwrite unmanaged agent workset",
        ]

    if workset_target.exists() and entry is not None:
        expected_hash = entry.get("sha256")
        hash_algorithm = entry.get("hash_algorithm")
        hash_clean = hash_algorithm == "sha256" and expected_hash and sha256_file(workset_target) == expected_hash
        if not hash_clean and not force:
            return 1, [
                f"hash drift detected for {path_text}",
                "refusing to overwrite manifest-managed agent workset without --force",
            ]

    artifacts, failures = _read_required_inputs(root, task_slug)
    if failures:
        return 1, ["Cannot generate agent workset."] + [f"- {failure}" for failure in failures]

    spec = _read_optional_report(root, task_slug, SPEC_FILENAME)
    structured_requirements = read_requirements(root, task_slug)
    preflight = _read_optional_report(root, task_slug, "preflight.md")
    test_contract_review = _read_optional_report(root, task_slug, "test-contract-review.md")
    content = _render_workset(
        slug=task_slug,
        generated_at=_timestamp(),
        artifacts=artifacts,
        spec=spec,
        structured_requirements=structured_requirements,
        preflight=preflight,
        test_contract_review=test_contract_review,
        selected_packs=_load_selected_packs(root),
        signals=detect_project_signals(root),
    )

    messages = [f"agent workset: {path_text}", _summary(preflight, test_contract_review)]
    if dry_run:
        action = "would refresh" if workset_target.exists() else "would create"
        messages.append(f"{action} file {path_text}")
        messages.append("dry run; no files written")
        return 0, messages

    if workset_target.exists() and workset_target.read_text(encoding="utf-8") == content:
        messages.append(f"skip unchanged file {path_text}")
        messages.append("skip existing manifest .harness/manifest.json")
        return 0, messages

    action = "refresh" if workset_target.exists() else "create"
    write_text(workset_target, content)
    write_manifest(root, extra_managed_paths=[workset_path])
    messages.append(f"{action} file {path_text}")
    messages.append("refreshed manifest .harness/manifest.json")
    messages.append("root AGENTS.md and CLAUDE.md were not modified")
    messages.append(".harness/generated/agent-instructions.md was not modified")
    return 0, messages
