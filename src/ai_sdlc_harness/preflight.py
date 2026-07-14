"""Implementation of ``ai-sdlc preflight``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .constants import PREFLIGHT_FILENAME, TASK_ARTIFACT_FILENAMES, TASK_SLUG_PATTERN
from .detect import detect_project_signals
from .files import read_text, resolve_under_root, write_text
from .manifest import load_manifest, sha256_file, write_manifest


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Finding:
    level: str
    message: str


@dataclass(frozen=True)
class ArtifactReadiness:
    filename: str
    present: bool
    readable: bool
    state: str
    message: str
    text: str = ""

    @property
    def usable(self) -> bool:
        return self.present and self.readable and self.state == "ready"


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _preflight_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug / PREFLIGHT_FILENAME


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


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


def _line_is_placeholder(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        return True
    if TODO_RE.search(stripped):
        return True
    if stripped.startswith(("- [ ]", "* [ ]")):
        return True
    if stripped in {"-", "*", "[]"}:
        return True
    return False


def _section_bodies(text: str) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = []
            sections.append(current)
            continue
        if current is not None:
            current.append(line)
    return sections


def _normalize_heading(heading: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", heading.lower()).strip()
    return re.sub(r"\s+", " ", normalized)


def _section_lines(text: str, headings: tuple[str, ...]) -> list[str] | None:
    wanted = {_normalize_heading(heading) for heading in headings}
    current: list[str] | None = None
    current_level = 0
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading = _normalize_heading(match.group(2))
            if current is not None and level <= current_level:
                return current
            if heading in wanted:
                current = []
                current_level = level
                continue
        if current is not None:
            current.append(line)
    return current


def _section_state(text: str, headings: tuple[str, ...]) -> str:
    lines = _section_lines(text, headings)
    if lines is None:
        return "missing"
    if not "\n".join(lines).strip():
        return "empty"
    if any(not _line_is_placeholder(line) for line in lines):
        return "ready"
    return "todo-only"


def _section_text(text: str, headings: tuple[str, ...]) -> str:
    lines = _section_lines(text, headings)
    return "\n".join(lines or []).strip()


def _state_label(state: str) -> str:
    return state.replace("-", " ")


def _ready(state: str) -> bool:
    return state == "ready"


def _non_placeholder_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if not _line_is_placeholder(line)]
    return "\n".join(lines).strip()


def _looks_not_applicable(text: str) -> bool:
    lowered = _non_placeholder_text(text).lower()
    return any(
        marker in lowered
        for marker in (
            "not applicable",
            "n/a",
            "no security",
            "no privacy",
            "no security or privacy",
            "none expected",
        )
    )


def _looks_unknown(text: str) -> bool:
    lowered = _non_placeholder_text(text).lower()
    return any(marker in lowered for marker in ("unknown", "unclear", "to confirm", "needs confirmation"))


def _has_open_question(text: str) -> bool:
    lowered = _non_placeholder_text(text).lower()
    if not lowered:
        return False
    if any(marker in lowered for marker in ("no open question", "no unresolved question", "none")) and "?" not in lowered:
        return False
    return any(marker in lowered for marker in ("?", "unknown", "unresolved", "open question", "to confirm"))


def _has_blocking_open_question(text: str) -> bool:
    lowered = _non_placeholder_text(text).lower()
    return _has_open_question(text) and any(
        marker in lowered
        for marker in (
            "blocking",
            "blocker",
            "before implementation",
            "before coding",
            "must be answered",
            "must resolve",
        )
    )


def _readiness_state(text: str) -> str:
    if not text.strip():
        return "empty"

    lines = text.splitlines()
    substantive_lines = [line for line in lines if not _line_is_placeholder(line)]
    if not substantive_lines:
        return "todo-only"

    sections = _section_bodies(text)
    if sections:
        filled_sections = 0
        for section in sections:
            if any(not _line_is_placeholder(line) for line in section):
                filled_sections += 1
        if filled_sections * 2 < len(sections):
            return "mostly-unfilled"

    return "ready"


def _artifact_readiness(root: Path, slug: str) -> list[ArtifactReadiness]:
    readiness: list[ArtifactReadiness] = []
    for filename in TASK_ARTIFACT_FILENAMES:
        relative = PurePosixPath(".harness") / "tasks" / slug / filename
        target = resolve_under_root(root, relative)
        if not target.exists():
            readiness.append(
                ArtifactReadiness(
                    filename=filename,
                    present=False,
                    readable=False,
                    state="missing",
                    message="missing file",
                )
            )
            continue
        if not target.is_file():
            readiness.append(
                ArtifactReadiness(
                    filename=filename,
                    present=True,
                    readable=False,
                    state="unreadable",
                    message="not a regular file",
                )
            )
            continue
        try:
            text = read_text(target)
        except Exception as exc:
            readiness.append(
                ArtifactReadiness(
                    filename=filename,
                    present=True,
                    readable=False,
                    state="unreadable",
                    message=f"unreadable file: {exc}",
                )
            )
            continue
        state = _readiness_state(text)
        message = "usable content present" if state == "ready" else state.replace("-", " ")
        readiness.append(
            ArtifactReadiness(
                filename=filename,
                present=True,
                readable=True,
                state=state,
                message=message,
                text=text,
            )
        )
    return readiness


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


def _artifact_issue_findings(readiness: list[ArtifactReadiness]) -> list[Finding]:
    findings: list[Finding] = []
    for item in readiness:
        if not item.present:
            findings.append(Finding("blocker", f"{item.filename} is missing."))
        elif not item.readable:
            findings.append(Finding("blocker", f"{item.filename} is unreadable."))
        elif item.state != "ready":
            findings.append(Finding("warning", f"{item.filename} is {item.message}."))
    return findings


def _findings(signals: dict[str, Any], readiness: list[ArtifactReadiness]) -> list[Finding]:
    findings = _artifact_issue_findings(readiness)
    by_name = {item.filename: item for item in readiness}

    task = by_name.get("task.md")
    acceptance = by_name.get("acceptance.md")
    architecture = by_name.get("architecture-notes.md")
    coupling = by_name.get("coupling-notes.md")
    test_contract = by_name.get("test-contract.md")
    verification = by_name.get("verification.md")

    if task and task.readable:
        boundary_state = _section_state(task.text, ("Implementation Boundary", "Scope"))
        if not _ready(boundary_state):
            findings.append(Finding("blocker", f"implementation boundary is {_state_label(boundary_state)}."))
            findings.append(Finding("blocker", "expected change area is missing or still TODO."))
        open_question_text = _section_text(task.text, ("Assumptions And Open Questions", "Open Questions"))
        if _has_blocking_open_question(open_question_text):
            findings.append(Finding("blocker", "blocking open questions remain before implementation."))
        elif _has_open_question(open_question_text):
            findings.append(Finding("warning", "open questions remain for human review."))

    if acceptance and acceptance.readable:
        requirements_state = _section_state(
            acceptance.text,
            ("Requirements And Acceptance Criteria", "Acceptance Criteria"),
        )
        protected_state = _section_state(
            acceptance.text,
            ("Protected Behavior And Non-Goals", "Non-Goals", "Non Goals"),
        )
        if not _ready(requirements_state):
            findings.append(
                Finding("blocker", f"requirements and acceptance criteria are {_state_label(requirements_state)}.")
            )
        if not _ready(protected_state):
            findings.append(Finding("warning", f"protected behavior and non-goals are {_state_label(protected_state)}."))

    if architecture and architecture.readable:
        architecture_boundary_state = _section_state(architecture.text, ("Boundary", "Architecture Boundary"))
        responsibility_state = _section_state(architecture.text, ("Responsibility Change",))
        interface_state = _section_state(architecture.text, ("Interface And Compatibility Impact",))
        security_state = _section_state(architecture.text, ("Security And Privacy Risk Surface",))
        security_text = _section_text(architecture.text, ("Security And Privacy Risk Surface",))
        architecture_open_questions = _section_text(
            architecture.text,
            ("Trade-Offs And Open Questions", "Trade Offs And Open Questions"),
        )

        if not (_ready(architecture_boundary_state) and _ready(responsibility_state)):
            findings.append(Finding("warning", "architecture boundary or responsibility impact is missing or still TODO."))
        if not _ready(interface_state):
            findings.append(Finding("warning", f"interface and compatibility impact is {_state_label(interface_state)}."))
        if not _ready(security_state) or _looks_unknown(security_text):
            findings.append(Finding("warning", "security/privacy risk surface is unknown or still TODO."))
        elif _looks_not_applicable(security_text):
            findings.append(Finding("info", "no security/privacy-sensitive surface was identified in task notes."))
        else:
            findings.append(Finding("info", "security/privacy risk-surface notes are present for review."))
        if _has_blocking_open_question(architecture_open_questions):
            findings.append(Finding("blocker", "blocking architecture open questions remain before implementation."))
        elif _has_open_question(architecture_open_questions):
            findings.append(Finding("warning", "architecture open questions remain for human review."))

    if coupling and coupling.readable:
        coupling_state = _section_state(coupling.text, ("New Or Changed Coupling",))
        maintainability_state = _section_state(coupling.text, ("Maintainability Sensors",))
        if not (_ready(coupling_state) and _ready(maintainability_state)):
            findings.append(Finding("warning", "coupling or maintainability notes are missing or still TODO."))

    if test_contract and test_contract.readable:
        desired_state = _section_state(test_contract.text, ("Desired Behavior Tests", "Test Intent"))
        regression_state = _section_state(test_contract.text, ("Regression Tests",))
        edge_state = _section_state(test_contract.text, ("Negative And Edge Cases",))
        if not (_ready(desired_state) or _ready(regression_state) or _ready(edge_state)):
            findings.append(Finding("warning", "test intent is missing or still TODO."))

    if verification and verification.readable:
        commands_state = _section_state(
            verification.text,
            ("Commands And Tests To Run", "Commands And Tests To Run Later", "Verification Commands"),
        )
        results_state = _section_state(verification.text, ("Results", "Verification Results"))
        if not _ready(commands_state):
            findings.append(Finding("warning", "verification command intent is missing or still TODO."))
        if not _ready(results_state):
            findings.append(Finding("warning", "verification results are not recorded yet."))

    if not signals.get("detected_test_frameworks"):
        findings.append(Finding("warning", "no test framework detected."))
    if not signals.get("detected_ci"):
        findings.append(Finding("warning", "no CI detected."))

    findings.append(Finding("info", "preflight uses shallow section and marker-based checks only."))
    findings.append(Finding("info", "preflight does not call AI models, scan security, run tests, validate compliance, or enforce packs."))
    findings.append(Finding("info", "no optional packs are selected."))
    return findings


def _recommended_actions(findings: list[Finding]) -> list[str]:
    blockers = [finding for finding in findings if finding.level == "blocker"]
    warnings = [finding for finding in findings if finding.level == "warning"]
    actions: list[str] = []
    if blockers:
        actions.append("Resolve blocker findings before asking a human or coding agent to implement the task.")
    if any("implementation boundary" in finding.message or "expected change area" in finding.message for finding in warnings + blockers):
        actions.append("Fill in the implementation boundary with the expected change area and limits.")
    if any("requirements and acceptance" in finding.message for finding in warnings + blockers):
        actions.append("Add concrete requirements and observable acceptance criteria.")
    if any("protected behavior" in finding.message or "interface and compatibility" in finding.message for finding in warnings):
        actions.append("Record protected areas, non-goals, and interface impact before implementation.")
    if any("architecture" in finding.message or "coupling" in finding.message for finding in warnings):
        actions.append("Fill in architecture, responsibility, coupling, and maintainability notes.")
    if any("security/privacy" in finding.message for finding in warnings):
        actions.append("Record whether the task touches security or privacy-sensitive surfaces, or mark not applicable.")
    if any("test intent" in finding.message or "verification command" in finding.message for finding in warnings):
        actions.append("Record intended tests and verification commands before implementation.")
    if any("verification results" in finding.message for finding in warnings):
        actions.append("Record verification results after implementation, or note not run and why.")
    if not actions:
        actions.append("Review the task artifacts and proceed with bounded implementation planning.")
    return actions


def _readiness_line(item: ArtifactReadiness | None, label: str, headings: tuple[str, ...]) -> str:
    if item is None or not item.present:
        return f"- {label}: missing"
    if not item.readable:
        return f"- {label}: unreadable"
    return f"- {label}: {_state_label(_section_state(item.text, headings))}"


def _artifact_presence_lines(readiness: list[ArtifactReadiness]) -> list[str]:
    lines: list[str] = []
    for item in readiness:
        present = "yes" if item.present else "no"
        readable = "yes" if item.readable else "no"
        lines.append(f"- {item.filename}: present={present}; readable={readable}; readiness={item.message}")
    return lines


def _render_report(
    *,
    slug: str,
    generated_at: str,
    signals: dict[str, Any],
    selected_packs: list[dict[str, Any]],
    readiness: list[ArtifactReadiness],
    findings: list[Finding],
) -> str:
    counts = {
        "blocker": sum(1 for finding in findings if finding.level == "blocker"),
        "warning": sum(1 for finding in findings if finding.level == "warning"),
        "info": sum(1 for finding in findings if finding.level == "info"),
    }
    by_name = {item.filename: item for item in readiness}
    answer = (
        "Needs boundary clarification before implementation."
        if counts["blocker"]
        else "No blocker findings; review warnings before implementation."
    )
    lines = [
        "# Preflight Report",
        "",
        f"Task slug: `{slug}`",
        f"Generated: {generated_at}",
        "",
        "## Summary",
        "",
        f"- Boundary answer: {answer}",
        f"- Blockers: {counts['blocker']}",
        f"- Warnings: {counts['warning']}",
        f"- Info: {counts['info']}",
        "",
        "## Boundary Readiness",
        "",
        _readiness_line(by_name.get("task.md"), "Implementation boundary", ("Implementation Boundary", "Scope")),
        _readiness_line(by_name.get("task.md"), "Assumptions and open questions", ("Assumptions And Open Questions", "Open Questions")),
        _readiness_line(
            by_name.get("acceptance.md"),
            "Protected behavior and non-goals",
            ("Protected Behavior And Non-Goals", "Non-Goals", "Non Goals"),
        ),
        _readiness_line(
            by_name.get("architecture-notes.md"),
            "Interface and compatibility impact",
            ("Interface And Compatibility Impact",),
        ),
        "",
        "## Requirements Readiness",
        "",
        _readiness_line(
            by_name.get("acceptance.md"),
            "Requirements and acceptance criteria",
            ("Requirements And Acceptance Criteria", "Acceptance Criteria"),
        ),
        "",
        "## Architecture / Coupling Readiness",
        "",
        _readiness_line(by_name.get("architecture-notes.md"), "Architecture boundary", ("Boundary", "Architecture Boundary")),
        _readiness_line(by_name.get("architecture-notes.md"), "Responsibility change", ("Responsibility Change",)),
        _readiness_line(by_name.get("coupling-notes.md"), "New or changed coupling", ("New Or Changed Coupling",)),
        _readiness_line(by_name.get("coupling-notes.md"), "Maintainability sensors", ("Maintainability Sensors",)),
        "",
        "## Security / Privacy Risk-Surface Readiness",
        "",
        _readiness_line(
            by_name.get("architecture-notes.md"),
            "Security and privacy risk surface",
            ("Security And Privacy Risk Surface",),
        ),
        "",
        "## Test / Verification Readiness",
        "",
        _readiness_line(by_name.get("test-contract.md"), "Desired behavior tests", ("Desired Behavior Tests", "Test Intent")),
        _readiness_line(by_name.get("test-contract.md"), "Regression tests", ("Regression Tests",)),
        _readiness_line(by_name.get("test-contract.md"), "Negative and edge cases", ("Negative And Edge Cases",)),
        _readiness_line(
            by_name.get("verification.md"),
            "Verification commands",
            ("Commands And Tests To Run", "Commands And Tests To Run Later", "Verification Commands"),
        ),
        _readiness_line(by_name.get("verification.md"), "Verification results", ("Results", "Verification Results")),
        "",
        "## Open Questions",
        "",
        _readiness_line(by_name.get("task.md"), "Task assumptions and open questions", ("Assumptions And Open Questions", "Open Questions")),
        _readiness_line(
            by_name.get("architecture-notes.md"),
            "Architecture trade-offs and open questions",
            ("Trade-Offs And Open Questions", "Trade Offs And Open Questions"),
        ),
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
        "## Selected Packs",
        "",
        "A pack is an optional reusable guardrail bundle. Packs are not the foundation baseline and are not selected by default.",
    ]
    if selected_packs:
        for pack in selected_packs:
            enabled = "yes" if pack["enabled"] else "no"
            lines.append(f"- {pack['id']} (version: {pack['version']}, enabled: {enabled})")
    else:
        lines.append("- none selected")

    lines.extend(
        [
            "",
            "## Findings",
            "",
        ]
    )
    for finding in findings:
        lines.append(f"- {finding.level}: {finding.message}")

    lines.extend(["", "## Recommended Next Actions", ""])
    for action in _recommended_actions(findings):
        lines.append(f"- {action}")

    lines.extend(
        [
            "",
            "## Disclaimer",
            "",
            "This report checks whether task artifacts appear bounded enough for implementation by a human or coding agent. It uses deterministic, shallow Markdown markers only. It does not ask interactive questions, call AI models, scan for security issues, run tests, validate compliance, enforce packs, execute packs, or prove correctness.",
            "",
            "Artifact presence details:",
        ]
    )
    lines.extend(_artifact_presence_lines(readiness))

    return "\n".join(lines) + "\n"


def _summarize(findings: list[Finding]) -> str:
    blockers = sum(1 for finding in findings if finding.level == "blocker")
    warnings = sum(1 for finding in findings if finding.level == "warning")
    infos = sum(1 for finding in findings if finding.level == "info")
    return f"summary: {blockers} blocker(s), {warnings} warning(s), {infos} info"


def run_preflight(root: Path, task_slug: str, *, dry_run: bool = False, force: bool = False) -> tuple[int, list[str]]:
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

    preflight_path = _preflight_path(task_slug)
    try:
        preflight_target = resolve_under_root(root, preflight_path)
        entries = _load_manifest_entries(root)
    except Exception as exc:
        return 1, [f"Could not read harness manifest or resolve preflight path: {exc}"]

    path_text = preflight_path.as_posix()
    entry = entries.get(path_text)
    if preflight_target.exists() and entry is None:
        return 1, [
            f"unmanaged existing file {path_text}",
            "refusing to overwrite unmanaged preflight report",
        ]

    if preflight_target.exists() and entry is not None:
        expected_hash = entry.get("sha256")
        hash_algorithm = entry.get("hash_algorithm")
        hash_clean = hash_algorithm == "sha256" and expected_hash and sha256_file(preflight_target) == expected_hash
        if not hash_clean and not force:
            return 1, [
                f"hash drift detected for {path_text}",
                "refusing to overwrite manifest-managed preflight report without --force",
            ]

    signals = detect_project_signals(root)
    readiness = _artifact_readiness(root, task_slug)
    selected_packs = _load_selected_packs(root)
    findings = _findings(signals, readiness)
    content = _render_report(
        slug=task_slug,
        generated_at=_timestamp(),
        signals=signals,
        selected_packs=selected_packs,
        readiness=readiness,
        findings=findings,
    )

    messages = [f"preflight report: {path_text}", _summarize(findings)]
    if dry_run:
        action = "would refresh" if preflight_target.exists() else "would create"
        messages.append(f"{action} file {path_text}")
        messages.append("dry run; no files written")
        return 0, messages

    if preflight_target.exists() and preflight_target.read_text(encoding="utf-8") == content:
        messages.append(f"skip unchanged file {path_text}")
        messages.append("skip existing manifest .harness/manifest.json")
        return 0, messages

    action = "refresh" if preflight_target.exists() else "create"
    write_text(preflight_target, content)
    write_manifest(root, extra_managed_paths=[preflight_path])
    messages.append(f"{action} file {path_text}")
    messages.append("refreshed manifest .harness/manifest.json")
    messages.append("root AGENTS.md and CLAUDE.md were not modified")
    return 0, messages
