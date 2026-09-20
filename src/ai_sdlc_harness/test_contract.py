"""Implementation of ``ai-sdlc test-contract``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .constants import TEST_CONTRACT_REVIEW_FILENAME, TASK_SLUG_PATTERN
from .detect import detect_project_signals, merge_documented_test_frameworks
from .files import (
    persist_exact_bytes,
    resolve_managed_output_under_root,
    resolve_under_root,
)
from .lineage import (
    CapturedDependency,
    build_provenance_record,
    capture_dependencies,
    lineage_definition_for_task,
    provenance_dependencies,
    snapshot_repository_observations,
)
from .manifest import (
    build_managed_file_record,
    load_manifest,
    load_manifest_model,
    persist_manifest_model,
    remove_provenance_records,
    replace_managed_and_provenance_records,
    sha256_file,
)


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
TEST_COMMAND_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|coverage|npm\s+test|npm\s+run\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|dotnet\s+test|mvn\s+test|gradle\s+test)\b",
    re.IGNORECASE,
)
NOT_RUN_RE = re.compile(
    r"\b(not run|not-run|skipped|skip|not applicable|n/a|manual only|manual-only|not practical|no automated)\b",
    re.IGNORECASE,
)
RATIONALE_RE = re.compile(r"\b(because|since|due to|reason|why|manual verification|manual review|not practical|not applicable|n/a)\b", re.IGNORECASE)


HEADING_ALIASES = {
    "requirements": (
        "Requirements And Acceptance Criteria",
        "Acceptance Criteria",
    ),
    "protected": (
        "Protected Behavior And Non-Goals",
        "Non-Goals",
        "Non Goals",
    ),
    "characterization": (
        "Characterization Tests",
    ),
    "desired": (
        "Desired Behavior Tests",
        "Test Intent",
    ),
    "regression": (
        "Regression Tests",
    ),
    "negative": (
        "Negative And Edge Cases",
    ),
    "commands": (
        "Commands And Checks Run",
        "Commands And Tests To Run",
        "Commands And Tests To Run Later",
        "Verification Commands",
    ),
    "results": (
        "Results",
        "Verification Results",
    ),
    "not_run": (
        "Not Run / Why",
    ),
    "manual_review": (
        "Manual Review Notes",
        "Manual Verification",
        "Review Notes",
    ),
    "evidence": (
        "Final Evidence",
        "Evidence",
        "Review Evidence",
    ),
    "generated_artifacts": (
        "Generated Or Updated Artifacts",
    ),
}


@dataclass(frozen=True)
class Finding:
    level: str
    message: str


@dataclass(frozen=True)
class InputReadiness:
    filename: str
    present: bool
    readable: bool
    state: str
    message: str
    text: str

    @property
    def substantive(self) -> bool:
        return self.present and self.readable and self.state == "has substantive content"


@dataclass(frozen=True)
class SectionReadiness:
    label: str
    present: bool
    state: str
    message: str

    @property
    def substantive(self) -> bool:
        return self.present and self.state == "substantive"

    @property
    def ready(self) -> bool:
        return self.substantive


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _review_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug / TEST_CONTRACT_REVIEW_FILENAME


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


def _artifact_path(slug: str, filename: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug / filename


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


def _non_placeholder_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if not _line_is_placeholder(line)]
    return "\n".join(lines).strip()


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


def _readiness_state(text: str) -> str:
    if not text.strip():
        return "empty"

    substantive_lines = [line for line in text.splitlines() if not _line_is_placeholder(line)]
    if not substantive_lines:
        return "TODO-only"

    sections = _section_bodies(text)
    if sections:
        filled_sections = 0
        for section in sections:
            if any(not _line_is_placeholder(line) for line in section):
                filled_sections += 1
        if filled_sections * 2 < len(sections):
            return "mostly unfilled"

    return "has substantive content"


def _read_input(
    slug: str,
    filename: str,
    captured: Mapping[str, CapturedDependency],
) -> InputReadiness:
    capture = captured[_artifact_path(slug, filename).as_posix()]
    state = capture.record.dependency_state
    if state == "missing":
        return InputReadiness(filename, present=False, readable=False, state="missing", message="missing", text="")
    if state == "not_regular":
        return InputReadiness(filename, present=True, readable=False, state="unreadable", message="not a regular file", text="")
    if state == "unreadable":
        return InputReadiness(filename, present=True, readable=False, state="unreadable", message="unreadable", text="")
    try:
        assert capture.content is not None
        text = capture.content.decode("utf-8")
    except Exception as exc:
        return InputReadiness(filename, present=True, readable=False, state="unreadable", message=f"unreadable: {exc}", text="")
    state = _readiness_state(text)
    return InputReadiness(filename, present=True, readable=True, state=state, message=state, text=text)


def _normalize_heading(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _sections_by_heading(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if match:
            current = _normalize_heading(match.group(1))
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _section_has_substantive_content(lines: list[str]) -> bool:
    return any(not _line_is_placeholder(line) for line in lines)


def _section_text(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def _find_section(sections: dict[str, list[str]], aliases: tuple[str, ...]) -> tuple[str, list[str]] | None:
    wanted = {_normalize_heading(alias) for alias in aliases}
    for heading, lines in sections.items():
        if heading in wanted:
            return heading, lines
    return None


def _section_readiness(input_readiness: InputReadiness, label: str, aliases: tuple[str, ...]) -> SectionReadiness:
    if not input_readiness.present:
        return SectionReadiness(label, present=False, state="missing", message="missing")
    if not input_readiness.readable:
        return SectionReadiness(label, present=False, state="unreadable", message="unreadable")

    sections = _sections_by_heading(input_readiness.text)
    found = _find_section(sections, aliases)
    if found is None:
        return SectionReadiness(label, present=False, state="missing", message="section missing")
    _, lines = found
    if not _section_text(lines):
        return SectionReadiness(label, present=True, state="empty", message="empty")
    if _section_has_substantive_content(lines):
        return SectionReadiness(label, present=True, state="substantive", message="substantive")
    return SectionReadiness(label, present=True, state="TODO-only", message="TODO-only")


def _all_test_contract_sections(test_contract: InputReadiness) -> dict[str, SectionReadiness]:
    labels = {
        "characterization": "characterization tests",
        "desired": "desired behavior tests",
        "regression": "regression tests",
        "negative": "negative and edge cases",
    }
    return {
        key: _section_readiness(test_contract, label, HEADING_ALIASES[key])
        for key, label in labels.items()
    }


def _acceptance_sections(acceptance: InputReadiness) -> dict[str, SectionReadiness]:
    return {
        "requirements": _section_readiness(acceptance, "requirements / acceptance criteria", HEADING_ALIASES["requirements"]),
        "protected": _section_readiness(acceptance, "protected behavior / non-goals", HEADING_ALIASES["protected"]),
    }


def _verification_sections(verification: InputReadiness) -> dict[str, SectionReadiness]:
    return {
        "commands": _section_readiness(verification, "commands and checks run", HEADING_ALIASES["commands"]),
        "results": _section_readiness(verification, "observed results", HEADING_ALIASES["results"]),
        "not_run": _section_readiness(verification, "test exceptions / not-run rationale", HEADING_ALIASES["not_run"]),
        "manual_review": _section_readiness(verification, "manual review checks", HEADING_ALIASES["manual_review"]),
    }


def _evidence_sections(evidence: InputReadiness) -> dict[str, SectionReadiness]:
    return {
        "evidence": _section_readiness(evidence, "evidence expectations", HEADING_ALIASES["evidence"]),
        "generated_artifacts": _section_readiness(evidence, "generated or updated artifacts", HEADING_ALIASES["generated_artifacts"]),
    }


def _verification_readiness(verification: InputReadiness) -> dict[str, str]:
    if not verification.present:
        return {"test_command": "missing", "manual_notes": "missing"}
    if not verification.readable:
        return {"test_command": "missing", "manual_notes": "missing"}

    sections = _sections_by_heading(verification.text)
    test_command = "missing"
    manual_notes = "missing"
    for heading, lines in sections.items():
        has_substantive = _section_has_substantive_content(lines)
        body = "\n".join(lines)
        if ("command" in heading or "test" in heading) and has_substantive:
            test_command = "present"
        if ("manual" in heading or "review" in heading or "note" in heading) and has_substantive:
            manual_notes = "present"
        if TEST_COMMAND_RE.search(body):
            test_command = "present"

    if not sections:
        substantive_text = "\n".join(line for line in verification.text.splitlines() if not _line_is_placeholder(line))
        if TEST_COMMAND_RE.search(substantive_text):
            test_command = "present"
        if substantive_text:
            manual_notes = "present"

    return {"test_command": test_command, "manual_notes": manual_notes}


def _has_test_exception_rationale(
    *,
    test_contract: InputReadiness,
) -> bool:
    text = _non_placeholder_text(test_contract.text if test_contract.readable else "")
    if not text or not NOT_RUN_RE.search(text):
        return False
    return bool(RATIONALE_RE.search(text))


def _has_unrationalized_not_run_text(*inputs: InputReadiness) -> bool:
    text = "\n".join(_non_placeholder_text(item.text) for item in inputs if item.present and item.readable)
    return bool(text and NOT_RUN_RE.search(text) and not RATIONALE_RE.search(text))


def _format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _findings(
    *,
    signals: dict[str, Any],
    acceptance: InputReadiness,
    test_contract: InputReadiness,
    verification_input: InputReadiness,
    evidence: InputReadiness,
    acceptance_sections: dict[str, SectionReadiness],
    test_sections: dict[str, SectionReadiness],
    evidence_sections: dict[str, SectionReadiness],
    preflight: InputReadiness,
) -> list[Finding]:
    findings: list[Finding] = []

    requirements = acceptance_sections["requirements"]
    protected = acceptance_sections["protected"]
    if not acceptance.present:
        findings.append(Finding("blocker", "requirements / acceptance criteria are missing."))
    elif not acceptance.readable:
        findings.append(Finding("blocker", "requirements / acceptance criteria are unreadable."))
    elif not requirements.ready:
        findings.append(Finding("blocker", f"requirements / acceptance criteria are {requirements.message}."))

    if acceptance.present and acceptance.readable and not protected.ready:
        findings.append(Finding("warning", f"protected behavior / non-goals are {protected.message}."))

    contract_has_substantive_section = any(section.ready for section in test_sections.values())
    if acceptance.present and acceptance.readable and not requirements.ready and not contract_has_substantive_section:
        findings.append(Finding("blocker", "no usable acceptance or test-contract content is available."))

    exception_rationale = _has_test_exception_rationale(
        test_contract=test_contract,
    )
    desired = test_sections["desired"]
    if not desired.ready and not exception_rationale:
        findings.append(Finding("blocker", f"desired behavior tests are {desired.message}."))

    for key, message in (
        ("characterization", "characterization tests"),
        ("regression", "regression tests"),
        ("negative", "negative / edge-case tests"),
    ):
        section = test_sections[key]
        if not section.ready:
            findings.append(Finding("warning", f"{message} are {section.message}."))

    if _has_unrationalized_not_run_text(test_contract, verification_input, evidence):
        findings.append(Finding("warning", "test exceptions / not-run rationale is missing."))

    if not evidence.present:
        findings.append(Finding("warning", "evidence expectations are missing."))
    elif not evidence.readable:
        findings.append(Finding("warning", "evidence expectations are unreadable."))
    elif not evidence_sections["evidence"].ready:
        findings.append(Finding("warning", f"evidence expectations are {evidence_sections['evidence'].message}."))

    if not signals.get("detected_test_frameworks"):
        findings.append(
            Finding("warning", "no recognized test-framework signal detected.")
        )
    if not signals.get("detected_ci"):
        findings.append(Finding("warning", "no CI detected."))

    preflight_state = "present" if preflight.present and preflight.readable else "missing"
    findings.append(Finding("info", f"preflight report is {preflight_state}."))
    findings.append(Finding("info", "no tests were run by this command."))
    findings.append(
        Finding(
            "info",
            "test-contract review is deterministic and shallow; it does not generate tests, run tests, call AI models, enforce packs, inspect actual test files deeply, prove correctness, validate compliance, or scan security.",
        )
    )
    return findings


def _recommended_actions(findings: list[Finding]) -> list[str]:
    blockers = [finding for finding in findings if finding.level == "blocker"]
    warnings = [finding for finding in findings if finding.level == "warning"]
    actions: list[str] = []
    if blockers:
        actions.append("Resolve blocker findings before asking a coding agent to implement the task.")
    if any("requirements / acceptance" in finding.message for finding in warnings + blockers):
        actions.append("Add concrete requirements and acceptance criteria for observable completion.")
    if any("protected behavior" in finding.message for finding in warnings):
        actions.append("Record protected behavior and non-goals before implementation.")
    if any("desired behavior" in finding.message for finding in blockers):
        actions.append("Add desired behavior tests, or record why automated tests are not practical and what review path replaces them.")
    if any("characterization" in finding.message for finding in warnings):
        actions.append("Add characterization checks for important existing behavior.")
    if any("regression" in finding.message for finding in warnings):
        actions.append("Add regression test intent for behavior that must not break again.")
    if any("negative" in finding.message or "edge" in finding.message for finding in warnings):
        actions.append("Add negative and edge case test intent.")
    if any("not-run" in finding.message for finding in warnings):
        actions.append("Record the check as not run in verification.md and explain why.")
    if any("evidence expectations" in finding.message for finding in warnings):
        actions.append("Record the expected implementation and verification evidence in evidence.md.")
    if any("no test framework" in finding.message for finding in warnings):
        actions.append("Confirm the intended test framework or manual test path.")
    if not actions:
        actions.append("Review the expected checks and proceed with bounded implementation planning.")
    return actions


def _presence_line(item: InputReadiness) -> str:
    present = "yes" if item.present else "no"
    readable = "yes" if item.readable else "no"
    return f"- {item.filename}: present={present}; readable={readable}; readiness={item.message}"


def _section_line(section: SectionReadiness) -> str:
    present = "yes" if section.present else "no"
    return f"- {section.label}: present={present}; readiness={section.message}"


def _render_report(
    *,
    slug: str,
    generated_at: str,
    signals: dict[str, Any],
    acceptance: InputReadiness,
    test_contract: InputReadiness,
    verification_input: InputReadiness,
    evidence: InputReadiness,
    preflight: InputReadiness,
    acceptance_sections: dict[str, SectionReadiness],
    test_sections: dict[str, SectionReadiness],
    verification_sections: dict[str, SectionReadiness],
    evidence_sections: dict[str, SectionReadiness],
    verification: dict[str, str],
    findings: list[Finding],
) -> str:
    counts = {
        "blocker": sum(1 for finding in findings if finding.level == "blocker"),
        "warning": sum(1 for finding in findings if finding.level == "warning"),
        "info": sum(1 for finding in findings if finding.level == "info"),
    }
    lines = [
        "# Test-Contract Readiness Review",
        "",
        f"Task slug: `{slug}`",
        f"Generated: {generated_at}",
        "",
        "## Summary",
        "",
        "- Review question: Are the expected checks clear enough for implementation and review?",
        f"- Answer: {'No; resolve blocker findings before implementation.' if counts['blocker'] else 'No blocker findings; review warnings before implementation.'}",
        f"- Blockers: {counts['blocker']}",
        f"- Warnings: {counts['warning']}",
        f"- Info: {counts['info']}",
        "",
        "## Requirements / Acceptance Readiness",
        "",
        _presence_line(acceptance),
        _section_line(acceptance_sections["requirements"]),
        _section_line(acceptance_sections["protected"]),
        "",
        "## Test Intent Readiness",
        "",
        _presence_line(test_contract),
        _section_line(test_sections["characterization"]),
        _section_line(test_sections["desired"]),
    ]
    lines.extend(
        [
            "",
            "## Regression / Negative Coverage",
            "",
            _section_line(test_sections["regression"]),
            _section_line(test_sections["negative"]),
            "",
            "## Verification Record",
            "",
            _presence_line(verification_input),
            _section_line(verification_sections["commands"]),
            _section_line(verification_sections["results"]),
            _section_line(verification_sections["not_run"]),
            _section_line(verification_sections["manual_review"]),
            f"- Test command marker: {verification['test_command']}",
            f"- Manual verification notes: {verification['manual_notes']}",
            "",
            "## Evidence Expectations",
            "",
            _presence_line(evidence),
            _section_line(evidence_sections["evidence"]),
            _section_line(evidence_sections["generated_artifacts"]),
            "",
            "## Repository Signals",
            "",
            f"- Detected test frameworks: {_format_list(signals.get('detected_test_frameworks', []))}",
            f"- Detected package managers: {_format_list(signals.get('detected_package_managers', []))}",
            f"- Detected CI: {_format_list(signals.get('detected_ci', []))}",
            f"- Preflight report: {'present' if preflight.present and preflight.readable else 'missing'}",
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
            "This report checks whether task artifacts define clear expected checks before implementation. It uses deterministic, shallow Markdown marker checks only. It does not generate tests, run tests, call AI models, enforce packs, inspect actual test files deeply, prove correctness, validate compliance, scan security, execute packs, or determine approval.",
        ]
    )

    return "\n".join(lines) + "\n"


def _summarize(findings: list[Finding]) -> str:
    blockers = sum(1 for finding in findings if finding.level == "blocker")
    warnings = sum(1 for finding in findings if finding.level == "warning")
    infos = sum(1 for finding in findings if finding.level == "info")
    return f"summary: {blockers} blocker(s), {warnings} warning(s), {infos} info"


def run_test_contract_review(root: Path, task_slug: str, *, dry_run: bool = False, force: bool = False) -> tuple[int, list[str]]:
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

    review_path = _review_path(task_slug)
    try:
        review_target = resolve_managed_output_under_root(root, review_path)
        entries = _load_manifest_entries(root)
    except Exception as exc:
        return 1, [f"Could not read harness manifest or resolve test-contract review path: {exc}"]

    path_text = review_path.as_posix()
    entry = entries.get(path_text)
    if review_target.exists() and entry is None:
        return 1, [
            f"unmanaged existing file {path_text}",
            "refusing to overwrite unmanaged test-contract review report",
        ]

    if review_target.exists() and entry is not None:
        expected_hash = entry.get("sha256")
        hash_algorithm = entry.get("hash_algorithm")
        hash_clean = hash_algorithm == "sha256" and expected_hash and sha256_file(review_target) == expected_hash
        if not hash_clean and not force:
            return 1, [
                f"hash drift detected for {path_text}",
                "refusing to overwrite manifest-managed test-contract review report without --force",
            ]

    try:
        expected = lineage_definition_for_task(
            "test_contract_review",
            task_slug,
        )
        captures = capture_dependencies(root, expected)
        dependency_snapshot = provenance_dependencies(captures)
        captured_by_path = {
            item.record.dependency_path: item for item in captures
        }
        observation_snapshot = snapshot_repository_observations(root, expected)
    except Exception as exc:
        return 1, [f"Could not snapshot test-contract provenance inputs: {exc}"]

    acceptance = _read_input(task_slug, "acceptance.md", captured_by_path)
    test_contract = _read_input(
        task_slug,
        "test-contract.md",
        captured_by_path,
    )
    verification_input = _read_input(
        task_slug,
        "verification.md",
        captured_by_path,
    )
    evidence = _read_input(task_slug, "evidence.md", captured_by_path)
    preflight = _read_input(task_slug, "preflight.md", captured_by_path)
    signals = merge_documented_test_frameworks(
        detect_project_signals(root),
        test_contract.text,
        verification_input.text,
    )
    acceptance_sections = _acceptance_sections(acceptance)
    test_sections = _all_test_contract_sections(test_contract)
    verification_sections = _verification_sections(verification_input)
    evidence_sections = _evidence_sections(evidence)
    verification = _verification_readiness(verification_input)
    findings = _findings(
        signals=signals,
        acceptance=acceptance,
        test_contract=test_contract,
        verification_input=verification_input,
        evidence=evidence,
        acceptance_sections=acceptance_sections,
        test_sections=test_sections,
        evidence_sections=evidence_sections,
        preflight=preflight,
    )
    generated_at = _timestamp()
    content = _render_report(
        slug=task_slug,
        generated_at=generated_at,
        signals=signals,
        acceptance=acceptance,
        test_contract=test_contract,
        verification_input=verification_input,
        evidence=evidence,
        preflight=preflight,
        acceptance_sections=acceptance_sections,
        test_sections=test_sections,
        verification_sections=verification_sections,
        evidence_sections=evidence_sections,
        verification=verification,
        findings=findings,
    )
    try:
        stable_observations = snapshot_repository_observations(root, expected)
    except Exception as exc:
        return 1, [
            f"Could not confirm test-contract repository observations: {exc}"
        ]
    if stable_observations != observation_snapshot:
        return 1, [
            "Repository observations changed during test-contract review; "
            "no files written."
        ]

    messages = [f"test-contract review report: {path_text}", _summarize(findings)]
    if dry_run:
        action = "would refresh" if review_target.exists() else "would create"
        messages.append(f"{action} file {path_text}")
        messages.append("dry run; no files written")
        return 0, messages

    content_bytes = content.encode("utf-8")
    existed_before = review_target.exists()
    try:
        output_will_change = (
            not existed_before or review_target.read_bytes() != content_bytes
        )
        if output_will_change:
            manifest_path = resolve_under_root(root, ".harness/manifest.json")
            current_manifest = load_manifest_model(manifest_path)
            invalidated = remove_provenance_records(
                current_manifest,
                [path_text],
                generated_at=generated_at,
            )
            if invalidated != current_manifest:
                persist_manifest_model(root, invalidated)

        persisted = persist_exact_bytes(review_target, content_bytes)
        provenance = build_provenance_record(
            expected,
            output_sha256=persisted.sha256,
            dependencies=dependency_snapshot,
            repository_observations=observation_snapshot,
        )
        managed = build_managed_file_record(root, path_text)
        manifest_path = resolve_under_root(root, ".harness/manifest.json")
        current_manifest = load_manifest_model(manifest_path)
        updated_manifest = replace_managed_and_provenance_records(
            current_manifest,
            [managed],
            [provenance],
            generated_at=generated_at,
            upgrade_to_v2=True,
        )
        manifest_changed = updated_manifest != current_manifest
        if manifest_changed:
            persist_manifest_model(root, updated_manifest)
    except Exception as exc:
        return 1, [
            f"Could not persist test-contract review and provenance: {exc}"
        ]

    if persisted.changed:
        action = "refresh" if existed_before else "create"
        messages.append(f"{action} file {path_text}")
    else:
        messages.append(f"skip unchanged file {path_text}")
    if manifest_changed:
        messages.append("refreshed manifest .harness/manifest.json")
        messages.append("root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified")
    else:
        messages.append("skip existing manifest .harness/manifest.json")
    return 0, messages
