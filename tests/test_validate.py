from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

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
from ai_sdlc_harness.verify import verify_project
from ai_sdlc_harness.manifest import write_manifest


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
    assert "## Prior Report Findings" in text
    assert "`preflight.md`: present=yes; readable=yes" in text
    assert "`spec.md`: present=no; readable=no" in text
    assert "`requirements.yaml`: present=no; readable=no" in text
    assert "`test-contract-review.md`: present=yes; readable=yes" in text
    assert "`generated/agent-workset.md`: present=yes; readable=yes" in text
    assert "`evidence-report.md`: present=yes; readable=yes" in text
    assert "## Manifest / Integrity Signals" in text
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


def test_validate_labels_and_deduplicates_copied_prior_report_findings(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _add_repo_signals(project_tmp)
    _write_workflow_outputs(
        project_tmp,
        slug,
        findings={
            "preflight.md": "- blocker: boundary issue remains.\n- warning: shared issue remains.\n- info: preflight noted context.\n",
            "test-contract-review.md": "- warning: shared issue remains.\n",
            "evidence-report.md": "- warning: evidence issue remains.\n",
            "generated/agent-workset.md": "- blocker: generated workset blocker.\n",
        },
    )

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Review-readiness answer: No" in text
    assert text.count("shared issue remains.") == 2
    assert "`preflight.md`: blocker: boundary issue remains." in text
    assert "`preflight.md`: warning: shared issue remains." in text
    assert "`test-contract-review.md`: warning: shared issue remains." not in text
    assert "`preflight.md`: info: preflight noted context." in text
    assert "`evidence-report.md`: warning: evidence issue remains." in text
    assert "- blocker: preflight.md reported blocker: boundary issue remains." in text
    assert "- warning: preflight.md reported warning: shared issue remains." in text
    assert "- warning: evidence-report.md reported warning: evidence issue remains." in text
    assert "- info: preflight.md reported info: preflight noted context." in text
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
    assert "root AGENTS.md and CLAUDE.md were not modified" in messages
    assert ".harness/generated/agent-instructions.md was not modified" in messages
    assert ".harness/tasks/validate-me/generated/agent-workset.md was not modified" in messages


def test_validate_dry_run_writes_nothing_and_preserves_manifest(project_tmp):
    slug = _start_sample_task(project_tmp)
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_validate(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
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


def test_validate_reports_other_manifest_drift_without_repairing_it(project_tmp):
    slug = _start_sample_task(project_tmp)
    acceptance_path = ".harness/tasks/validate-me/acceptance.md"
    original_hash = _manifest_entry(project_tmp, acceptance_path)["sha256"]
    _task_path(project_tmp, slug, "acceptance.md").write_text("changed acceptance\n", encoding="utf-8")

    code, _ = run_validate(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert f"`{acceptance_path}`: status=present and readable; hash=hash drift" in text
    assert f"blocker: manifest-managed task artifact has hash drift: {acceptance_path}." in text
    assert _manifest_entry(project_tmp, acceptance_path)["sha256"] == original_hash


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
