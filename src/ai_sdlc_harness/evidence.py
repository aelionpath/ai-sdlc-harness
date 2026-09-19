"""Implementation of ``ai-sdlc evidence``."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import (
    AGENT_WORKSET_FILENAME,
    EVIDENCE_REPORT_FILENAME,
    PREFLIGHT_FILENAME,
    TASK_ARTIFACT_FILENAMES,
    TASK_GENERATED_DIRNAME,
    TASK_SLUG_PATTERN,
    TEST_CONTRACT_REVIEW_FILENAME,
)
from .files import read_text, resolve_under_root, write_text
from .manifest import load_manifest, sha256_file
from .redact import redact_text


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
FINDING_LINE_RE = re.compile(r"^\s*-?\s*(blocker|warning):\s+(.+?)\s*$", re.IGNORECASE)
ENV_LINE_RE = re.compile(
    r"^\s*(?:export\s+|\$env:)?[A-Z_][A-Z0-9_]{1,}\s*=",
    re.IGNORECASE,
)
TEST_COMMAND_RE = re.compile(
    r"\b(pytest|unittest|tox|nox|coverage|npm\s+test|npm\s+run\s+test|yarn\s+test|pnpm\s+test|cargo\s+test|go\s+test|dotnet\s+test|mvn\s+test|gradle\s+test)\b",
    re.IGNORECASE,
)
TEST_RESULT_RE = re.compile(
    r"\b(test result|tests? passed|tests? failed|passed in|failed in|exit code|return code|returncode|successfully ran)\b",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
NOT_RUN_RE = re.compile(
    r"\b(not run|not-run|skipped|skip|not applicable|n/a|manual only|manual-only|not practical|no automated)\b",
    re.IGNORECASE,
)
RATIONALE_RE = re.compile(
    r"\b(because|since|due to|reason|why|manual verification|manual review|not practical|not applicable|n/a)\b",
    re.IGNORECASE,
)
WHY_RE = re.compile(r"\b(why|because|since|so that|in order to|needed to|to address)\b", re.IGNORECASE)
RISK_ACCEPTANCE_RE = re.compile(r"\b(risk acceptance|accepted risk|acceptance|accepted|none|not applicable|n/a)\b", re.IGNORECASE)
MAX_EXCERPT_CHARS = 1200
MAX_EXCERPT_LINES = 40

DISCLAIMER = (
    "This report summarizes task-scoped implementation and review trace for human review. It does not run tests, "
    "inspect target code deeply, prove correctness, prove security, validate compliance, scan for security issues, "
    "enforce packs, execute packs, generate tests, perform audit validation, or call AI models."
)


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


@dataclass(frozen=True)
class SectionEvidence:
    label: str
    source: str
    state: str
    text: str
    aliases: tuple[str, ...]

    @property
    def substantive(self) -> bool:
        return self.state == "substantive"


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


def _artifact_path(slug: str, filename: str) -> PurePosixPath:
    return _task_dir_path(slug) / filename


def _workset_path(slug: str) -> PurePosixPath:
    return _task_dir_path(slug) / TASK_GENERATED_DIRNAME / AGENT_WORKSET_FILENAME


def _report_path(slug: str) -> PurePosixPath:
    return _task_dir_path(slug) / EVIDENCE_REPORT_FILENAME


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


def _write_manifest_for_evidence_report(root: Path, report_path: PurePosixPath, report_target: Path) -> None:
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


def _read_readiness_artifacts(root: Path, slug: str) -> list[ArtifactStatus]:
    paths = (
        (PREFLIGHT_FILENAME, _artifact_path(slug, PREFLIGHT_FILENAME)),
        (TEST_CONTRACT_REVIEW_FILENAME, _artifact_path(slug, TEST_CONTRACT_REVIEW_FILENAME)),
        (f"{TASK_GENERATED_DIRNAME}/{AGENT_WORKSET_FILENAME}", _workset_path(slug)),
    )
    return [_read_artifact(root, relative, label) for label, relative in paths]


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


def _normalize_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _section_text(text: str, aliases: tuple[str, ...]) -> str:
    alias_set = {_normalize_heading(alias) for alias in aliases}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line.strip())
        if not match:
            continue
        if _normalize_heading(match.group(2)) not in alias_set:
            continue
        start_level = len(match.group(1))
        start = index + 1
        end = len(lines)
        for end_index in range(start, len(lines)):
            next_match = HEADING_RE.match(lines[end_index].strip())
            if next_match and len(next_match.group(1)) <= start_level:
                end = end_index
                break
        section_lines = lines[start:end]
        if start_level != 1:
            return "\n".join(section_lines).strip()

        direct_lines: list[str] = []
        for section_line in section_lines:
            if HEADING_RE.match(section_line.strip()):
                break
            direct_lines.append(section_line)
        direct_text = "\n".join(direct_lines).strip()
        if direct_text:
            return direct_text
    return ""


def _section_evidence(item: ArtifactStatus, label: str, aliases: tuple[str, ...]) -> SectionEvidence:
    if not item.present:
        return SectionEvidence(label, item.filename, "missing artifact", "", aliases)
    if not item.readable:
        return SectionEvidence(label, item.filename, "unreadable artifact", "", aliases)
    text = _section_text(item.text, aliases)
    if not text:
        return SectionEvidence(label, item.filename, "missing section", "", aliases)
    return SectionEvidence(label, item.filename, _readiness_state(text), text, aliases)


def _first_substantive_section(*sections: SectionEvidence) -> SectionEvidence:
    for section in sections:
        if section.substantive:
            return section
    for section in sections:
        if section.state not in {"missing section", "missing artifact"}:
            return section
    return sections[0]


def _status_label(section: SectionEvidence) -> str:
    return f"{section.state} in `{section.source}`"


def _section_line(section: SectionEvidence) -> str:
    return f"- {section.label}: {_status_label(section)}"


def _render_section_excerpt(section: SectionEvidence) -> list[str]:
    if not section.text:
        return [f"- Source: `{section.source}`", f"- Status: {section.state}"]
    return [
        f"- Source: `{section.source}`",
        f"- Status: {section.state}",
        "",
        "```text",
        _bounded_excerpt(section.text, max_chars=MAX_EXCERPT_CHARS),
        "```",
    ]


def _verification_command_status(verification: ArtifactStatus) -> str:
    if not verification.readable:
        return "missing"
    return "present" if TEST_COMMAND_RE.search(verification.text) else "missing"


def _test_result_status(evidence: ArtifactStatus, verification: ArtifactStatus) -> str:
    text = "\n".join(item.text for item in (evidence, verification) if item.readable)
    return "present" if TEST_RESULT_RE.search(text) else "missing"


def _combined_readable_text(*items: ArtifactStatus) -> str:
    return "\n".join(item.text for item in items if item.readable)


def _has_unrationalized_not_run_text(*items: ArtifactStatus) -> bool:
    for line in _combined_readable_text(*items).splitlines():
        if NOT_RUN_RE.search(line) and not RATIONALE_RE.search(line):
            return True
    return False


def _contains_none_or_not_applicable(text: str) -> bool:
    return bool(re.search(r"\b(none|not applicable|n/a)\b", text, re.IGNORECASE))


def _trace_sections(by_name: dict[str, ArtifactStatus]) -> dict[str, SectionEvidence]:
    evidence = by_name["evidence.md"]
    verification = by_name["verification.md"]
    acceptance = by_name["acceptance.md"]

    final_evidence = _section_evidence(
        evidence,
        "What changed",
        ("Final Evidence", "Evidence", "Review Evidence"),
    )
    generated_artifacts = _section_evidence(
        evidence,
        "Files or artifacts updated",
        ("Generated Or Updated Artifacts", "Changed Artifacts", "Updated Artifacts"),
    )
    evidence_tests = _section_evidence(
        evidence,
        "Tests/checks run",
        ("Tests And Checks Run", "Commands", "Tests", "Verification"),
    )
    verification_commands = _section_evidence(
        verification,
        "Verification commands",
        ("Commands And Checks Run", "Commands And Tests To Run", "Commands And Tests To Run Later", "Verification Commands", "Commands", "Tests"),
    )
    evidence_results = _section_evidence(
        evidence,
        "Test/check results",
        ("Results", "Verification Results"),
    )
    verification_results = _section_evidence(
        verification,
        "Verification results",
        ("Results", "Verification Results"),
    )
    known_gaps = _section_evidence(
        evidence,
        "Known gaps and risks",
        ("Known Gaps And Risks", "Known Gaps", "Risks"),
    )
    references = _section_evidence(
        evidence,
        "References or links",
        ("References", "Supporting Artifacts", "Links"),
    )
    requirements = _section_evidence(
        acceptance,
        "Requirements / acceptance criteria",
        ("Requirements And Acceptance Criteria", "Acceptance Criteria"),
    )
    protected = _section_evidence(
        acceptance,
        "Protected behavior / non-goals",
        ("Protected Behavior And Non-Goals", "Non-Goals", "Non Goals"),
    )
    manual_review = _section_evidence(
        verification,
        "Manual review notes",
        ("Manual Review Notes", "Manual Verification", "Review Notes", "Notes"),
    )
    not_run = _first_substantive_section(
        _section_evidence(
            evidence,
            "Commands not run and why",
            ("Commands Not Run And Why", "Not Run / Why", "Not Run And Why"),
        ),
        _section_evidence(
            verification,
            "Commands not run and why",
            ("Commands Not Run And Why", "Not Run / Why", "Not Run And Why"),
        ),
    )

    return {
        "what_changed": final_evidence,
        "artifacts_updated": generated_artifacts,
        "tests_run": _first_substantive_section(evidence_tests, verification_commands),
        "results": _first_substantive_section(evidence_results, verification_results),
        "gaps_risks": known_gaps,
        "references": references,
        "requirements": requirements,
        "protected": protected,
        "manual_review": manual_review,
        "not_run": not_run,
    }


def _todo_sources(task_artifacts: list[ArtifactStatus], readiness_artifacts: list[ArtifactStatus]) -> list[str]:
    sources: list[str] = []
    for item in (*task_artifacts, *readiness_artifacts):
        if item.filename == EVIDENCE_REPORT_FILENAME or not item.readable:
            continue
        if TODO_RE.search(item.text):
            sources.append(item.filename)
    return sources


def _report_findings(readiness_artifacts: list[ArtifactStatus]) -> list[ReportFinding]:
    findings: list[ReportFinding] = []
    for item in readiness_artifacts:
        if not item.present or not item.readable:
            continue
        for line in item.text.splitlines():
            match = FINDING_LINE_RE.match(line)
            if not match:
                continue
            findings.append(ReportFinding(item.filename, match.group(1).lower(), match.group(2)))
    return findings[:30]


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


def _build_findings(
    *,
    evidence: ArtifactStatus,
    verification: ArtifactStatus,
    verification_command: str,
    test_result_evidence: str,
    trace_sections: dict[str, SectionEvidence],
    todo_sources: list[str],
    readiness_artifacts: list[ArtifactStatus],
    report_findings: list[ReportFinding],
    manifest_entries: list[ManifestTaskEntry],
    report_path_text: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for item in (evidence, verification):
        if not item.present:
            findings.append(Finding("blocker", f"{item.filename} is missing."))
        elif not item.readable:
            findings.append(Finding("blocker", f"{item.filename} is unreadable."))

    if evidence.present and evidence.readable and evidence.state == "TODO-only":
        findings.append(Finding("warning", "evidence.md is TODO-only."))
    if verification.present and verification.readable and verification.state == "TODO-only":
        findings.append(Finding("warning", "verification.md is TODO-only."))
    if not evidence.substantive and not verification.substantive:
        findings.append(Finding("blocker", "evidence.md and verification.md both have no substantive content."))

    what_changed = trace_sections["what_changed"]
    if not what_changed.substantive:
        findings.append(Finding("blocker", f"implementation trace for what changed is {_status_label(what_changed)}."))
    elif not WHY_RE.search(what_changed.text):
        findings.append(Finding("warning", "why the implementation changed is not clearly recorded."))

    if not trace_sections["requirements"].substantive:
        findings.append(
            Finding("warning", f"requirements / acceptance evidence is {_status_label(trace_sections['requirements'])}.")
        )
    if not trace_sections["protected"].substantive:
        findings.append(
            Finding("warning", f"protected behavior / non-goals evidence is {_status_label(trace_sections['protected'])}.")
        )
    if not trace_sections["artifacts_updated"].substantive:
        findings.append(Finding("warning", f"files or artifacts updated are {_status_label(trace_sections['artifacts_updated'])}."))
    if not trace_sections["tests_run"].substantive:
        findings.append(Finding("warning", f"tests/checks run are {_status_label(trace_sections['tests_run'])}."))
    if not trace_sections["results"].substantive:
        findings.append(Finding("warning", f"test/check results are {_status_label(trace_sections['results'])}."))
    if not trace_sections["not_run"].substantive:
        findings.append(Finding("warning", f"commands not run and why are {_status_label(trace_sections['not_run'])}."))
    if not trace_sections["manual_review"].substantive:
        findings.append(Finding("warning", f"manual review notes are {_status_label(trace_sections['manual_review'])}."))
    gaps_risks = trace_sections["gaps_risks"]
    if not gaps_risks.substantive:
        findings.append(Finding("warning", f"known gaps and unresolved risks are {_status_label(gaps_risks)}."))
        findings.append(Finding("warning", f"implementation deviations are {_status_label(gaps_risks)}."))
    else:
        if not re.search(r"\bdeviations?\b", gaps_risks.text, re.IGNORECASE) and not _contains_none_or_not_applicable(gaps_risks.text):
            findings.append(Finding("warning", "implementation deviations are not clearly recorded."))
        if not re.search(r"\bgaps?\b", gaps_risks.text, re.IGNORECASE) and not _contains_none_or_not_applicable(gaps_risks.text):
            findings.append(Finding("warning", "known gaps are not clearly recorded."))
        if not re.search(r"\brisks?\b", gaps_risks.text, re.IGNORECASE) and not _contains_none_or_not_applicable(gaps_risks.text):
            findings.append(Finding("warning", "unresolved risks are not clearly recorded."))
        if not RISK_ACCEPTANCE_RE.search(gaps_risks.text):
            findings.append(Finding("warning", "risk acceptances are not clearly recorded."))
    if not trace_sections["references"].substantive:
        findings.append(Finding("warning", f"references or supporting artifacts are {_status_label(trace_sections['references'])}."))

    if verification_command == "missing":
        findings.append(Finding("warning", "no verification command recorded."))
    if test_result_evidence == "missing":
        findings.append(Finding("warning", "no test result evidence recorded."))
    if _has_unrationalized_not_run_text(evidence, verification):
        findings.append(Finding("warning", "not-run or skipped commands are recorded without rationale."))
    if todo_sources:
        findings.append(Finding("warning", f"unresolved TODOs remain in: {', '.join(todo_sources)}."))

    for item in readiness_artifacts:
        if not item.present:
            findings.append(Finding("warning", f"{item.filename} is missing."))
        elif not item.readable:
            findings.append(Finding("warning", f"{item.filename} is unreadable."))

    for report_finding in report_findings:
        if report_finding.level in {"blocker", "warning"}:
            findings.append(
                Finding("warning", f"{report_finding.source} reported {report_finding.level}: {report_finding.message}")
            )

    for entry in manifest_entries:
        if entry.path == report_path_text:
            continue
        if entry.status == "missing":
            findings.append(Finding("blocker", f"manifest-managed task artifact is missing: {entry.path}."))
        elif entry.hash_status in {"hash drift", "missing hash"}:
            findings.append(Finding("blocker", f"manifest-managed task artifact has {entry.hash_status}: {entry.path}."))

    findings.append(Finding("info", "report generated deterministically."))
    findings.append(Finding("info", "harness did not run tests."))
    findings.append(
        Finding(
            "info",
            "harness did not inspect target code deeply, prove correctness/security/compliance, call AI models, enforce packs, generate tests, or perform audit/compliance validation.",
        )
    )
    return findings


def _summarize(findings: list[Finding]) -> str:
    blockers = sum(1 for finding in findings if finding.level == "blocker")
    warnings = sum(1 for finding in findings if finding.level == "warning")
    infos = sum(1 for finding in findings if finding.level == "info")
    return f"summary: {blockers} blocker(s), {warnings} warning(s), {infos} info"


def _recommended_actions(findings: list[Finding]) -> list[str]:
    messages = [finding.message for finding in findings if finding.level in {"blocker", "warning"}]
    actions: list[str] = []
    if any("what changed" in message or "why" in message or "files or artifacts" in message for message in messages):
        actions.append("Record what changed, why it changed, and the files or artifacts updated in evidence.md.")
    if any("requirements" in message or "protected behavior" in message for message in messages):
        actions.append("Record requirements addressed, acceptance evidence, protected behavior, and non-goals for human review.")
    if any("verification.md" in message or "verification command" in message or "tests/checks" in message for message in messages):
        actions.append("Record verification commands, tests/checks run, results, commands not run and why, and manual review notes.")
    if any("test result evidence" in message or "test/check results" in message for message in messages):
        actions.append("Record test command results or explain why tests were not run.")
    if any("gaps" in message or "risks" in message or "risk acceptances" in message for message in messages):
        actions.append("Record known gaps, unresolved risks, and risk acceptances before relying on this report for review.")
    if any("references" in message or "supporting artifacts" in message for message in messages):
        actions.append("Add references or links to supporting artifacts for the human reviewer.")
    if any("TODO" in message for message in messages):
        actions.append("Resolve or explicitly carry forward unresolved TODOs before relying on this report for review.")
    if any("manifest-managed" in message for message in messages):
        actions.append("Run ai-sdlc verify and resolve protected artifact drift before final review.")
    if not actions:
        actions.append("Review the recorded evidence, verification notes, and readiness reports before accepting the implementation.")
    return actions


def _render_status_line(item: ArtifactStatus) -> str:
    present = "yes" if item.present else "no"
    readable = "yes" if item.readable else "no"
    return f"- `{item.filename}`: present={present}; readable={readable}; readiness={item.message}"


def _render_report(
    *,
    slug: str,
    generated_at: str,
    task_artifacts: list[ArtifactStatus],
    readiness_artifacts: list[ArtifactStatus],
    evidence: ArtifactStatus,
    verification: ArtifactStatus,
    verification_command: str,
    test_result_evidence: str,
    trace_sections: dict[str, SectionEvidence],
    todo_sources: list[str],
    report_findings: list[ReportFinding],
    manifest_entries: list[ManifestTaskEntry],
    findings: list[Finding],
) -> str:
    counts = {
        "blocker": sum(1 for finding in findings if finding.level == "blocker"),
        "warning": sum(1 for finding in findings if finding.level == "warning"),
        "info": sum(1 for finding in findings if finding.level == "info"),
    }
    lines = [
        "# Evidence Report",
        "",
        "## Summary",
        "",
        f"- Task slug: `{slug}`",
        f"- Generated: {generated_at}",
        f"- Blockers: {counts['blocker']}",
        f"- Warnings: {counts['warning']}",
        f"- Info: {counts['info']}",
        f"- `evidence.md`: {evidence.message}",
        f"- `verification.md`: {verification.message}",
        f"- Verification command: {verification_command}",
        f"- Test/check result evidence: {test_result_evidence}",
        f"- Unresolved TODOs: {'present in ' + ', '.join(todo_sources) if todo_sources else 'none detected'}",
        "",
        "## Implementation Trace",
        "",
    ]
    lines.append(_section_line(trace_sections["what_changed"]))
    lines.append("")
    lines.extend(_render_section_excerpt(trace_sections["what_changed"]))
    lines.extend(["", _section_line(trace_sections["artifacts_updated"])])
    lines.extend(
        [
            "",
            "## Requirements / Acceptance Evidence",
            "",
            _section_line(trace_sections["requirements"]),
            "",
            "## Boundary / Protected Behavior Evidence",
            "",
            _section_line(trace_sections["protected"]),
            "",
            "## Test / Verification Evidence",
            "",
            _section_line(trace_sections["tests_run"]),
            _section_line(trace_sections["results"]),
            _section_line(trace_sections["not_run"]),
            _section_line(trace_sections["manual_review"]),
            "",
            "## Deviations, Gaps, And Risks",
            "",
            _section_line(trace_sections["gaps_risks"]),
        ]
    )
    if trace_sections["gaps_risks"].text:
        lines.extend(["", "```text", _bounded_excerpt(trace_sections["gaps_risks"].text, max_chars=MAX_EXCERPT_CHARS), "```"])

    lines.extend(
        [
            "",
            "## Supporting Artifacts",
            "",
            _section_line(trace_sections["references"]),
            "",
            "Task artifact presence:",
        ]
    )
    lines.extend(_render_status_line(item) for item in task_artifacts)

    lines.extend(["", "Manifest-managed task artifact summary:"])
    if manifest_entries:
        for entry in manifest_entries:
            lines.append(f"- `{entry.path}`: status={entry.status}; hash={entry.hash_status}")
    else:
        lines.append("- none recorded for this task")

    lines.extend(["", "## Prior Report Signals", ""])
    lines.extend(_render_status_line(item) for item in readiness_artifacts)
    lines.append("")
    if report_findings:
        for finding in report_findings:
            lines.append(f"- `{finding.source}`: {finding.level}: {finding.message}")
    else:
        lines.append("- none detected in readable prior reports or worksets")

    lines.extend(["", "## Findings", ""])
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
            DISCLAIMER,
            "",
        ]
    )
    return "\n".join(lines)


def run_evidence(root: Path, task_slug: str, *, dry_run: bool = False, force: bool = False) -> tuple[int, list[str]]:
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

    evidence_report_path = _report_path(task_slug)
    try:
        report_target = resolve_under_root(root, evidence_report_path)
        entries = _load_manifest_entries(root)
    except Exception as exc:
        return 1, [f"Could not read harness manifest or resolve evidence report path: {exc}"]

    path_text = evidence_report_path.as_posix()
    entry = entries.get(path_text)
    if report_target.exists() and entry is None:
        return 1, [
            f"unmanaged existing file {path_text}",
            "refusing to overwrite unmanaged evidence report",
        ]

    if report_target.exists() and entry is not None:
        expected_hash = entry.get("sha256")
        hash_algorithm = entry.get("hash_algorithm")
        hash_clean = hash_algorithm == "sha256" and expected_hash and sha256_file(report_target) == expected_hash
        if not hash_clean and not force:
            return 1, [
                f"hash drift detected for {path_text}",
                "refusing to overwrite manifest-managed evidence report without --force",
            ]

    task_artifacts = _read_task_artifacts(root, task_slug)
    readiness_artifacts = _read_readiness_artifacts(root, task_slug)
    by_name = {item.filename: item for item in task_artifacts}
    evidence = by_name["evidence.md"]
    verification = by_name["verification.md"]
    verification_command = _verification_command_status(verification)
    test_result_evidence = _test_result_status(evidence, verification)
    trace_sections = _trace_sections(by_name)
    todo_sources = _todo_sources(task_artifacts, readiness_artifacts)
    report_findings = _report_findings(readiness_artifacts)
    manifest_entries = _manifest_task_entries(root, task_slug, entries)
    findings = _build_findings(
        evidence=evidence,
        verification=verification,
        verification_command=verification_command,
        test_result_evidence=test_result_evidence,
        trace_sections=trace_sections,
        todo_sources=todo_sources,
        readiness_artifacts=readiness_artifacts,
        report_findings=report_findings,
        manifest_entries=manifest_entries,
        report_path_text=path_text,
    )
    content = _render_report(
        slug=task_slug,
        generated_at=_timestamp(),
        task_artifacts=task_artifacts,
        readiness_artifacts=readiness_artifacts,
        evidence=evidence,
        verification=verification,
        verification_command=verification_command,
        test_result_evidence=test_result_evidence,
        trace_sections=trace_sections,
        todo_sources=todo_sources,
        report_findings=report_findings,
        manifest_entries=manifest_entries,
        findings=findings,
    )

    messages = [f"evidence report: {path_text}", _summarize(findings)]
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
    write_text(report_target, content)
    _write_manifest_for_evidence_report(root, evidence_report_path, report_target)
    messages.append(f"{action} file {path_text}")
    messages.append("refreshed manifest .harness/manifest.json")
    messages.append("root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified")
    messages.append(".harness/generated/agent-instructions.md was not modified")
    messages.append(f".harness/tasks/{task_slug}/generated/agent-workset.md was not modified")
    return 0, messages
