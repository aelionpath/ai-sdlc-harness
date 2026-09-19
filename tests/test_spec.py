from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from ai_sdlc_harness import spec as spec_module
from ai_sdlc_harness.cli import main
from ai_sdlc_harness.files import PathSafetyError
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.spec import run_spec
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.verify import verify_project
from tests.lineage_test_helpers import (
    install_released_v1_with_current_task_records,
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


def _spec_path(root: Path, slug: str) -> Path:
    return _task_path(root, slug, "spec.md")


def _requirements_path(root: Path, slug: str) -> Path:
    return _task_path(root, slug, "requirements.yaml")


def _manifest(root: Path) -> dict:
    return json.loads((root / ".harness" / "manifest.json").read_text(encoding="utf-8"))


def _manifest_entry(root: Path, path: str) -> dict:
    entries = {entry["path"]: entry for entry in _manifest(root)["managed_files"]}
    return entries[path]


def _start_sample_task(root: Path, title: str = "Spec me") -> str:
    assert init_project(root)[0] == 0
    assert start_task(root, title)[0] == 0
    return "spec-me"


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        if os.environ.get("AI_SDLC_REQUIRE_REAL_SYMLINKS") == "1":
            pytest.fail(f"real symlink creation is required but unavailable: {exc}", pytrace=False)
        pytest.skip(f"symlink creation is unavailable: {exc}")


def _fill_task_inputs(root: Path, slug: str) -> None:
    _task_path(root, slug, "task.md").write_text(
        """# Task: Spec me

Task slug: `spec-me`

## Implementation Boundary

Add the deterministic spec command only.

## Assumptions And Open Questions

No open questions.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- spec.md is generated for the selected task.
- Weak source artifacts become findings in spec.md.

## Protected Behavior And Non-Goals

- Do not create JSON spec artifacts.
- No protected behavior changes beyond the new command.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "architecture-notes.md").write_text(
        """# Architecture Notes

## Boundary

Keep the implementation inside harness command/report plumbing.

## Responsibility Change

Spec consolidates implementation intent before workset generation.

## Existing Patterns To Preserve

Preserve manifest-managed report command behavior.

## Interface And Compatibility Impact

Add `ai-sdlc spec --task <slug>` without changing existing commands.

## Architecture Hygiene

Use lightweight Markdown heading extraction only.

## Security And Privacy Risk Surface

No security/privacy-sensitive surface identified.

## Trade-Offs And Open Questions

None.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "coupling-notes.md").write_text(
        """# Coupling Notes

## New Or Changed Coupling

Spec becomes a generated task artifact tracked by the manifest.

## Maintainability Sensors

Keep the first stage Markdown-only and deterministic.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Existing report commands remain non-destructive.

## Desired Behavior Tests

- spec.md includes the requested sections.

## Regression Tests

- verify detects hash drift.

## Negative And Edge Cases

- Unmanaged spec.md is refused.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run

py -m pytest

## Results

Record results after implementation.

## Not Run / Why

No tests run because this is source fixture text.

## Manual Review Notes

Review generated Markdown wording.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

Record final implementation evidence.

## Generated Or Updated Artifacts

Record generated or updated artifacts.

## Tests And Checks Run

Record tests and checks run.

## Results

Record command results.

## Known Gaps And Risks

Record known gaps and risks.

## References

Record relevant references.
""",
        encoding="utf-8",
    )


def test_cli_dispatches_spec_command(project_tmp, capsys):
    slug = _start_sample_task(project_tmp)

    current = Path.cwd()
    try:
        import os

        os.chdir(project_tmp)
        code = main(["spec", "--task", slug, "--dry-run"])
    finally:
        os.chdir(current)

    output = capsys.readouterr().out
    assert code == 0
    assert "spec report: .harness/tasks/spec-me/spec.md" in output
    assert "dry run; no files written" in output


def test_spec_fails_before_init(project_tmp):
    code, messages = run_spec(project_tmp, "missing-task")

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_spec_fails_if_task_does_not_exist(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_spec(project_tmp, "missing-task")

    assert code == 1
    assert "Task folder does not exist: .harness/tasks/missing-task" in messages[0]


def test_spec_rejects_unsafe_task_slug(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_spec(project_tmp, "../bad")

    assert code == 2
    assert "Task slug is unsafe" in messages[0]


def test_spec_uses_task_start_title_and_preserves_task_inputs(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Add validation for negative numbers")[0] == 0
    slug = "add-validation-for-negative-numbers"
    tracked = [_task_path(project_tmp, slug, filename) for filename in TASK_FILES]
    before = {path: path.read_bytes() for path in tracked}

    code, _ = run_spec(project_tmp, slug)

    text = _spec_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Task title: Add validation for negative numbers" in text
    assert "- Task title: Default Workflow Context" not in text
    assert {path: path.read_bytes() for path in tracked} == before


def test_spec_falls_back_to_slug_title_when_task_title_is_missing(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Add validation for negative numbers")[0] == 0
    slug = "add-validation-for-negative-numbers"
    task = _task_path(project_tmp, slug, "task.md")
    task.write_text(
        """# Task

## Default Workflow Context

This task file no longer carries an explicit title.

## Implementation Boundary

TODO: Describe the expected change area.
""",
        encoding="utf-8",
    )
    before = task.read_bytes()

    code, _ = run_spec(project_tmp, slug)

    text = _spec_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Task title: Add validation for negative numbers" in text
    assert "- Task title: Default Workflow Context" not in text
    assert task.read_bytes() == before


def test_spec_creates_markdown_with_sections_findings_and_manifest_entry(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "preflight.md").write_text(
        """# Preflight

- blocker: boundary must be accepted.
- warning: open question remains.
- info: preflight uses shallow checks only.
""",
        encoding="utf-8",
    )

    code, messages = run_spec(project_tmp, slug)

    path = _spec_path(project_tmp, slug)
    req_path = _requirements_path(project_tmp, slug)
    text = path.read_text(encoding="utf-8")
    requirements = yaml.safe_load(req_path.read_text(encoding="utf-8"))
    assert code == 0
    assert f"spec report: .harness/tasks/{slug}/spec.md" in messages
    assert f"requirements file: .harness/tasks/{slug}/requirements.yaml" in messages
    assert path.is_file()
    assert req_path.is_file()
    for heading in (
        "## Summary",
        "## Implementation Intent",
        "## Structured Requirements",
        "## Task Boundary",
        "## Requirements / Acceptance Criteria",
        "## Protected Behavior / Non-Goals",
        "## Architecture / Interface Impact",
        "## Coupling / Maintainability Notes",
        "## Security / Privacy Risk-Surface Notes",
        "## Test Expectations",
        "## Verification Record",
        "## Evidence Expectations",
        "## Open Questions",
        "## Preflight Signals",
        "## Findings",
        "## Recommended Next Actions",
        "## Disclaimer",
    ):
        assert heading in text
    assert "- Spec question: Is the implementation intent clear enough to generate a useful workset and guide a coding agent?" in text
    assert "- Implementation-intent answer: No" in text
    assert "Source: `task.md`" in text
    assert "Add the deterministic spec command only." in text
    assert "Source: `preflight.md`" in text
    assert "- blocker: preflight.md reported blocker: boundary must be accepted." in text
    assert "- warning: preflight.md reported warning: open question remains." in text
    assert "- info: preflight.md reported info: preflight uses shallow checks only." in text
    assert "- info: spec generated deterministic Markdown consolidation." in text
    assert "- info: spec does not call AI models." in text
    assert "- info: spec does not generate requirements automatically." in text
    assert "- info: spec does not validate correctness, security, or compliance." in text
    assert "- info: spec does not run tests." in text
    assert "- info: spec does not enforce packs." in text
    assert "Generated artifact: `.harness/tasks/spec-me/requirements.yaml`" in text
    assert "Requirement count: 2" in text
    assert "Finding count: 0" in text
    assert "- Artifact role: advisory projection." in text
    assert "- Authority: non-authoritative." in text
    assert "`requirements.yaml` is not authoritative." in text
    assert "requirements.md" not in text
    assert "semantic enforcement" not in text.lower()
    assert requirements == {
        "schema_version": 1,
        "artifact_role": "advisory_projection",
        "authority": "non_authoritative",
        "edit_model": "edit_source_task_artifacts_and_rerun_spec",
        "task_slug": "spec-me",
        "source_model": "deterministic_acceptance_markdown_projection",
        "source_artifacts": [{"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"}],
        "requirements": [
            {
                "id": "REQ-001",
                "statement": "spec.md is generated for the selected task.",
                "status": "draft",
                "source": {"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"},
                "acceptance_criteria": [],
                "verification": [],
            },
            {
                "id": "REQ-002",
                "statement": "Weak source artifacts become findings in spec.md.",
                "status": "draft",
                "source": {"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"},
                "acceptance_criteria": [],
                "verification": [],
            },
        ],
        "findings": [],
    }
    requirements_text = req_path.read_text(encoding="utf-8")
    assert "generated_at" not in requirements_text
    assert "timestamp" not in requirements_text.lower()

    entry = _manifest_entry(project_tmp, f".harness/tasks/{slug}/spec.md")
    assert entry["protected"] is True
    assert entry["hash_algorithm"] == "sha256"
    assert entry["sha256"]
    req_entry = _manifest_entry(project_tmp, f".harness/tasks/{slug}/requirements.yaml")
    assert req_entry["protected"] is True
    assert req_entry["hash_algorithm"] == "sha256"
    assert req_entry["sha256"]
    provenance = {
        record["output_path"]: record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
    }
    spec_record = provenance[f".harness/tasks/{slug}/spec.md"]
    requirements_record = provenance[
        f".harness/tasks/{slug}/requirements.yaml"
    ]
    assert spec_record["output_sha256"] == hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    assert requirements_record["output_sha256"] == hashlib.sha256(
        req_path.read_bytes()
    ).hexdigest()
    assert spec_record["dependencies"] == requirements_record["dependencies"]
    assert f".harness/tasks/{slug}/requirements.yaml" not in {
        item["dependency_path"] for item in spec_record["dependencies"]
    }
    assert f".harness/tasks/{slug}/spec.md" not in {
        item["dependency_path"] for item in requirements_record["dependencies"]
    }
    assert spec_record["repository_observations"] == []
    assert requirements_record["repository_observations"] == []


def test_spec_records_weak_inputs_as_findings_without_command_failure(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "acceptance.md").unlink()

    code, _ = run_spec(project_tmp, slug)

    text = _spec_path(project_tmp, slug).read_text(encoding="utf-8")
    requirements = yaml.safe_load(_requirements_path(project_tmp, slug).read_text(encoding="utf-8"))
    assert code == 0
    assert "- Implementation-intent answer: No" in text
    assert "- blocker: acceptance.md is missing." in text
    assert "- blocker: implementation boundary is TODO-only." in text
    assert "- blocker: requirements / acceptance criteria are missing source." in text
    assert "- warning: preflight.md is missing." in text
    assert requirements["requirements"] == []
    assert requirements["findings"] == [{"level": "warning", "message": "acceptance.md contains no explicit requirement list items."}]


def test_spec_extracts_only_explicit_top_level_requirement_items(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

This prose paragraph should not become a requirement.

- Validate user settings input before saving it.
* Reject unsupported input types.
1. Preserve existing error behavior.
  - Nested detail should not become a requirement.
- TODO: add another requirement later.

## Protected Behavior And Non-Goals

- Preserve existing settings read behavior.
""",
        encoding="utf-8",
    )

    code, _ = run_spec(project_tmp, slug)

    requirements = yaml.safe_load(_requirements_path(project_tmp, slug).read_text(encoding="utf-8"))
    assert code == 0
    assert [item["id"] for item in requirements["requirements"]] == ["REQ-001", "REQ-002", "REQ-003"]
    assert [item["statement"] for item in requirements["requirements"]] == [
        "Validate user settings input before saving it.",
        "Reject unsupported input types.",
        "Preserve existing error behavior.",
    ]
    assert all(item["status"] == "draft" for item in requirements["requirements"])
    assert requirements["findings"] == []


def test_spec_prose_only_acceptance_does_not_create_requirements(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

The implementation should validate settings carefully. This prose is intentionally not split.

## Protected Behavior And Non-Goals

- Preserve existing settings read behavior.
""",
        encoding="utf-8",
    )

    code, _ = run_spec(project_tmp, slug)

    requirements = yaml.safe_load(_requirements_path(project_tmp, slug).read_text(encoding="utf-8"))
    assert code == 0
    assert requirements["requirements"] == []
    assert requirements["findings"] == [{"level": "warning", "message": "acceptance.md contains no explicit requirement list items."}]


def test_spec_treats_missing_preflight_as_warning_only(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)

    code, _ = run_spec(project_tmp, slug)

    text = _spec_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Implementation-intent answer: Caution" in text
    assert "- warning: preflight.md is missing." in text
    assert "- blocker:" not in text


def test_spec_dry_run_writes_nothing_and_preserves_manifest(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_spec(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
    assert not _spec_path(project_tmp, slug).exists()
    assert not _requirements_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_spec_skips_manifest_refresh_when_content_is_byte_identical(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr("ai_sdlc_harness.spec._timestamp", lambda: "2026-07-10T00:00:00+00:00")
    assert run_spec(project_tmp, slug)[0] == 0
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_spec(project_tmp, slug)

    assert code == 0
    assert "skip unchanged file .harness/tasks/spec-me/spec.md" in messages
    assert "skip unchanged file .harness/tasks/spec-me/requirements.yaml" in messages
    assert "skip existing manifest .harness/manifest.json" in messages
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before
    assert len(_manifest(project_tmp)["generated_artifact_provenance"]) == 2


def test_spec_never_overwrites_unmanaged_existing_spec(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    spec = _spec_path(project_tmp, slug)
    spec.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_spec(project_tmp, slug, force=True)

    assert code == 1
    assert "unmanaged existing file .harness/tasks/spec-me/spec.md" in messages
    assert spec.read_text(encoding="utf-8") == "user owned\n"
    assert not _requirements_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_spec_never_overwrites_unmanaged_existing_requirements(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    requirements = _requirements_path(project_tmp, slug)
    requirements.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_spec(project_tmp, slug, force=True)

    assert code == 1
    assert "unmanaged existing file .harness/tasks/spec-me/requirements.yaml" in messages
    assert requirements.read_text(encoding="utf-8") == "user owned\n"
    assert not _spec_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_spec_does_not_rewrite_hash_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    spec = _spec_path(project_tmp, slug)
    spec.write_text("custom spec\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_spec(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/spec-me/spec.md" in messages
    assert spec.read_text(encoding="utf-8") == "custom spec\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_spec_does_not_rewrite_requirements_hash_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    spec_before = _spec_path(project_tmp, slug).read_text(encoding="utf-8")
    requirements = _requirements_path(project_tmp, slug)
    requirements.write_text("custom requirements\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_spec(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/spec-me/requirements.yaml" in messages
    assert requirements.read_text(encoding="utf-8") == "custom requirements\n"
    assert _spec_path(project_tmp, slug).read_text(encoding="utf-8") == spec_before
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_spec_force_overwrites_only_manifest_managed_spec(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    spec = _spec_path(project_tmp, slug)
    requirements = _requirements_path(project_tmp, slug)
    user_note = spec.parent / "user-note.md"
    spec.write_text("custom spec\n", encoding="utf-8")
    requirements.write_text("custom requirements\n", encoding="utf-8")
    user_note.write_text("keep me\n", encoding="utf-8")

    code, messages = run_spec(project_tmp, slug, force=True)

    assert code == 0
    assert "refresh file .harness/tasks/spec-me/spec.md" in messages
    assert "refresh file .harness/tasks/spec-me/requirements.yaml" in messages
    assert "custom spec" not in spec.read_text(encoding="utf-8")
    assert "custom requirements" not in requirements.read_text(encoding="utf-8")
    assert user_note.read_text(encoding="utf-8") == "keep me\n"


def test_spec_does_not_modify_inputs_agent_files_or_workset(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    (project_tmp / "AGENTS.md").write_text("existing agents\n", encoding="utf-8")
    (project_tmp / "CLAUDE.md").write_text("existing claude\n", encoding="utf-8")
    workset = project_tmp / ".harness" / "tasks" / slug / "generated" / "agent-workset.md"
    workset.parent.mkdir()
    workset.write_text("existing workset\n", encoding="utf-8")
    tracked = [
        *(_task_path(project_tmp, slug, filename) for filename in TASK_FILES),
        project_tmp / "AGENTS.md",
        project_tmp / "CLAUDE.md",
        project_tmp / ".harness" / "generated" / "agent-instructions.md",
        workset,
    ]
    before = {path: path.read_bytes() for path in tracked}

    code, messages = run_spec(project_tmp, slug, force=True)

    assert code == 0
    assert {path: path.read_bytes() for path in tracked} == before
    assert "root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified" in messages
    assert ".harness/generated/agent-instructions.md was not modified" in messages
    assert ".harness/tasks/spec-me/generated/agent-workset.md was not modified" in messages


def test_verify_fails_when_manifest_managed_spec_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    _spec_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/spec-me/spec.md" in message for message in messages)


def test_verify_fails_when_manifest_managed_spec_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    _spec_path(project_tmp, slug).write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/tasks/spec-me/spec.md" in message for message in messages)


def test_verify_fails_when_manifest_managed_requirements_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    _requirements_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/spec-me/requirements.yaml" in message for message in messages)


def test_verify_fails_when_manifest_managed_requirements_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    _requirements_path(project_tmp, slug).write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/tasks/spec-me/requirements.yaml" in message for message in messages)


def test_verify_does_not_require_every_task_to_have_spec(project_tmp):
    _start_sample_task(project_tmp)

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_status_reports_manifest_managed_spec_count_and_remains_read_only(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0
    unmanaged_task = project_tmp / ".harness" / "tasks" / "unmanaged-task"
    unmanaged_task.mkdir(parents=True)
    (unmanaged_task / "spec.md").write_text("not managed\n", encoding="utf-8")
    (unmanaged_task / "requirements.yaml").write_text("not managed\n", encoding="utf-8")
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    tracked.append(_spec_path(project_tmp, slug))
    tracked.append(_requirements_path(project_tmp, slug))
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert "manifest-managed spec reports: 1" in messages
    assert "manifest-managed requirements files: 1" in messages
    assert before == after


def test_spec_failure_after_first_write_records_no_new_sibling_provenance(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr(
        "ai_sdlc_harness.spec._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_spec(project_tmp, slug)[0] == 0
    acceptance = _task_path(project_tmp, slug, "acceptance.md")
    acceptance.write_text(
        acceptance.read_text(encoding="utf-8").replace(
            "- Weak source artifacts become findings in spec.md.",
            "- Weak source artifacts become findings in spec.md.\n"
            "- Both sibling outputs change for this failure test.",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.spec._timestamp",
        lambda: "2026-07-29T00:00:01+00:00",
    )
    real_persist = spec_module.persist_exact_bytes
    calls = 0

    def fail_second(path, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_persist(path, content)
        records = _manifest(project_tmp)["generated_artifact_provenance"]
        sibling_paths = {
            f".harness/tasks/{slug}/spec.md",
            f".harness/tasks/{slug}/requirements.yaml",
        }
        assert not sibling_paths.intersection(
            record["output_path"] for record in records
        )
        raise OSError("simulated second output failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.spec.persist_exact_bytes",
        fail_second,
    )

    code, messages = run_spec(project_tmp, slug)

    assert code == 1
    assert "simulated second output failure" in messages[0]
    assert not _manifest(project_tmp)["generated_artifact_provenance"]


def test_spec_invalidates_only_changed_sibling_before_writes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr(
        "ai_sdlc_harness.spec._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_spec(project_tmp, slug)[0] == 0
    monkeypatch.setattr(
        "ai_sdlc_harness.spec._timestamp",
        lambda: "2026-07-29T00:00:01+00:00",
    )

    def fail_first(_path, _content):
        records = {
            record["output_path"]: record
            for record in _manifest(project_tmp)[
                "generated_artifact_provenance"
            ]
        }
        assert f".harness/tasks/{slug}/spec.md" not in records
        assert f".harness/tasks/{slug}/requirements.yaml" in records
        raise OSError("simulated first output failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.spec.persist_exact_bytes",
        fail_first,
    )

    code, messages = run_spec(project_tmp, slug)

    assert code == 1
    assert "simulated first output failure" in messages[0]


def test_successful_spec_migrates_v1_and_records_both_outputs(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    legacy = install_released_v1_with_current_task_records(project_tmp)
    managed_before = {
        record["path"]: record for record in legacy["managed_files"]
    }

    assert run_spec(project_tmp, slug)[0] == 0

    migrated = _manifest(project_tmp)
    assert migrated["manifest_schema_version"] == 2
    after_managed = {
        record["path"]: record for record in migrated["managed_files"]
    }
    for path, record in managed_before.items():
        assert after_managed[path] == record
    assert {
        record["output_path"]
        for record in migrated["generated_artifact_provenance"]
    } == {
        f".harness/tasks/{slug}/spec.md",
        f".harness/tasks/{slug}/requirements.yaml",
    }


def test_spec_preserves_unrelated_preflight_provenance(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_preflight(project_tmp, slug)[0] == 0
    preflight_path = f".harness/tasks/{slug}/preflight.md"
    before = next(
        record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"] == preflight_path
    )

    assert run_spec(project_tmp, slug)[0] == 0

    after = next(
        record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"] == preflight_path
    )
    assert after == before


def test_spec_rendering_uses_the_exact_captured_acceptance_bytes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    acceptance = _task_path(project_tmp, slug, "acceptance.md")
    captured_bytes = acceptance.read_bytes()
    real_capture = spec_module.capture_dependencies

    def capture_then_change(root, expected):
        captures = real_capture(root, expected)
        acceptance.write_text(
            "# Acceptance\n\n## Requirements And Acceptance Criteria\n\n"
            "- Changed after capture.\n",
            encoding="utf-8",
        )
        return captures

    monkeypatch.setattr(
        "ai_sdlc_harness.spec.capture_dependencies",
        capture_then_change,
    )

    code, _messages = run_spec(project_tmp, slug)

    assert code == 0
    requirements = yaml.safe_load(
        _requirements_path(project_tmp, slug).read_text(encoding="utf-8")
    )
    assert [
        item["statement"] for item in requirements["requirements"]
    ] == [
        "spec.md is generated for the selected task.",
        "Weak source artifacts become findings in spec.md.",
    ]
    records = {
        record["output_path"]: record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
    }
    dependency = next(
        item
        for item in records[f".harness/tasks/{slug}/spec.md"]["dependencies"]
        if item["dependency_path"].endswith("/acceptance.md")
    )
    assert dependency["dependency_sha256"] == hashlib.sha256(
        captured_bytes
    ).hexdigest()


def test_spec_rejects_symlinked_output_before_ownership_checks(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    target = project_tmp / "redirected-spec.md"
    target.write_bytes(b"keep target")
    output = _spec_path(project_tmp, slug)
    _symlink_or_skip(output, target)
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    code, messages = run_spec(project_tmp, slug, force=True)

    assert code == 1
    assert "managed output path must not be a symlink" in messages[0]
    assert output.is_symlink()
    assert target.read_bytes() == b"keep target"
    assert not _requirements_path(project_tmp, slug).exists()
    assert manifest_path.read_bytes() == manifest_before


def test_spec_checks_mocked_output_safety_before_capture(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    spec_output = _spec_path(project_tmp, slug)
    requirements_output = _requirements_path(project_tmp, slug)
    spec_output.write_bytes(b"keep spec")
    requirements_output.write_bytes(b"keep requirements")
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    def reject_output(*_args, **_kwargs):
        raise PathSafetyError("simulated unsafe managed output")

    def fail_if_captured(*_args, **_kwargs):
        pytest.fail("dependency capture ran after output safety failure")

    monkeypatch.setattr(
        spec_module,
        "resolve_managed_output_under_root",
        reject_output,
    )
    monkeypatch.setattr(
        spec_module,
        "capture_dependencies",
        fail_if_captured,
    )

    code, messages = run_spec(project_tmp, slug, force=True)

    assert code == 1
    assert "simulated unsafe managed output" in messages[0]
    assert manifest_path.read_bytes() == manifest_before
    assert spec_output.read_bytes() == b"keep spec"
    assert requirements_output.read_bytes() == b"keep requirements"


def test_spec_final_manifest_failure_records_no_new_sibling_provenance(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr(
        "ai_sdlc_harness.preflight._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_preflight(project_tmp, slug)[0] == 0
    monkeypatch.setattr(
        "ai_sdlc_harness.spec._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_spec(project_tmp, slug)[0] == 0
    spec_path_text = f".harness/tasks/{slug}/spec.md"
    requirements_path_text = f".harness/tasks/{slug}/requirements.yaml"
    old_hashes = {
        path: _manifest_entry(project_tmp, path)["sha256"]
        for path in (spec_path_text, requirements_path_text)
    }
    preflight_path = f".harness/tasks/{slug}/preflight.md"
    unrelated_before = next(
        record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"] == preflight_path
    )
    acceptance = _task_path(project_tmp, slug, "acceptance.md")
    acceptance.write_text(
        acceptance.read_text(encoding="utf-8").replace(
            "- Weak source artifacts become findings in spec.md.",
            "- Weak source artifacts become findings in spec.md.\n"
            "- Both outputs change before final manifest failure.",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.spec._timestamp",
        lambda: "2026-07-29T00:00:01+00:00",
    )
    real_persist = spec_module.persist_manifest_model
    calls = 0

    def fail_final(root, document):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_persist(root, document)
        raise OSError("simulated spec final manifest failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.spec.persist_manifest_model",
        fail_final,
    )

    code, messages = run_spec(project_tmp, slug)

    assert code == 1
    assert "simulated spec final manifest failure" in messages[0]
    manifest = _manifest(project_tmp)
    records = {
        record["output_path"]: record
        for record in manifest["generated_artifact_provenance"]
    }
    assert spec_path_text not in records
    assert requirements_path_text not in records
    assert records[preflight_path] == unrelated_before
    for path, old_hash in old_hashes.items():
        assert _manifest_entry(project_tmp, path)["sha256"] == old_hash
        target = project_tmp / Path(*path.split("/"))
        assert hashlib.sha256(target.read_bytes()).hexdigest() != old_hash
