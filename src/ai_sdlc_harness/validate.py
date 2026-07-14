"""Implementation of ``ai-sdlc validate``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import (
    AGENT_WORKSET_FILENAME,
    EVIDENCE_REPORT_FILENAME,
    PREFLIGHT_FILENAME,
    REQUIREMENTS_FILENAME,
    SPEC_FILENAME,
    TASK_ARTIFACT_FILENAMES,
    TASK_GENERATED_DIRNAME,
    TASK_SLUG_PATTERN,
    TEST_CONTRACT_REVIEW_FILENAME,
    VALIDATION_REPORT_FILENAME,
)
from .detect import detect_project_signals
from .files import read_text, resolve_under_root, write_text
from .manifest import load_manifest, sha256_file
from .requirements import RequirementsReadResult, read_requirements


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
FINDING_LINE_RE = re.compile(r"^\s*-?\s*(?:[^:]{1,80}:\s+)?(blocker|warning|info):\s+(.+?)\s*$", re.IGNORECASE)
TEST_COMMAND_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|coverage|npm\s+test|npm\s+run\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|dotnet\s+test|mvn\s+test|gradle\s+test)\b",
    re.IGNORECASE,
)
TEST_RESULT_RE = re.compile(
    r"\b(test result|tests? passed|tests? failed|passed in|failed in|exit code|return code|returncode|successfully ran)\b",
    re.IGNORECASE,
)

DISCLAIMER = (
    "This report summarizes task workflow validation signals. It does not run tests, inspect target code deeply, "
    "prove correctness, prove security, validate compliance, scan for security issues, enforce packs, execute packs, "
    "or call AI models."
)

PRIOR_FINDING_REPORTS = {PREFLIGHT_FILENAME, TEST_CONTRACT_REVIEW_FILENAME, EVIDENCE_REPORT_FILENAME}


@dataclass(frozen=True)
class ArtifactStatus:
    filename: str
    relative_path: PurePosixPath
    present: bool
    readable: bool
    state: str
    message: str
    text: str

    @property
    def substantive(self) -> bool:
        return self.present and self.readable and self.state == "substantive"


@dataclass(frozen=True)
class ReportFinding:
    source: str
    level: str
    message: str


@dataclass(frozen=True)
class ManifestTaskEntry:
    path: str
    status: str
    hash_status: str


@dataclass(frozen=True)
class Finding:
    level: str
    message: str


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


def _artifact_path(slug: str, filename: str) -> PurePosixPath:
    return _task_dir_path(slug) / filename


def _workset_path(slug: str) -> PurePosixPath:
    return _task_dir_path(slug) / TASK_GENERATED_DIRNAME / AGENT_WORKSET_FILENAME


def _report_path(slug: str) -> PurePosixPath:
    return _task_dir_path(slug) / VALIDATION_REPORT_FILENAME


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


def _write_manifest_for_validation_report(root: Path, report_path: PurePosixPath, report_target: Path) -> None:
    manifest_path = resolve_under_root(root, ".harness/manifest.json")
    manifest = load_manifest(manifest_path)
    entries = manifest.get("managed_files")
    if not isinstance(entries, list):
        raise ValueError("manifest managed_files must be a list")

    path_text = report_path.as_posix()
    updated_entry = {
        "path": path_text,
        "protected": True,
        "hash_algorithm": "sha256",
        "sha256": sha256_file(report_target),
    }
    replaced = False
    updated_entries: list[Any] = []
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("path")) == path_text:
            updated_entries.append(updated_entry)
            replaced = True
        else:
            updated_entries.append(entry)
    if not replaced:
        updated_entries.append(updated_entry)

    manifest["generated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest["managed_files"] = updated_entries
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


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


def _readiness_state(text: str) -> str:
    if not text.strip():
        return "empty"
    if not any(not _line_is_placeholder(line) for line in text.splitlines()):
        return "TODO-only"
    return "substantive"


def _read_artifact(root: Path, relative_path: PurePosixPath, filename: str) -> ArtifactStatus:
    target = resolve_under_root(root, relative_path)
    if not target.exists():
        return ArtifactStatus(filename, relative_path, present=False, readable=False, state="missing", message="missing", text="")
    if not target.is_file():
        return ArtifactStatus(
            filename,
            relative_path,
            present=True,
            readable=False,
            state="unreadable",
            message="not a regular file",
            text="",
        )
    try:
        text = read_text(target)
    except Exception as exc:
        return ArtifactStatus(
            filename,
            relative_path,
            present=True,
            readable=False,
            state="unreadable",
            message=f"unreadable: {exc}",
            text="",
        )
    state = _readiness_state(text)
    return ArtifactStatus(filename, relative_path, present=True, readable=True, state=state, message=state, text=text)


def _read_task_artifacts(root: Path, slug: str) -> list[ArtifactStatus]:
    return [_read_artifact(root, _artifact_path(slug, filename), filename) for filename in TASK_ARTIFACT_FILENAMES]


def _read_generated_reports(root: Path, slug: str) -> list[ArtifactStatus]:
    reports = (
        (PREFLIGHT_FILENAME, _artifact_path(slug, PREFLIGHT_FILENAME)),
        (SPEC_FILENAME, _artifact_path(slug, SPEC_FILENAME)),
        (REQUIREMENTS_FILENAME, _artifact_path(slug, REQUIREMENTS_FILENAME)),
        (TEST_CONTRACT_REVIEW_FILENAME, _artifact_path(slug, TEST_CONTRACT_REVIEW_FILENAME)),
        (f"{TASK_GENERATED_DIRNAME}/{AGENT_WORKSET_FILENAME}", _workset_path(slug)),
        (EVIDENCE_REPORT_FILENAME, _artifact_path(slug, EVIDENCE_REPORT_FILENAME)),
    )
    return [_read_artifact(root, relative, label) for label, relative in reports]


def _verification_command_status(verification: ArtifactStatus) -> str:
    if not verification.readable:
        return "missing"
    return "present" if TEST_COMMAND_RE.search(verification.text) else "missing"


def _test_result_status(evidence: ArtifactStatus, verification: ArtifactStatus) -> str:
    text = "\n".join(item.text for item in (evidence, verification) if item.readable)
    return "present" if TEST_RESULT_RE.search(text) else "missing"


def _todo_sources(task_artifacts: list[ArtifactStatus]) -> list[str]:
    sources: list[str] = []
    for item in task_artifacts:
        if item.filename == VALIDATION_REPORT_FILENAME or not item.readable:
            continue
        if TODO_RE.search(item.text):
            sources.append(item.filename)
    return sources


def _report_findings(generated_reports: list[ArtifactStatus]) -> list[ReportFinding]:
    findings: list[ReportFinding] = []
    seen: set[tuple[str, str]] = set()
    for item in generated_reports:
        if item.filename not in PRIOR_FINDING_REPORTS:
            continue
        if not item.present or not item.readable:
            continue
        for line in item.text.splitlines():
            match = FINDING_LINE_RE.match(line)
            if not match:
                continue
            level = match.group(1).lower()
            message = match.group(2).strip()
            key = (level, message.lower())
            if key in seen:
                continue
            seen.add(key)
            findings.append(ReportFinding(item.filename, level, message))
            if len(findings) >= 30:
                return findings
    return findings


def _manifest_task_entries(root: Path, slug: str, entries: dict[str, dict[str, Any]]) -> list[ManifestTaskEntry]:
    prefix = f".harness/tasks/{slug}/"
    task_entries: list[ManifestTaskEntry] = []
    for path_text in sorted(path for path in entries if path.startswith(prefix)):
        try:
            target = resolve_under_root(root, path_text)
        except Exception as exc:
            task_entries.append(ManifestTaskEntry(path_text, f"unsafe path: {exc}", "not checked"))
            continue
        if not target.is_file():
            task_entries.append(ManifestTaskEntry(path_text, "missing", "missing"))
            continue
        try:
            read_text(target)
        except Exception as exc:
            task_entries.append(ManifestTaskEntry(path_text, f"unreadable: {exc}", "not checked"))
            continue
        entry = entries[path_text]
        if entry.get("hash_algorithm") == "sha256" and entry.get("sha256"):
            hash_status = "clean" if sha256_file(target) == entry["sha256"] else "hash drift"
        elif entry.get("hash_algorithm") == "sha256":
            hash_status = "missing hash"
        else:
            hash_status = "not hashed"
        task_entries.append(ManifestTaskEntry(path_text, "present and readable", hash_status))
    return task_entries


def _workflow_checks(
    *,
    task_artifacts: list[ArtifactStatus],
    generated_reports: list[ArtifactStatus],
    manifest_entries: list[ManifestTaskEntry],
    report_path_text: str,
) -> dict[str, str]:
    by_name = {item.filename: item for item in generated_reports}
    task_started = "yes" if any(item.present for item in task_artifacts) else "no"
    manifest_relevant = [entry for entry in manifest_entries if entry.path != report_path_text]
    manifest_clean = "clean"
    if any(entry.status in {"missing"} or entry.hash_status in {"hash drift", "missing hash"} for entry in manifest_relevant):
        manifest_clean = "issues detected"
    elif not manifest_relevant:
        manifest_clean = "no task manifest entries detected"
    return {
        "task was started": task_started,
        "preflight was generated": "yes" if by_name[PREFLIGHT_FILENAME].present and by_name[PREFLIGHT_FILENAME].readable else "no",
        "spec was generated": "yes" if by_name[SPEC_FILENAME].present and by_name[SPEC_FILENAME].readable else "no",
        "requirements.yaml was generated": "yes"
        if by_name[REQUIREMENTS_FILENAME].present and by_name[REQUIREMENTS_FILENAME].readable
        else "no",
        "test-contract review was generated": "yes"
        if by_name[TEST_CONTRACT_REVIEW_FILENAME].present and by_name[TEST_CONTRACT_REVIEW_FILENAME].readable
        else "no",
        "workset was generated": "yes"
        if by_name[f"{TASK_GENERATED_DIRNAME}/{AGENT_WORKSET_FILENAME}"].present
        and by_name[f"{TASK_GENERATED_DIRNAME}/{AGENT_WORKSET_FILENAME}"].readable
        else "no",
        "evidence report was generated": "yes"
        if by_name[EVIDENCE_REPORT_FILENAME].present and by_name[EVIDENCE_REPORT_FILENAME].readable
        else "no",
        "manifest verification signals": manifest_clean,
    }


def _key_workflow_outputs(generated_reports: list[ArtifactStatus]) -> list[ArtifactStatus]:
    return [item for item in generated_reports if item.filename not in {SPEC_FILENAME, REQUIREMENTS_FILENAME}]


def _workflow_outputs_ready(generated_reports: list[ArtifactStatus]) -> bool:
    return all(item.present and item.readable for item in _key_workflow_outputs(generated_reports))


def _review_readiness(findings: list[Finding], generated_reports: list[ArtifactStatus]) -> tuple[str, str]:
    blockers = [finding for finding in findings if finding.level == "blocker"]
    warnings = [finding for finding in findings if finding.level == "warning"]
    missing_outputs = [item.filename for item in _key_workflow_outputs(generated_reports) if not (item.present and item.readable)]
    if blockers:
        return "No", "blocker findings must be resolved before human review."
    if missing_outputs:
        return "No", f"key workflow outputs are missing or unreadable: {', '.join(missing_outputs)}."
    if warnings:
        return "Caution", "no blockers were found, but warning findings remain for human review."
    return "Yes", "recorded workflow signals show no blocker or warning findings."


def _build_findings(
    *,
    task_artifacts: list[ArtifactStatus],
    generated_reports: list[ArtifactStatus],
    evidence: ArtifactStatus,
    verification: ArtifactStatus,
    verification_command: str,
    test_result_evidence: str,
    todo_sources: list[str],
    report_findings: list[ReportFinding],
    manifest_entries: list[ManifestTaskEntry],
    structured_requirements: RequirementsReadResult,
    signals: dict[str, Any],
    report_path_text: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for item in task_artifacts:
        if not item.present:
            findings.append(Finding("blocker", f"{item.filename} is missing."))
        elif not item.readable:
            findings.append(Finding("blocker", f"{item.filename} is unreadable."))
        elif item.state in {"empty", "TODO-only"}:
            findings.append(Finding("blocker", f"{item.filename} is {item.state}."))

    by_name = {item.filename: item for item in task_artifacts}
    acceptance = by_name["acceptance.md"]
    test_contract = by_name["test-contract.md"]
    if not acceptance.substantive and not test_contract.substantive:
        findings.append(Finding("blocker", "no usable acceptance criteria and no usable test-contract content are available."))
    if not evidence.substantive and not verification.substantive:
        findings.append(Finding("blocker", "no usable evidence and no usable verification content are available."))

    for item in generated_reports:
        if not item.present:
            findings.append(Finding("warning", f"{item.filename} is missing."))
        elif not item.readable:
            findings.append(Finding("warning", f"{item.filename} is unreadable."))

    if todo_sources:
        findings.append(Finding("warning", f"unresolved TODOs remain in: {', '.join(todo_sources)}."))
    if verification_command == "missing":
        findings.append(Finding("warning", "no verification command recorded."))
    if test_result_evidence == "missing":
        findings.append(Finding("warning", "no test result evidence recorded."))
    if not signals.get("detected_ci"):
        findings.append(Finding("warning", "no CI detected."))
    if not signals.get("detected_test_frameworks"):
        findings.append(Finding("warning", "no test framework detected."))

    for report_finding in report_findings:
        findings.append(Finding(report_finding.level, f"{report_finding.source} reported {report_finding.level}: {report_finding.message}"))

    if structured_requirements.present and structured_requirements.readable and not structured_requirements.schema_valid:
        for problem in structured_requirements.problems:
            findings.append(Finding("blocker", f"requirements.yaml schema invalid: {problem}"))

    for entry in manifest_entries:
        if entry.path == report_path_text:
            continue
        if entry.status == "missing":
            findings.append(Finding("blocker", f"manifest-managed task artifact is missing: {entry.path}."))
        elif entry.status.startswith("unreadable"):
            findings.append(Finding("blocker", f"manifest-managed task artifact is unreadable: {entry.path}."))
        elif entry.hash_status in {"hash drift", "missing hash"}:
            findings.append(Finding("blocker", f"manifest-managed task artifact has {entry.hash_status}: {entry.path}."))

    findings.append(Finding("info", "validation generated deterministically."))
    findings.append(Finding("info", "no optional packs selected."))
    findings.append(Finding("info", "validate does not run tests."))
    findings.append(Finding("info", "validate does not inspect target code deeply."))
    findings.append(Finding("info", "validate does not prove correctness, security, or compliance."))
    findings.append(Finding("info", "validate does not call AI models."))
    findings.append(Finding("info", "validate does not enforce packs."))
    return findings


def _summarize(findings: list[Finding]) -> str:
    blockers = sum(1 for finding in findings if finding.level == "blocker")
    warnings = sum(1 for finding in findings if finding.level == "warning")
    infos = sum(1 for finding in findings if finding.level == "info")
    return f"summary: {blockers} blocker(s), {warnings} warning(s), {infos} info"


def _recommended_actions(findings: list[Finding]) -> list[str]:
    messages = [finding.message for finding in findings if finding.level in {"blocker", "warning"}]
    actions: list[str] = []
    if any("acceptance" in message for message in messages):
        actions.append("Add substantive acceptance criteria for the behavior a human should review.")
    if any("test-contract" in message for message in messages):
        actions.append("Add substantive test-contract content for expected characterization, regression, edge, and desired-behavior checks.")
    if any("evidence" in message for message in messages):
        actions.append("Record implementation evidence, verification results, and unresolved risks in evidence.md.")
    if any("verification" in message or "test result" in message for message in messages):
        actions.append("Record verification commands and results in verification.md or evidence.md.")
    if any("manifest-managed" in message for message in messages):
        actions.append("Run ai-sdlc verify and resolve protected task artifact drift before final human review.")
    if any("TODO" in message for message in messages):
        actions.append("Resolve or explicitly carry forward unresolved TODOs before relying on these findings.")
    if not actions:
        actions.append("Review the validation findings and source task artifacts before accepting the work.")
    return actions[:5]


def _render_status_line(item: ArtifactStatus) -> str:
    present = "yes" if item.present else "no"
    readable = "yes" if item.readable else "no"
    return f"- `{item.filename}`: present={present}; readable={readable}; state={item.message}"


def _render_plain_status_line(item: ArtifactStatus) -> str:
    present = "yes" if item.present else "no"
    readable = "yes" if item.readable else "no"
    return f"- {item.filename}: present={present}; readable={readable}; state={item.message}"


def _render_structured_requirements_status(result: RequirementsReadResult) -> list[str]:
    lines = ["## Structured Requirements", ""]
    if not result.present:
        lines.append("- requirements.yaml: present=no; readable=no; schema=missing")
        lines.append("- Requirement count: 0")
        lines.append("- Finding count: 0")
        return lines
    readable = "yes" if result.readable else "no"
    if not result.readable:
        lines.append(f"- requirements.yaml: present=yes; readable={readable}; schema=not checked; message={result.message}")
        lines.append("- Requirement count: 0")
        lines.append("- Finding count: 0")
        return lines
    schema = "valid" if result.schema_valid else "invalid"
    lines.append(f"- requirements.yaml: present=yes; readable=yes; schema={schema}")
    lines.append(f"- Requirement count: {result.requirement_count}")
    lines.append(f"- Finding count: {result.finding_count}")
    if result.problems:
        lines.append("- Schema problems:")
        for problem in result.problems:
            lines.append(f"  - {problem}")
    return lines


def _render_report(
    *,
    slug: str,
    generated_at: str,
    task_artifacts: list[ArtifactStatus],
    generated_reports: list[ArtifactStatus],
    evidence: ArtifactStatus,
    verification: ArtifactStatus,
    verification_command: str,
    test_result_evidence: str,
    todo_sources: list[str],
    report_findings: list[ReportFinding],
    manifest_entries: list[ManifestTaskEntry],
    workflow_checks: dict[str, str],
    structured_requirements: RequirementsReadResult,
    findings: list[Finding],
) -> str:
    counts = {
        "blocker": sum(1 for finding in findings if finding.level == "blocker"),
        "warning": sum(1 for finding in findings if finding.level == "warning"),
        "info": sum(1 for finding in findings if finding.level == "info"),
    }
    by_name = {item.filename: item for item in task_artifacts}
    acceptance = by_name["acceptance.md"]
    test_contract = by_name["test-contract.md"]
    answer, answer_reason = _review_readiness(findings, generated_reports)
    missing_or_unreadable = [item for item in _key_workflow_outputs(generated_reports) if not (item.present and item.readable)]
    lines = [
        "# Validation Report",
        "",
        "## Summary",
        "",
        f"- Task slug: `{slug}`",
        f"- Generated: {generated_at}",
        "- Review question: Is this task review-ready based on recorded workflow signals?",
        f"- Review-readiness answer: {answer}",
        f"- Blockers: {counts['blocker']}",
        f"- Warnings: {counts['warning']}",
        f"- Info: {counts['info']}",
        "",
        "## Review Readiness",
        "",
        f"- Answer: {answer}",
        f"- Reason: {answer_reason}",
        f"- Key workflow outputs present/readable: {'yes' if _workflow_outputs_ready(generated_reports) else 'no'}",
        "",
        "## Workflow Signals",
        "",
    ]
    for label, value in workflow_checks.items():
        lines.append(f"- {label}: {value}")
    lines.append("")
    lines.extend(_render_status_line(item) for item in generated_reports)

    lines.extend([""])
    lines.extend(_render_structured_requirements_status(structured_requirements))

    lines.extend(["", "## Task Artifact Readiness", ""])
    lines.extend(_render_status_line(item) for item in task_artifacts)

    lines.extend(
        [
            "",
            "Task content signals:",
            "",
            f"- Unresolved TODOs: {'present in ' + ', '.join(todo_sources) if todo_sources else 'missing'}",
            f"- Acceptance criteria: {'substantive' if acceptance.substantive else 'missing'}",
            f"- Test contract: {'substantive' if test_contract.substantive else 'missing'}",
            f"- Verification evidence: {'substantive' if verification.substantive else 'missing'}",
            f"- Implementation evidence: {'substantive' if evidence.substantive else 'missing'}",
            f"- Verification command: {verification_command}",
            f"- Test result evidence: {test_result_evidence}",
            "",
            "## Prior Report Findings",
            "",
            "Prior reports parsed for findings: `preflight.md`, `test-contract-review.md`, `evidence-report.md`.",
            "`generated/agent-workset.md` is treated as a supporting artifact only.",
            "",
        ]
    )
    if report_findings:
        for finding in report_findings:
            lines.append(f"- `{finding.source}`: {finding.level}: {finding.message}")
    else:
        lines.append("- none detected in readable prior reports")

    lines.extend(["", "## Manifest / Integrity Signals", ""])
    if manifest_entries:
        for entry in manifest_entries:
            lines.append(f"- `{entry.path}`: status={entry.status}; hash={entry.hash_status}")
    else:
        lines.append("- none recorded for this task")

    lines.extend(["", "## Missing Or Stale Artifacts", ""])
    if missing_or_unreadable:
        for item in missing_or_unreadable:
            lines.append(_render_plain_status_line(item))
    else:
        lines.append("- no missing or unreadable key workflow outputs detected")
    lines.append("- stale artifact detection is not performed by this validate report.")

    lines.extend(["", "## Findings", ""])
    for finding in findings:
        lines.append(f"- {finding.level}: {finding.message}")

    lines.extend(["", "## Recommended Next Actions", ""])
    for action in _recommended_actions(findings):
        lines.append(f"- {action}")

    lines.extend(
        [
            "",
            "## Human Review Checklist",
            "",
            "- [ ] Review task artifacts for substantive task intent.",
            "- [ ] Review generated report presence and copied blocker/warning signals.",
            "- [ ] Review evidence.md and verification.md for recorded implementation and verification evidence.",
            "- [ ] Confirm unresolved TODOs, risks, or follow-up work are acceptable for human review.",
            "- [ ] Confirm manifest-managed artifact drift has been resolved or intentionally handled.",
            "",
            "## Disclaimer",
            "",
            DISCLAIMER,
            "",
        ]
    )
    return "\n".join(lines)


def run_validate(root: Path, task_slug: str, *, dry_run: bool = False, force: bool = False) -> tuple[int, list[str]]:
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

    validation_report_path = _report_path(task_slug)
    try:
        report_target = resolve_under_root(root, validation_report_path)
        entries = _load_manifest_entries(root)
    except Exception as exc:
        return 1, [f"Could not read harness manifest or resolve validation report path: {exc}"]

    path_text = validation_report_path.as_posix()
    entry = entries.get(path_text)
    if report_target.exists() and entry is None:
        return 1, [
            f"unmanaged existing file {path_text}",
            "refusing to overwrite unmanaged validation report",
        ]

    if report_target.exists() and entry is not None:
        expected_hash = entry.get("sha256")
        hash_algorithm = entry.get("hash_algorithm")
        hash_clean = hash_algorithm == "sha256" and expected_hash and sha256_file(report_target) == expected_hash
        if not hash_clean and not force:
            return 1, [
                f"hash drift detected for {path_text}",
                "refusing to overwrite manifest-managed validation report without --force",
            ]

    task_artifacts = _read_task_artifacts(root, task_slug)
    generated_reports = _read_generated_reports(root, task_slug)
    by_name = {item.filename: item for item in task_artifacts}
    evidence = by_name["evidence.md"]
    verification = by_name["verification.md"]
    verification_command = _verification_command_status(verification)
    test_result_evidence = _test_result_status(evidence, verification)
    todo_sources = _todo_sources(task_artifacts)
    report_findings = _report_findings(generated_reports)
    manifest_entries = _manifest_task_entries(root, task_slug, entries)
    report_manifest_entries = [entry for entry in manifest_entries if entry.path != path_text]
    signals = detect_project_signals(root)
    structured_requirements = read_requirements(root, task_slug)
    workflow_checks = _workflow_checks(
        task_artifacts=task_artifacts,
        generated_reports=generated_reports,
        manifest_entries=report_manifest_entries,
        report_path_text=path_text,
    )
    findings = _build_findings(
        task_artifacts=task_artifacts,
        generated_reports=generated_reports,
        evidence=evidence,
        verification=verification,
        verification_command=verification_command,
        test_result_evidence=test_result_evidence,
        todo_sources=todo_sources,
        report_findings=report_findings,
        manifest_entries=report_manifest_entries,
        structured_requirements=structured_requirements,
        signals=signals,
        report_path_text=path_text,
    )
    content = _render_report(
        slug=task_slug,
        generated_at=_timestamp(),
        task_artifacts=task_artifacts,
        generated_reports=generated_reports,
        evidence=evidence,
        verification=verification,
        verification_command=verification_command,
        test_result_evidence=test_result_evidence,
        todo_sources=todo_sources,
        report_findings=report_findings,
        manifest_entries=report_manifest_entries,
        workflow_checks=workflow_checks,
        structured_requirements=structured_requirements,
        findings=findings,
    )

    messages = [f"validation report: {path_text}", _summarize(findings)]
    if dry_run:
        action = "would refresh" if report_target.exists() else "would create"
        messages.append(f"{action} file {path_text}")
        messages.append("dry run; no files written")
        return 0, messages

    if report_target.exists() and report_target.read_text(encoding="utf-8") == content:
        messages.append(f"skip unchanged file {path_text}")
        messages.append("skip existing manifest .harness/manifest.json")
        return 0, messages

    action = "refresh" if report_target.exists() else "create"
    try:
        write_text(report_target, content)
        _write_manifest_for_validation_report(root, validation_report_path, report_target)
    except Exception as exc:
        return 1, [f"Could not write validation report or refresh manifest: {exc}"]
    messages.append(f"{action} file {path_text}")
    messages.append("refreshed manifest .harness/manifest.json")
    messages.append("root AGENTS.md and CLAUDE.md were not modified")
    messages.append(".harness/generated/agent-instructions.md was not modified")
    messages.append(f".harness/tasks/{task_slug}/generated/agent-workset.md was not modified")
    return 0, messages
