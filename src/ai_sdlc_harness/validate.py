"""Implementation of ``ai-sdlc validate``."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .constants import TASK_SLUG_PATTERN, VALIDATION_REPORT_FILENAME
from .files import (
    PathSafetyError,
    persist_exact_bytes,
    resolve_managed_output_under_root,
    resolve_under_root,
    sha256_bytes,
)
from .lineage import LineageValidationError, validate_manifest_provenance
from .manifest import (
    ManifestV2,
    ManagedFileRecord,
    load_manifest_model,
    persist_manifest_model,
    remove_provenance_records,
    replace_managed_and_provenance_records,
)
from .validation import (
    ArtifactStatus,
    ValidationInspectionResult,
    ValidationRequirementsStatus,
    capture_validation_snapshot,
    inspect_validation_snapshot,
    validation_provenance_from_snapshot,
)


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
DISCLAIMER = (
    "This report summarizes task workflow validation signals. It does not run tests, inspect target code deeply, "
    "prove correctness, prove security, validate compliance, scan for security issues, enforce packs, execute packs, "
    "or call AI models."
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


def _report_path(slug: str) -> PurePosixPath:
    return _task_dir_path(slug) / VALIDATION_REPORT_FILENAME


def _validate_task_slug(slug: str) -> None:
    if not slug or not slug.strip():
        raise ValueError("Task slug must not be empty.")
    if "\x00" in slug:
        raise ValueError("Task slug contains an unsafe NUL character.")
    if TASK_SLUG_RE.fullmatch(slug) is None:
        raise ValueError(
            "Task slug is unsafe. Use the safe slug created by `ai-sdlc task start`."
        )


def _summarize(result: ValidationInspectionResult) -> str:
    return (
        f"summary: {result.blocker_count} blocker(s), "
        f"{result.warning_count} warning(s), {result.info_count} info"
    )


def _render_status_line(item: ArtifactStatus) -> str:
    present = "yes" if item.present else "no"
    readable = "yes" if item.readable else "no"
    return (
        f"- `{item.filename}`: present={present}; readable={readable}; "
        f"state={item.message}"
    )


def _render_plain_status_line(item: ArtifactStatus) -> str:
    present = "yes" if item.present else "no"
    readable = "yes" if item.readable else "no"
    return (
        f"- {item.filename}: present={present}; readable={readable}; "
        f"state={item.message}"
    )


def _render_structured_requirements_status(
    result: ValidationRequirementsStatus,
) -> list[str]:
    lines = ["## Structured Requirements", ""]
    if not result.present:
        lines.append(
            "- requirements.yaml: present=no; readable=no; schema=missing"
        )
        lines.append("- Requirement count: 0")
        lines.append("- Finding count: 0")
        return lines
    readable = "yes" if result.readable else "no"
    if not result.readable:
        lines.append(
            "- requirements.yaml: present=yes; "
            f"readable={readable}; schema=not checked; message={result.message}"
        )
        lines.append("- Requirement count: 0")
        lines.append("- Finding count: 0")
        return lines
    schema = "valid" if result.schema_valid else "invalid"
    lines.append(
        f"- requirements.yaml: present=yes; readable=yes; schema={schema}"
    )
    lines.append(f"- Requirement count: {result.requirement_count}")
    lines.append(f"- Finding count: {result.finding_count}")
    if result.problems:
        lines.append("- Schema problems:")
        for problem in result.problems:
            lines.append(f"  - {problem}")
    return lines


def _key_workflow_outputs(
    result: ValidationInspectionResult,
) -> tuple[ArtifactStatus, ...]:
    return tuple(
        item
        for item in result.generated_reports
        if item.filename not in {"spec.md", "requirements.yaml"}
    )


def _render_report(
    *, result: ValidationInspectionResult, generated_at: str
) -> str:
    by_name = {item.filename: item for item in result.task_artifacts}
    acceptance = by_name["acceptance.md"]
    test_contract = by_name["test-contract.md"]
    evidence = by_name["evidence.md"]
    verification = by_name["verification.md"]
    missing_or_unreadable = tuple(
        item
        for item in _key_workflow_outputs(result)
        if not (item.present and item.readable)
    )
    lines = [
        "# Validation Report",
        "",
        "## Summary",
        "",
        f"- Task slug: `{result.task_slug}`",
        f"- Generated: {generated_at}",
        "- Review question: Is this task review-ready based on recorded workflow signals?",
        f"- Review-readiness answer: {result.review_readiness}",
        f"- Current blockers: {result.blocker_count}",
        f"- Current warnings: {result.warning_count}",
        f"- Current info: {result.info_count}",
        "- Prior report observations shown: "
        f"{len(result.report_findings)} (excluded from current counts)",
        f"- CLEAN: {'yes' if result.clean else 'no'}",
        "",
        "## Review Readiness",
        "",
        f"- Answer: {result.review_readiness}",
        f"- Reason: {result.review_readiness_reason}",
        "- Key workflow outputs present/readable: "
        f"{'yes' if not missing_or_unreadable else 'no'}",
        "",
        "## Workflow Signals",
        "",
    ]
    for label, value in result.workflow_checks:
        lines.append(f"- {label}: {value}")
    lines.append("")
    lines.extend(_render_status_line(item) for item in result.generated_reports)

    lines.append("")
    lines.extend(_render_structured_requirements_status(result.structured_requirements))

    lines.extend(["", "## Task Artifact Readiness", ""])
    lines.extend(_render_status_line(item) for item in result.task_artifacts)
    lines.extend(
        [
            "",
            "Task content signals:",
            "",
            "- Unresolved TODOs: "
            + (
                "present in " + ", ".join(result.todo_sources)
                if result.todo_sources
                else "missing"
            ),
            f"- Acceptance criteria: {'substantive' if acceptance.substantive else 'missing'}",
            f"- Test contract: {'substantive' if test_contract.substantive else 'missing'}",
            f"- Verification evidence: {'substantive' if verification.substantive else 'missing'}",
            f"- Implementation evidence: {'substantive' if evidence.substantive else 'missing'}",
            f"- Verification command: {result.verification_command}",
            f"- Test result evidence: {result.test_result_evidence}",
            "",
            "## Prior Report Observations",
            "",
            "Explicit Findings sections inspected for prior report observations: `preflight.md`, `test-contract-review.md`, `evidence-report.md`.",
            "Prior report observations are retained for provenance and excluded from current blocker/warning counts.",
            "`generated/agent-workset.md` is treated as a supporting artifact only.",
            "",
        ]
    )
    if result.report_findings:
        for finding in result.report_findings:
            lines.append(
                f"- `{finding.source}`: {finding.level}: {finding.message}"
            )
    else:
        lines.append("- none detected in readable prior reports")

    lines.extend(["", "## Missing Or Stale Artifacts", ""])
    if missing_or_unreadable:
        lines.extend(_render_plain_status_line(item) for item in missing_or_unreadable)
    else:
        lines.append("- no missing or unreadable key workflow outputs detected")
    lines.append("- stale artifact detection is not performed by this validate report.")

    lines.extend(["", "## Findings", ""])
    lines.extend(
        f"- {finding.level}: {finding.message}" for finding in result.findings
    )

    lines.extend(["", "## Recommended Next Actions", ""])
    lines.extend(f"- {action}" for action in result.recommended_actions)
    lines.extend(
        [
            "",
            "## Human Review Checklist",
            "",
            "- [ ] Review task artifacts for substantive task intent.",
            "- [ ] Review generated report presence and copied blocker/warning signals.",
            "- [ ] Review evidence.md and verification.md for recorded implementation and verification evidence.",
            "- [ ] Confirm unresolved TODOs, risks, or follow-up work are acceptable for human review.",
            "- [ ] Run `ai-sdlc verify` separately to review repository integrity.",
            "",
            "## Disclaimer",
            "",
            DISCLAIMER,
            "",
        ]
    )
    return "\n".join(lines)


def run_validate(
    root: Path,
    task_slug: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[int, list[str]]:
    harness_root = root / ".harness"
    if (
        not harness_root.is_dir()
        or not (harness_root / "config.yaml").is_file()
        or not (harness_root / "manifest.json").is_file()
    ):
        return 1, [
            "AI SDLC Harness is not initialized. Run `ai-sdlc init` from the project root."
        ]

    try:
        _validate_task_slug(task_slug)
    except ValueError as exc:
        return 2, [str(exc)]

    task_dir = _task_dir_path(task_slug)
    task_target = resolve_under_root(root, task_dir)
    if not task_target.is_dir():
        return 1, [
            f"Task folder does not exist: {task_dir.as_posix()}. Run `ai-sdlc task start` first."
        ]

    validation_report_path = _report_path(task_slug)
    path_text = validation_report_path.as_posix()
    try:
        report_target = resolve_managed_output_under_root(
            root, validation_report_path
        )
    except (PathSafetyError, OSError, RuntimeError):
        return 1, [
            f"unsafe managed validation report path: {path_text}",
            "refusing validation report path with symlink or unsafe indirection",
        ]

    try:
        manifest_path = resolve_under_root(root, ".harness/manifest.json")
        manifest = load_manifest_model(manifest_path)
        if isinstance(manifest, ManifestV2):
            validate_manifest_provenance(manifest)
    except (OSError, RuntimeError, ValueError, LineageValidationError):
        return 1, [
            "Could not read harness manifest or validate its closed provenance."
        ]

    managed_record = next(
        (record for record in manifest.managed_files if record.path == path_text),
        None,
    )
    try:
        report_exists = report_target.exists()
        if report_exists and not report_target.is_file():
            return 1, [
                f"managed validation report is not a regular file: {path_text}"
            ]
        existing_report_bytes = report_target.read_bytes() if report_exists else None
    except OSError:
        return 1, [f"Could not read managed validation report: {path_text}"]

    if report_exists and managed_record is None:
        return 1, [
            f"unmanaged existing file {path_text}",
            "refusing to overwrite unmanaged validation report",
        ]

    if report_exists and managed_record is not None:
        hash_clean = (
            managed_record.protected
            and managed_record.hash_algorithm == "sha256"
            and managed_record.sha256 is not None
            and sha256_bytes(existing_report_bytes or b"")
            == managed_record.sha256
        )
        if not hash_clean and not force:
            return 1, [
                f"hash drift detected for {path_text}",
                "refusing to overwrite manifest-managed validation report without --force",
            ]

    try:
        snapshot = capture_validation_snapshot(root, task_slug)
        result = inspect_validation_snapshot(snapshot)
    except Exception as exc:
        return 1, [f"Could not inspect validation inputs: {exc}"]
    generated_at = _timestamp()
    content = _render_report(
        result=result,
        generated_at=generated_at,
    ).encode("utf-8")
    intended_hash = sha256_bytes(content)
    intended_managed_record = ManagedFileRecord(
        path_text,
        True,
        "sha256",
        intended_hash,
    )
    try:
        intended_provenance = validation_provenance_from_snapshot(
            snapshot,
            output_sha256=intended_hash,
        )
    except Exception as exc:
        return 1, [f"Could not build validation provenance: {exc}"]

    existing_provenance = (
        next(
            (
                record
                for record in manifest.generated_artifact_provenance
                if record.output_path == path_text
            ),
            None,
        )
        if isinstance(manifest, ManifestV2)
        else None
    )
    report_changed = existing_report_bytes != content
    managed_current = managed_record == intended_managed_record
    provenance_current = existing_provenance == intended_provenance

    messages = [f"validation report: {path_text}", _summarize(result)]
    if dry_run:
        if not report_exists:
            messages.append(f"would create file {path_text}")
        elif report_changed:
            messages.append(f"would refresh file {path_text}")
        else:
            messages.append(f"would skip unchanged file {path_text}")
        if existing_provenance is None:
            messages.append(f"would create validation provenance for {path_text}")
        elif not provenance_current or not managed_current:
            messages.append(f"would refresh validation provenance for {path_text}")
        else:
            messages.append(f"would skip current validation provenance for {path_text}")
        messages.append("dry run; no files written")
        return 0, messages

    if (
        not report_changed
        and isinstance(manifest, ManifestV2)
        and managed_current
        and provenance_current
    ):
        messages.append(f"skip unchanged file {path_text}")
        messages.append(f"skip current validation provenance for {path_text}")
        messages.append("skip existing manifest .harness/manifest.json")
        return 0, messages

    working_manifest = manifest
    if report_changed and existing_provenance is not None:
        try:
            working_manifest = remove_provenance_records(
                manifest,
                (path_text,),
                generated_at=generated_at,
            )
            persist_manifest_model(root, working_manifest)
        except Exception:
            return 1, [
                "Could not invalidate validation provenance before changing "
                f"{path_text}."
            ]

    confirmed_hash = intended_hash
    if report_changed:
        try:
            persisted = persist_exact_bytes(report_target, content)
            confirmed_hash = persisted.sha256
        except Exception:
            return 1, [f"Could not persist exact validation report bytes: {path_text}"]

    try:
        confirmed_provenance = validation_provenance_from_snapshot(
            snapshot,
            output_sha256=confirmed_hash,
        )
        confirmed_managed_record = ManagedFileRecord(
            path_text,
            True,
            "sha256",
            confirmed_hash,
        )
        updated_manifest = replace_managed_and_provenance_records(
            working_manifest,
            (confirmed_managed_record,),
            (confirmed_provenance,),
            generated_at=generated_at,
            upgrade_to_v2=True,
        )
        assert isinstance(updated_manifest, ManifestV2)
        validate_manifest_provenance(updated_manifest)
        persist_manifest_model(root, updated_manifest)
    except Exception:
        return 1, [
            "Could not record validation report ownership and provenance in "
            ".harness/manifest.json."
        ]

    action = "refresh" if report_exists else "create"
    if report_changed:
        messages.append(f"{action} file {path_text}")
    else:
        messages.append(f"skip unchanged file {path_text}")
    provenance_action = "create" if existing_provenance is None else "refresh"
    messages.append(f"{provenance_action} validation provenance for {path_text}")
    messages.append("refreshed manifest .harness/manifest.json")
    messages.append("root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified")
    messages.append(".harness/generated/agent-instructions.md was not modified")
    messages.append(
        f".harness/tasks/{task_slug}/generated/agent-workset.md was not modified"
    )
    return 0, messages
