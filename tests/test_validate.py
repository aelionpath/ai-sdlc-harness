from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
import yaml

from ai_sdlc_harness.evidence import run_evidence
from ai_sdlc_harness.generate import run_generate
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.spec import run_spec
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.test_contract import run_test_contract_review
from ai_sdlc_harness.validate import run_validate
from ai_sdlc_harness.validation import (
    ValidationCurrentnessState,
    capture_validation_snapshot,
    inspect_validation_snapshot,
    resolve_validation_currentness,
    validation_dependency_paths,
    validation_observation_definitions,
)
from ai_sdlc_harness.verify import verify_project
from ai_sdlc_harness.manifest import (
    ManifestV2,
    load_manifest_model,
    persist_manifest_model,
    remove_provenance_records,
    write_manifest,
)


TASK_FILES = [
    "task.md",
    "acceptance.md",
    "architecture-notes.md",
    "coupling-notes.md",
    "test-contract.md",
    "verification.md",
    "evidence.md",
]

BASE_MANAGED_FILES = [
    ".harness/config.yaml",
    ".harness/state.json",
    ".harness/manifest.json",
    ".harness/generated/agent-instructions.md",
    ".harness/packs/selected.yaml",
]


def _task_path(root: Path, slug: str, filename: str) -> Path:
    return root / ".harness" / "tasks" / slug / filename


def _workset_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "generated" / "agent-workset.md"


def _report_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "validation-report.md"


def _requirements_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "requirements.yaml"


def _manifest(root: Path) -> dict:
    return json.loads((root / ".harness" / "manifest.json").read_text(encoding="utf-8"))


def _manifest_entry(root: Path, path: str) -> dict:
    entries = {entry["path"]: entry for entry in _manifest(root)["managed_files"]}
    return entries[path]


def _start_sample_task(root: Path, title: str = "Validate me") -> str:
    assert init_project(root)[0] == 0
    assert start_task(root, title)[0] == 0
    return "validate-me"


def _fill_task_inputs(root: Path, slug: str) -> None:
    _task_path(root, slug, "task.md").write_text(
        """# Task: Validate me

Task slug: `validate-me`

## Scope

Summarize workflow validation signals only.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "acceptance.md").write_text(
        """# Acceptance

## Acceptance Criteria

- validation-report.md is created for the selected task.
- The report summarizes workflow consistency findings for human review.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "architecture-notes.md").write_text(
        """# Architecture Notes

## Boundary

Keep this inside deterministic harness report generation.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "coupling-notes.md").write_text(
        """# Coupling Notes

## New Or Changed Coupling

The report is manifest-managed like other task outputs.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "test-contract.md").write_text(
        """# Test Contract

## Desired Behavior Tests

- Validation generates findings for human review.

## Regression Tests

- Unmanaged reports are refused.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run Later

py -m pytest -p no:cacheprovider

## Results

Tests passed in 1.00s.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

Implemented deterministic validation summarization.

## Verification Results

py -m pytest -p no:cacheprovider passed in 1.00s.
""",
        encoding="utf-8",
    )


def _add_repo_signals(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname = \"sample\"\n", encoding="utf-8")
    (root / "tests").mkdir()
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")


def _write_workflow_outputs(root: Path, slug: str, *, findings: dict[str, str] | None = None) -> None:
    findings = findings or {}
    for filename in ("preflight.md", "test-contract-review.md", "evidence-report.md"):
        _task_path(root, slug, filename).write_text(findings.get(filename, "# Report\n\nNo findings.\n"), encoding="utf-8")
    _workset_path(root, slug).parent.mkdir(exist_ok=True)
    _workset_path(root, slug).write_text(findings.get("generated/agent-workset.md", "# Workset\n\nSupport artifact.\n"), encoding="utf-8")
    write_manifest(
        root,
        extra_managed_paths=[
            PurePosixPath(".harness") / "tasks" / slug / "preflight.md",
            PurePosixPath(".harness") / "tasks" / slug / "test-contract-review.md",
            PurePosixPath(".harness") / "tasks" / slug / "evidence-report.md",
            PurePosixPath(".harness") / "tasks" / slug / "generated" / "agent-workset.md",
        ],
    )


def _write_spec_output(root: Path, slug: str) -> None:
    _task_path(root, slug, "spec.md").write_text("# Specification\n\nReady for validation signal tests.\n", encoding="utf-8")


def _write_requirements_output(root: Path, slug: str, *, status: str = "draft") -> None:
    _requirements_path(root, slug).write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "artifact_role": "advisory_projection",
                "authority": "non_authoritative",
                "edit_model": "edit_source_task_artifacts_and_rerun_spec",
                "task_slug": slug,
                "source_model": "deterministic_acceptance_markdown_projection",
                "source_artifacts": [{"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"}],
                "requirements": [
                    {
                        "id": "REQ-001",
                        "statement": "Validation report is created.",
                        "status": status,
                        "source": {"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"},
                        "acceptance_criteria": [],
                        "verification": [],
                    }
                ],
                "findings": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _refresh_task_manifest(root: Path) -> None:
    write_manifest(root)


def test_validate_fails_before_init(project_tmp):
    code, messages = run_validate(project_tmp, "missing-task")

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_validate_fails_if_task_does_not_exist(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_validate(project_tmp, "missing-task")

    assert code == 1
    assert "Task folder does not exist: .harness/tasks/missing-task" in messages[0]


def test_validate_rejects_unsafe_task_slug(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_validate(project_tmp, "../bad")

    assert code == 2
    assert "Task slug is unsafe" in messages[0]


def test_validate_creates_report_with_review_signals_and_manifest_entry(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _refresh_task_manifest(project_tmp)
    _add_repo_signals(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    assert run_generate(project_tmp, slug)[0] == 0
    assert run_evidence(project_tmp, slug)[0] == 0

    code, messages = run_validate(project_tmp, slug)

    path = _report_path(project_tmp, slug)
    text = path.read_text(encoding="utf-8")
    assert code == 0
    assert f"validation report: .harness/tasks/{slug}/validation-report.md" in messages
    assert path.is_file()
    assert "Task slug: `validate-me`" in text
    assert (
        "This report summarizes task workflow validation signals. It does not run tests, inspect target code deeply, "
        "prove correctness, prove security, validate compliance, scan for security issues, enforce packs, execute packs, "
        "or call AI models."
    ) in text
    assert "semantic enforcement" not in text.lower()
    assert "## Summary" in text
    assert "- Review question: Is this task review-ready based on recorded workflow signals?" in text
    assert "- Review-readiness answer: Caution" in text
    assert "## Review Readiness" in text
    assert "## Workflow Signals" in text
    assert "## Task Artifact Readiness" in text
    assert "`task.md`: present=yes; readable=yes" in text
    assert "## Prior Report Observations" in text
    assert "excluded from current blocker/warning counts" in text
    assert "`preflight.md`: present=yes; readable=yes" in text
    assert "`spec.md`: present=no; readable=no" in text
    assert "`requirements.yaml`: present=no; readable=no" in text
    assert "`test-contract-review.md`: present=yes; readable=yes" in text
    assert "`generated/agent-workset.md`: present=yes; readable=yes" in text
    assert "`evidence-report.md`: present=yes; readable=yes" in text
    assert "## Manifest / Integrity Signals" not in text
    assert "- CLEAN: no" in text
    assert "## Missing Or Stale Artifacts" in text
    assert "- stale artifact detection is not performed by this validate report." in text
    assert "- preflight was generated: yes" in text
    assert "- spec was generated: no" in text
    assert "- requirements.yaml was generated: no" in text
    assert "- workset was generated: yes" in text
    assert "## Findings" in text
    assert "## Recommended Next Actions" in text
    assert "## Human Review Checklist" in text
    assert "## Disclaimer" in text
    assert "- info: validate does not run tests." in text
    assert "- info: validate does not inspect target code deeply." in text
    assert "- info: validate does not prove correctness, security, or compliance." in text
    assert "- info: validate does not call AI models." in text
    assert "- info: validate does not enforce packs." in text
    assert "compliant" not in text.lower()

    entry = _manifest_entry(project_tmp, f".harness/tasks/{slug}/validation-report.md")
    assert entry["protected"] is True
    assert entry["hash_algorithm"] == "sha256"
    assert entry["sha256"]


def test_validate_generates_report_with_blockers_for_missing_or_todo_inputs(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "acceptance.md").unlink()

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Review-readiness answer: No" in text
    assert "blocker: acceptance.md is missing." in text
    assert "blocker: test-contract.md is TODO-only." in text
    assert "blocker: verification.md is TODO-only." in text
    assert "blocker: evidence.md is TODO-only." in text
    assert "blocker: no usable acceptance criteria and no usable test-contract content are available." in text
    assert "blocker: no usable evidence and no usable verification content are available." in text


def test_validate_keeps_prior_observations_out_of_current_findings(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(
        project_tmp,
        slug,
        findings={
            "preflight.md": "# Preflight Report\n\n## Findings\n\n- blocker: boundary issue remains.\n- warning: shared issue remains.\n- info: preflight noted context.\n",
            "test-contract-review.md": "# Test-Contract Readiness Review\n\n## Findings\n\n- warning: shared issue remains.\n",
            "evidence-report.md": "# Evidence Report\n\n## Findings\n\n- warning: evidence issue remains.\n",
            "generated/agent-workset.md": "# Agent Workset\n\n## Findings\n\n- blocker: generated workset blocker.\n",
        },
    )
    _write_spec_output(project_tmp, slug)
    _write_requirements_output(project_tmp, slug)

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Review-readiness answer: Yes" in text
    assert text.count("shared issue remains.") == 2
    assert "`preflight.md`: blocker: boundary issue remains." in text
    assert "`preflight.md`: warning: shared issue remains." in text
    assert "`test-contract-review.md`: warning: shared issue remains." in text
    assert "`preflight.md`: info: preflight noted context." in text
    assert "`evidence-report.md`: warning: evidence issue remains." in text
    current_findings = text.split("## Findings", 1)[1].split(
        "## Recommended Next Actions", 1
    )[0]
    assert "boundary issue remains" not in current_findings
    assert "shared issue remains" not in current_findings
    assert "evidence issue remains" not in current_findings
    assert "generated workset blocker" not in text


def test_validate_missing_prior_reports_are_warnings_and_not_review_ready(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _refresh_task_manifest(project_tmp)

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Review-readiness answer: No" in text
    assert "- warning: preflight.md is missing." in text
    assert "- warning: spec.md is missing." in text
    assert "- warning: requirements.yaml is missing." in text
    assert "- warning: test-contract-review.md is missing." in text
    assert "- warning: generated/agent-workset.md is missing." in text
    assert "- warning: evidence-report.md is missing." in text


def test_validate_review_readiness_caution_when_only_warnings_remain(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "verification.md").write_text(
        """# Verification

## Results

Tests passed in 1.00s.
""",
        encoding="utf-8",
    )
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Review-readiness answer: Caution" in text
    assert "- warning: no verification command recorded." in text


def test_validate_review_readiness_yes_only_when_key_outputs_ready_and_no_warnings(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)
    _write_spec_output(project_tmp, slug)
    _write_requirements_output(project_tmp, slug)

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Review-readiness answer: Yes" in text
    assert "- Answer: Yes" in text
    assert "- Key workflow outputs present/readable: yes" in text
    assert "- blocker:" not in text
    assert "- warning:" not in text
    assert "- info:" in text


def test_validate_reports_present_spec_without_parsing_spec_findings(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- spec was generated: yes" in text
    assert "- requirements.yaml was generated: yes" in text
    assert "`spec.md`: present=yes; readable=yes" in text
    assert "`requirements.yaml`: present=yes; readable=yes" in text
    assert "- requirements.yaml: present=yes; readable=yes; schema=valid" in text
    assert "- Requirement count: 2" in text
    assert "- Finding count: 0" in text
    assert "spec.md reported" not in text


def test_validate_missing_spec_is_warning_only_and_not_command_failure(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Review-readiness answer: Caution" in text
    assert "- spec was generated: no" in text
    assert "- requirements.yaml was generated: no" in text
    assert "- warning: spec.md is missing." in text
    assert "- warning: requirements.yaml is missing." in text
    assert "- blocker:" not in text


def test_validate_reports_invalid_requirements_as_blocker_without_command_failure(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)
    _write_spec_output(project_tmp, slug)
    _requirements_path(project_tmp, slug).write_text("not: [valid\n", encoding="utf-8")

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- requirements.yaml: present=yes; readable=yes; schema=invalid" in text
    assert "blocker: requirements.yaml schema invalid: requirements.yaml is malformed YAML:" in text


def test_validate_rejects_invalid_requirement_status_without_command_failure(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)
    _write_spec_output(project_tmp, slug)
    _write_requirements_output(project_tmp, slug, status="verified")

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- requirements.yaml: present=yes; readable=yes; schema=invalid" in text
    assert "blocker: requirements.yaml schema invalid: invalid requirement status: REQ-001 uses verified." in text


def test_validate_rejects_missing_or_incorrect_advisory_fields_without_command_failure(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)
    _write_spec_output(project_tmp, slug)

    base_document = {
        "schema_version": 1,
        "artifact_role": "advisory_projection",
        "authority": "non_authoritative",
        "edit_model": "edit_source_task_artifacts_and_rerun_spec",
        "task_slug": slug,
        "source_model": "deterministic_acceptance_markdown_projection",
        "source_artifacts": [{"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"}],
        "requirements": [],
        "findings": [],
    }
    cases = [
        ("artifact_role", None, "artifact_role must be advisory_projection."),
        ("artifact_role", "implementation_contract", "artifact_role must be advisory_projection."),
        ("authority", None, "authority must be non_authoritative."),
        ("authority", "authoritative", "authority must be non_authoritative."),
        ("edit_model", None, "edit_model must be edit_source_task_artifacts_and_rerun_spec."),
        ("edit_model", "edit_requirements_yaml_directly", "edit_model must be edit_source_task_artifacts_and_rerun_spec."),
    ]

    for field, value, problem in cases:
        document = dict(base_document)
        if value is None:
            document.pop(field)
        else:
            document[field] = value
        _requirements_path(project_tmp, slug).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

        code, _ = run_validate(project_tmp, slug)

        text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
        assert code == 0
        assert "- requirements.yaml: present=yes; readable=yes; schema=invalid" in text
        assert f"blocker: requirements.yaml schema invalid: {problem}" in text


def test_validate_does_not_overwrite_inputs_or_generated_reports(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_preflight(project_tmp, slug)[0] == 0
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    assert run_generate(project_tmp, slug)[0] == 0
    assert run_evidence(project_tmp, slug)[0] == 0
    (project_tmp / "AGENTS.md").write_text("existing agents\n", encoding="utf-8")
    (project_tmp / "CLAUDE.md").write_text("existing claude\n", encoding="utf-8")
    tracked_paths = [
        *(_task_path(project_tmp, slug, filename) for filename in TASK_FILES),
        _task_path(project_tmp, slug, "preflight.md"),
        _task_path(project_tmp, slug, "test-contract-review.md"),
        _workset_path(project_tmp, slug),
        _task_path(project_tmp, slug, "evidence-report.md"),
        project_tmp / "AGENTS.md",
        project_tmp / "CLAUDE.md",
        project_tmp / ".harness" / "generated" / "agent-instructions.md",
    ]
    before = {path: path.read_bytes() for path in tracked_paths}

    code, messages = run_validate(project_tmp, slug, force=True)

    assert code == 0
    assert {path: path.read_bytes() for path in tracked_paths} == before
    assert "root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified" in messages
    assert ".harness/generated/agent-instructions.md was not modified" in messages
    assert ".harness/tasks/validate-me/generated/agent-workset.md was not modified" in messages


def test_validate_dry_run_writes_nothing_and_preserves_manifest(project_tmp):
    slug = _start_sample_task(project_tmp)
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
    assert any("would create validation provenance" in item for item in messages)
    assert not _report_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_validate_rerun_refreshes_manifest_managed_clean_report(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    path_text = f".harness/tasks/{slug}/validation-report.md"
    monkeypatch.setattr("ai_sdlc_harness.validate._timestamp", lambda: "2026-07-08T00:00:00+00:00")
    assert run_validate(project_tmp, slug)[0] == 0
    first_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    monkeypatch.setattr("ai_sdlc_harness.validate._timestamp", lambda: "2026-07-08T00:00:01+00:00")
    code, messages = run_validate(project_tmp, slug)
    second_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    assert code == 0
    assert f"refresh file {path_text}" in messages
    assert first_hash != second_hash


def test_validate_skips_manifest_refresh_when_content_is_byte_identical(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    monkeypatch.setattr("ai_sdlc_harness.validate._timestamp", lambda: "2026-07-08T00:00:00+00:00")
    assert run_validate(project_tmp, slug)[0] == 0
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug)

    assert code == 0
    assert "skip unchanged file .harness/tasks/validate-me/validation-report.md" in messages
    assert "skip existing manifest .harness/manifest.json" in messages
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_validate_does_not_rewrite_hash_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    report = _report_path(project_tmp, slug)
    report.write_text("custom report\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/validate-me/validation-report.md" in messages
    assert report.read_text(encoding="utf-8") == "custom report\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_validate_force_overwrites_only_manifest_managed_report(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    report = _report_path(project_tmp, slug)
    user_note = report.parent / "user-note.md"
    report.write_text("custom report\n", encoding="utf-8")
    user_note.write_text("keep me\n", encoding="utf-8")

    code, messages = run_validate(project_tmp, slug, force=True)

    assert code == 0
    assert "refresh file .harness/tasks/validate-me/validation-report.md" in messages
    assert "custom report" not in report.read_text(encoding="utf-8")
    assert user_note.read_text(encoding="utf-8") == "keep me\n"


def test_validate_force_rejects_mocked_final_report_symlink_before_semantics(
    project_tmp, monkeypatch
):
    slug = _start_sample_task(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    report = _report_path(project_tmp, slug)
    sentinel = project_tmp / "validation-sentinel.md"
    sentinel.write_bytes(b"do not overwrite\n")
    report_before = report.read_bytes()
    sentinel_before = sentinel.read_bytes()
    manifest = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest.read_bytes()
    real_is_symlink = Path.is_symlink

    def report_final_symlink(path):
        return path == report or real_is_symlink(path)

    def fail_capture(*_args, **_kwargs):
        raise AssertionError("semantic snapshot capture must not run")

    monkeypatch.setattr(Path, "is_symlink", report_final_symlink)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate.capture_validation_snapshot", fail_capture
    )

    code, messages = run_validate(project_tmp, slug, force=True)

    rendered = "\n".join(messages)
    assert code == 1
    assert (
        "unsafe managed validation report path: "
        ".harness/tasks/validate-me/validation-report.md"
    ) in messages
    assert str(project_tmp) not in rendered
    assert report.read_bytes() == report_before
    assert sentinel.read_bytes() == sentinel_before
    assert manifest.read_bytes() == manifest_before


def test_validate_dry_run_rejects_mocked_symlinked_report_parent_before_semantics(
    project_tmp, monkeypatch
):
    slug = _start_sample_task(project_tmp)
    report = _report_path(project_tmp, slug)
    linked_parent = report.parent
    manifest = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest.read_bytes()
    real_is_symlink = Path.is_symlink

    def report_parent_symlink(path):
        return path == linked_parent or real_is_symlink(path)

    def fail_capture(*_args, **_kwargs):
        raise AssertionError("semantic snapshot capture must not run")

    monkeypatch.setattr(Path, "is_symlink", report_parent_symlink)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate.capture_validation_snapshot", fail_capture
    )

    code, messages = run_validate(project_tmp, slug, dry_run=True)

    rendered = "\n".join(messages)
    assert code == 1
    assert (
        "unsafe managed validation report path: "
        ".harness/tasks/validate-me/validation-report.md"
    ) in messages
    assert str(project_tmp) not in rendered
    assert not report.exists()
    assert manifest.read_bytes() == manifest_before


def test_validate_semantics_ignore_other_manifest_hash_drift_without_repairing_it(project_tmp):
    slug = _start_sample_task(project_tmp)
    acceptance_path = ".harness/tasks/validate-me/acceptance.md"
    original_hash = _manifest_entry(project_tmp, acceptance_path)["sha256"]
    _task_path(project_tmp, slug, "acceptance.md").write_text("changed acceptance\n", encoding="utf-8")

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "## Manifest / Integrity Signals" not in text
    assert "manifest-managed task artifact" not in text
    assert _manifest_entry(project_tmp, acceptance_path)["sha256"] == original_hash


def test_validate_blocks_malformed_or_non_utf8_main_manifest(project_tmp):
    slug = _start_sample_task(project_tmp)
    manifest = project_tmp / ".harness" / "manifest.json"

    for content in (b"{not json\n", b"\xff\xfe"):
        manifest.write_bytes(content)
        code, messages = run_validate(project_tmp, slug)

        assert code == 1
        assert "Could not read harness manifest" in messages[0]
        assert not _report_path(project_tmp, slug).exists()


def test_validate_reads_each_semantic_dependency_once(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)
    _write_spec_output(project_tmp, slug)
    _write_requirements_output(project_tmp, slug)
    dependency_root = project_tmp / ".harness" / "tasks" / slug
    expected = {
        *(dependency_root / filename for filename in TASK_FILES),
        dependency_root / "preflight.md",
        dependency_root / "spec.md",
        dependency_root / "requirements.yaml",
        dependency_root / "test-contract-review.md",
        dependency_root / "generated" / "agent-workset.md",
        dependency_root / "evidence-report.md",
    }
    reads = {path.resolve(): 0 for path in expected}
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def counted_read_bytes(path):
        resolved = path.resolve()
        if resolved in reads:
            reads[resolved] += 1
        return real_read_bytes(path)

    def counted_read_text(path, *args, **kwargs):
        resolved = path.resolve()
        if resolved in reads:
            reads[resolved] += 1
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    monkeypatch.setattr(Path, "read_text", counted_read_text)

    code, _ = run_validate(project_tmp, slug)

    assert code == 0
    assert set(reads) == {path.resolve() for path in expected}
    assert all(count == 1 for count in reads.values())
    assert reads[(dependency_root / "requirements.yaml").resolve()] == 1


def test_validate_report_counts_and_clean_state_match_inspection_result(
    project_tmp, monkeypatch
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(project_tmp, slug)
    _write_spec_output(project_tmp, slug)
    _write_requirements_output(project_tmp, slug)
    expected = inspect_validation_snapshot(
        capture_validation_snapshot(project_tmp, slug)
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: "2026-09-15T00:00:00+00:00",
    )

    code, messages = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert f"- Current blockers: {expected.blocker_count}" in text
    assert f"- Current warnings: {expected.warning_count}" in text
    assert f"- Current info: {expected.info_count}" in text
    assert (
        f"- Prior report observations shown: {len(expected.report_findings)} "
        "(excluded from current counts)"
    ) in text
    assert f"- CLEAN: {'yes' if expected.clean else 'no'}" in text
    assert f"- Review-readiness answer: {expected.review_readiness}" in text
    assert (
        f"summary: {expected.blocker_count} blocker(s), "
        f"{expected.warning_count} warning(s), {expected.info_count} info"
        in messages
    )


def test_validate_never_overwrites_unmanaged_existing_report(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Blocked validation")[0] == 0
    slug = "blocked-validation"
    report = _report_path(project_tmp, slug)
    report.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug, force=True)

    assert code == 1
    assert "unmanaged existing file .harness/tasks/blocked-validation/validation-report.md" in messages
    assert report.read_text(encoding="utf-8") == "user owned\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_verify_passes_after_validation_report_creation(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_fails_when_manifest_managed_validation_report_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    _report_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/validate-me/validation-report.md" in message for message in messages)


def test_verify_fails_when_manifest_managed_validation_report_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    _report_path(project_tmp, slug).write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/tasks/validate-me/validation-report.md" in message for message in messages)


def test_verify_does_not_require_every_task_to_have_validation_report(project_tmp):
    _start_sample_task(project_tmp)

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_status_reports_manifest_managed_validation_report_count_and_remains_read_only(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    unmanaged_task = project_tmp / ".harness" / "tasks" / "unmanaged-task"
    unmanaged_task.mkdir(parents=True)
    (unmanaged_task / "validation-report.md").write_text("not managed\n", encoding="utf-8")
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    tracked.append(_report_path(project_tmp, slug))
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert "manifest-managed validation reports: 1" in messages
    assert before == after


def _prepare_complete_validation_inputs(root: Path) -> str:
    slug = _start_sample_task(root)
    _fill_task_inputs(root, slug)
    _add_repo_signals(root)
    _write_workflow_outputs(root, slug)
    _write_spec_output(root, slug)
    _write_requirements_output(root, slug)
    return slug


def _validation_provenance(root: Path, slug: str):
    manifest = load_manifest_model(root / ".harness" / "manifest.json")
    assert isinstance(manifest, ManifestV2)
    path_text = f".harness/tasks/{slug}/validation-report.md"
    record = next(
        item
        for item in manifest.generated_artifact_provenance
        if item.output_path == path_text
    )
    return manifest, record


def _install_current_manifest_as_v1(root: Path) -> bytes:
    data = _manifest(root)
    data.pop("manifest_schema_version")
    data.pop("generated_artifact_provenance")
    content = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (root / ".harness" / "manifest.json").write_bytes(content)
    return content


def test_validate_records_exact_validation_provenance(project_tmp, monkeypatch):
    slug = _prepare_complete_validation_inputs(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: "2026-09-17T00:00:00+00:00",
    )

    code, _messages = run_validate(project_tmp, slug)

    manifest, record = _validation_provenance(project_tmp, slug)
    path_text = f".harness/tasks/{slug}/validation-report.md"
    managed = next(item for item in manifest.managed_files if item.path == path_text)
    assert code == 0
    assert record.producer_command == "validate"
    assert record.producer_version == 1
    assert record.task_slug == slug
    assert record.output_sha256 == managed.sha256
    assert record.output_sha256 == hashlib.sha256(
        _report_path(project_tmp, slug).read_bytes()
    ).hexdigest()
    assert tuple(item.dependency_path for item in record.dependencies) == tuple(
        sorted(path.as_posix() for path in validation_dependency_paths(slug))
    )
    assert tuple(
        (item.observation_path, item.predicate)
        for item in record.repository_observations
    ) == tuple(
        sorted(
            (path.as_posix(), predicate.value)
            for path, predicate in validation_observation_definitions(slug)
        )
    )
    assert ".harness/manifest.json" not in {
        item.dependency_path for item in record.dependencies
    }


def test_validate_captures_one_snapshot_for_semantics_and_provenance(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    real_capture = capture_validation_snapshot
    captured = []

    def counted_capture(root, task_slug):
        snapshot = real_capture(root, task_slug)
        captured.append(snapshot)
        return snapshot

    monkeypatch.setattr(
        "ai_sdlc_harness.validate.capture_validation_snapshot",
        counted_capture,
    )

    assert run_validate(project_tmp, slug)[0] == 0

    _manifest_model, record = _validation_provenance(project_tmp, slug)
    assert len(captured) == 1
    snapshot = captured[0]
    by_path = {item.dependency_path: item for item in record.dependencies}
    assert all(
        by_path[item.path.as_posix()].dependency_sha256 == item.sha256
        and by_path[item.path.as_posix()].dependency_state == item.state.value
        for item in snapshot.dependencies
    )


def test_resolve_validation_currentness_current_and_missing(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    report_before = _report_path(project_tmp, slug).read_bytes()
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    current = resolve_validation_currentness(project_tmp, slug)
    assert _report_path(project_tmp, slug).read_bytes() == report_before
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before
    _report_path(project_tmp, slug).unlink()
    missing = resolve_validation_currentness(project_tmp, slug)

    assert current.state is ValidationCurrentnessState.CURRENT
    assert current.findings == ()
    assert missing.state is ValidationCurrentnessState.MISSING
    assert missing.findings[0].level == "warning"


def test_resolve_validation_currentness_missing_provenance_is_unknown_warning(
    project_tmp,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    manifest, _record = _validation_provenance(project_tmp, slug)
    path_text = f".harness/tasks/{slug}/validation-report.md"
    without = remove_provenance_records(
        manifest,
        (path_text,),
        generated_at="remove-validation-provenance",
    )
    persist_manifest_model(project_tmp, without)

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.UNKNOWN
    assert result.findings[0].code == "validation_provenance_missing"
    assert result.findings[0].level == "warning"
    assert "ai-sdlc validate --task validate-me" in result.findings[0].message


def test_resolve_validation_currentness_v1_is_unknown_without_migration(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    legacy_bytes = _install_current_manifest_as_v1(project_tmp)

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.UNKNOWN
    assert result.findings[0].code == "legacy_validation_manifest"
    assert result.findings[0].level == "warning"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == legacy_bytes


def test_v1_validation_report_hash_drift_is_invalid(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    _install_current_manifest_as_v1(project_tmp)
    _report_path(project_tmp, slug).write_bytes(b"legacy drift\n")

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.INVALID
    assert result.findings[0].code == "validation_report_hash_drift"


@pytest.mark.parametrize("dependency_index", range(13))
def test_each_changed_validation_dependency_is_stale(
    project_tmp,
    dependency_index,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    changed_path = project_tmp.joinpath(
        *validation_dependency_paths(slug)[dependency_index].parts
    )
    changed_path.write_bytes(changed_path.read_bytes() + b"\nchanged")

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.STALE
    assert result.findings[0].level == "warning"


def test_changed_validation_observation_is_stale(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    (project_tmp / "tests").rmdir()

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.STALE


def test_old_validation_producer_version_is_stale(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    manifest, record = _validation_provenance(project_tmp, slug)
    changed = tuple(
        replace(item, producer_version=2) if item == record else item
        for item in manifest.generated_artifact_provenance
    )
    persist_manifest_model(
        project_tmp,
        replace(manifest, generated_artifact_provenance=changed),
    )

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.STALE


def test_manifest_generated_at_does_not_control_validation_currentness(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    manifest, _record = _validation_provenance(project_tmp, slug)
    persist_manifest_model(
        project_tmp,
        replace(manifest, generated_at="2099-01-01T00:00:00+00:00"),
    )

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.CURRENT


def test_uninspectable_current_dependency_is_unknown_blocker(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    blocked_path = project_tmp.joinpath(*validation_dependency_paths(slug)[0].parts)
    real_read_bytes = Path.read_bytes

    def controlled_read_bytes(path):
        if path == blocked_path:
            raise PermissionError("simulated dependency access failure")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", controlled_read_bytes)

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.UNKNOWN
    assert result.findings[0].code == "validation_dependency_uninspectable"
    assert result.findings[0].level == "blocker"
    assert str(project_tmp) not in result.findings[0].message


def test_uninspectable_current_observation_is_unknown_blocker(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    blocked_path = project_tmp / "tests"
    real_is_dir = Path.is_dir

    def controlled_is_dir(path):
        if path == blocked_path:
            raise PermissionError("simulated observation access failure")
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", controlled_is_dir)

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.UNKNOWN
    assert any(
        item.code == "validation_observation_uninspectable"
        and item.level == "blocker"
        for item in result.findings
    )


def test_invalid_validation_provenance_is_invalid_currentness(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    manifest, record = _validation_provenance(project_tmp, slug)
    changed = tuple(
        replace(item, producer_command="not-validate")
        if item == record
        else item
        for item in manifest.generated_artifact_provenance
    )
    persist_manifest_model(
        project_tmp,
        replace(manifest, generated_artifact_provenance=changed),
    )

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.INVALID
    assert result.findings[0].code == "validation_provenance_invalid"


def test_validation_report_hash_drift_is_invalid_currentness(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    _report_path(project_tmp, slug).write_bytes(b"drifted report\n")

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.INVALID
    assert result.findings[0].code == "validation_report_hash_drift"


def test_malformed_manifest_is_invalid_validation_currentness(project_tmp):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    (project_tmp / ".harness" / "manifest.json").write_text("{bad json\n")

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.INVALID
    assert result.findings[0].code == "validation_manifest_invalid"


def test_unreadable_manifest_is_invalid_validation_currentness(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    real_read_text = Path.read_text

    def controlled_read_text(path, *args, **kwargs):
        if path == manifest_path:
            raise PermissionError("simulated unreadable manifest")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", controlled_read_text)

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.INVALID
    assert result.findings[0].code == "validation_manifest_invalid"


def test_unsafe_validation_report_path_is_invalid_currentness(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    report = _report_path(project_tmp, slug)
    real_is_symlink = Path.is_symlink

    def controlled_is_symlink(path):
        return path == report or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", controlled_is_symlink)

    result = resolve_validation_currentness(project_tmp, slug)

    assert result.state is ValidationCurrentnessState.INVALID
    assert result.findings[0].code == "validation_report_path_unsafe"


def test_currentness_resolver_captures_validation_snapshot_once(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_validate(project_tmp, slug)[0] == 0
    real_capture = capture_validation_snapshot
    calls = 0

    def counted_capture(root, task_slug):
        nonlocal calls
        calls += 1
        return real_capture(root, task_slug)

    monkeypatch.setattr(
        "ai_sdlc_harness.validation.capture_validation_snapshot",
        counted_capture,
    )

    assert resolve_validation_currentness(project_tmp, slug).state is (
        ValidationCurrentnessState.CURRENT
    )
    assert calls == 1


def test_unchanged_report_with_missing_provenance_refreshes_manifest_only(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: "2026-09-17T01:00:00+00:00",
    )
    assert run_validate(project_tmp, slug)[0] == 0
    manifest, _record = _validation_provenance(project_tmp, slug)
    path_text = f".harness/tasks/{slug}/validation-report.md"
    persist_manifest_model(
        project_tmp,
        remove_provenance_records(
            manifest,
            (path_text,),
            generated_at="remove-validation-provenance",
        ),
    )
    report_before = _report_path(project_tmp, slug).read_bytes()
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug)

    assert code == 0
    assert f"skip unchanged file {path_text}" in messages
    assert f"create validation provenance for {path_text}" in messages
    assert _report_path(project_tmp, slug).read_bytes() == report_before
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() != manifest_before
    assert resolve_validation_currentness(project_tmp, slug).state is (
        ValidationCurrentnessState.CURRENT
    )


def test_unchanged_report_with_old_producer_refreshes_manifest_only(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: "2026-09-17T02:00:00+00:00",
    )
    assert run_validate(project_tmp, slug)[0] == 0
    manifest, record = _validation_provenance(project_tmp, slug)
    persist_manifest_model(
        project_tmp,
        replace(
            manifest,
            generated_artifact_provenance=tuple(
                replace(item, producer_version=2) if item == record else item
                for item in manifest.generated_artifact_provenance
            ),
        ),
    )
    report_before = _report_path(project_tmp, slug).read_bytes()

    code, messages = run_validate(project_tmp, slug)

    assert code == 0
    assert "skip unchanged file .harness/tasks/validate-me/validation-report.md" in messages
    assert "refresh validation provenance for .harness/tasks/validate-me/validation-report.md" in messages
    assert _report_path(project_tmp, slug).read_bytes() == report_before
    assert resolve_validation_currentness(project_tmp, slug).state is (
        ValidationCurrentnessState.CURRENT
    )


def test_stale_provenance_dry_run_plans_refresh_and_writes_nothing(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: "2026-09-17T03:00:00+00:00",
    )
    assert run_validate(project_tmp, slug)[0] == 0
    manifest, record = _validation_provenance(project_tmp, slug)
    persist_manifest_model(
        project_tmp,
        replace(
            manifest,
            generated_artifact_provenance=tuple(
                replace(item, producer_version=2) if item == record else item
                for item in manifest.generated_artifact_provenance
            ),
        ),
    )
    report_before = _report_path(project_tmp, slug).read_bytes()
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug, dry_run=True)

    assert code == 0
    assert any("would refresh validation provenance" in item for item in messages)
    assert _report_path(project_tmp, slug).read_bytes() == report_before
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_fully_converged_dry_run_plans_complete_noop(project_tmp, monkeypatch):
    slug = _prepare_complete_validation_inputs(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: "2026-09-17T04:00:00+00:00",
    )
    assert run_validate(project_tmp, slug)[0] == 0
    report_before = _report_path(project_tmp, slug).read_bytes()
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug, dry_run=True)

    assert code == 0
    assert any("would skip unchanged file" in item for item in messages)
    assert any("would skip current validation provenance" in item for item in messages)
    assert _report_path(project_tmp, slug).read_bytes() == report_before
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_validation_producer_upgrades_v1_only_on_success(project_tmp, monkeypatch):
    slug = _prepare_complete_validation_inputs(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: "2026-09-17T05:00:00+00:00",
    )
    assert run_validate(project_tmp, slug)[0] == 0
    _install_current_manifest_as_v1(project_tmp)

    assert run_validate(project_tmp, slug, dry_run=True)[0] == 0
    assert not isinstance(
        load_manifest_model(project_tmp / ".harness" / "manifest.json"),
        ManifestV2,
    )
    assert run_validate(project_tmp, slug)[0] == 0
    upgraded = load_manifest_model(project_tmp / ".harness" / "manifest.json")
    assert isinstance(upgraded, ManifestV2)
    assert any(
        item.output_path.endswith("/validation-report.md")
        for item in upgraded.generated_artifact_provenance
    )


def test_invalidation_manifest_failure_leaves_report_untouched(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    timestamps = iter(
        ("2026-09-17T06:00:00+00:00", "2026-09-17T06:00:01+00:00")
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: next(timestamps),
    )
    assert run_validate(project_tmp, slug)[0] == 0
    report_before = _report_path(project_tmp, slug).read_bytes()
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    def fail_persist(_root, _document):
        raise OSError("simulated invalidation failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.validate.persist_manifest_model",
        fail_persist,
    )

    code, _messages = run_validate(project_tmp, slug)

    assert code == 1
    assert _report_path(project_tmp, slug).read_bytes() == report_before
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_report_persistence_failure_leaves_old_validation_provenance_absent(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    timestamps = iter(
        ("2026-09-17T07:00:00+00:00", "2026-09-17T07:00:01+00:00")
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: next(timestamps),
    )
    assert run_validate(project_tmp, slug)[0] == 0
    report_before = _report_path(project_tmp, slug).read_bytes()

    def fail_report(_path, _content):
        raise OSError("simulated report failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.validate.persist_exact_bytes",
        fail_report,
    )

    code, _messages = run_validate(project_tmp, slug)

    manifest = load_manifest_model(project_tmp / ".harness" / "manifest.json")
    assert isinstance(manifest, ManifestV2)
    assert code == 1
    assert _report_path(project_tmp, slug).read_bytes() == report_before
    assert not any(
        item.output_path.endswith("/validation-report.md")
        for item in manifest.generated_artifact_provenance
    )


def test_final_manifest_failure_leaves_changed_report_without_old_provenance(
    project_tmp,
    monkeypatch,
):
    slug = _prepare_complete_validation_inputs(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    timestamps = iter(
        ("2026-09-17T08:00:00+00:00", "2026-09-17T08:00:01+00:00")
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.validate._timestamp",
        lambda: next(timestamps),
    )
    assert run_validate(project_tmp, slug)[0] == 0
    before_manifest, _record = _validation_provenance(project_tmp, slug)
    unrelated = tuple(
        item
        for item in before_manifest.generated_artifact_provenance
        if not item.output_path.endswith("/validation-report.md")
    )
    report_before = _report_path(project_tmp, slug).read_bytes()
    persistence_calls = 0

    def fail_final(root, document):
        nonlocal persistence_calls
        persistence_calls += 1
        if persistence_calls == 2:
            raise OSError("simulated final manifest failure")
        persist_manifest_model(root, document)

    monkeypatch.setattr(
        "ai_sdlc_harness.validate.persist_manifest_model",
        fail_final,
    )

    code, _messages = run_validate(project_tmp, slug)

    manifest = load_manifest_model(project_tmp / ".harness" / "manifest.json")
    assert isinstance(manifest, ManifestV2)
    assert code == 1
    assert _report_path(project_tmp, slug).read_bytes() != report_before
    assert not any(
        item.output_path.endswith("/validation-report.md")
        for item in manifest.generated_artifact_provenance
    )
    assert manifest.generated_artifact_provenance == unrelated
