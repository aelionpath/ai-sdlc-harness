from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest
import yaml

import ai_sdlc_harness.workflow as workflow_module
from ai_sdlc_harness.context import ContextFinding, FindingLevel, FreshnessState
from ai_sdlc_harness.generate import run_generate
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.lineage import LineageResolutionResult
from ai_sdlc_harness.manifest import write_manifest
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.spec import run_spec
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.test_contract import run_test_contract_review
from ai_sdlc_harness.validation import (
    ValidationCurrentnessFinding,
    ValidationCurrentnessResult,
    ValidationCurrentnessState,
)
from ai_sdlc_harness.workflow import (
    LINEAGE_KEYS,
    WorkflowArtifactState,
    WorkflowOutcomeCategory,
    WorkflowPhase,
    resolve_workflow_state,
)


SLUG = "workflow-task"


def _task_path(root: Path, filename: str) -> Path:
    return root / ".harness" / "tasks" / SLUG / filename


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _requirements_text() -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "artifact_role": "advisory_projection",
            "authority": "non_authoritative",
            "edit_model": "edit_source_task_artifacts_and_rerun_spec",
            "task_slug": SLUG,
            "source_model": "deterministic_acceptance_markdown_projection",
            "source_artifacts": [
                {
                    "path": "acceptance.md",
                    "section": "Requirements And Acceptance Criteria",
                }
            ],
            "requirements": [
                {
                    "id": "REQ-001",
                    "statement": "Resolve workflow status deterministically.",
                    "status": "draft",
                    "source": {
                        "path": "acceptance.md",
                        "section": "Requirements And Acceptance Criteria",
                    },
                    "acceptance_criteria": [],
                    "verification": [],
                }
            ],
            "findings": [],
        },
        sort_keys=False,
    )


def _prepare_task(root: Path, *, evidence: str | None = None, verification: str | None = None) -> None:
    assert init_project(root)[0] == 0
    assert start_task(root, "Workflow Task")[0] == 0
    contents = {
        "task.md": (
            "# Task\n\n## Implementation Boundary\n\n"
            "Implement deterministic workflow status.\n\n"
            "## Assumptions And Open Questions\n\nNo blockers remain.\n"
        ),
        "acceptance.md": (
            "# Acceptance\n\n## Requirements And Acceptance Criteria\n\n"
            "- Status exposes one next action.\n\n"
            "## Protected Behavior And Non-Goals\n\n"
            "- Preserve generated-output integrity.\n"
        ),
        "architecture-notes.md": "# Architecture\n\nKeep the resolver pure.\n",
        "coupling-notes.md": "# Coupling\n\nReuse existing subsystems.\n",
        "test-contract.md": "# Test Contract\n\n- Exercise workflow navigation.\n",
        "verification.md": verification
        or "# Verification\n\npy -m pytest tests\n\nTests passed in 1.0s.\n",
        "evidence.md": evidence or "# Evidence\n\nImplementation evidence recorded.\n",
    }
    for filename, content in contents.items():
        _task_path(root, filename).write_text(content, encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    write_manifest(root)


def _lineage(**overrides: FreshnessState) -> LineageResolutionResult:
    states = {key: FreshnessState.FRESH for key in LINEAGE_KEYS}
    states.update(overrides)
    return LineageResolutionResult(states, ())


def _patch_lineage(monkeypatch, **overrides: FreshnessState) -> None:
    result = _lineage(**overrides)
    monkeypatch.setattr(workflow_module, "resolve_derived_freshness", lambda *_: result)


def _write_handoff(root: Path, *, workset: bool = True, context: bool = True) -> None:
    generated = _task_path(root, "generated")
    generated.mkdir(exist_ok=True)
    if workset:
        (generated / "agent-workset.md").write_text("# Workset\n\nImplement.\n", encoding="utf-8")
    if context:
        (generated / "context-manifest.yaml").write_text("schema_version: 1\n", encoding="utf-8")


def _write_evidence_report(root: Path) -> None:
    _task_path(root, "evidence-report.md").write_text(
        "# Evidence Report\n\nEvidence is ready.\n", encoding="utf-8"
    )


def _write_clean_semantic_outputs(root: Path) -> None:
    reports = {
        "preflight.md": "# Preflight\n\nReady.\n",
        "spec.md": "# Specification\n\nReady.\n",
        "requirements.yaml": _requirements_text(),
        "test-contract-review.md": "# Test Contract Review\n\nReady.\n",
    }
    for filename, content in reports.items():
        _task_path(root, filename).write_text(content, encoding="utf-8")
    _write_handoff(root)
    _write_evidence_report(root)
    write_manifest(root)


def _patch_currentness(monkeypatch, state: ValidationCurrentnessState, *, blocker: bool = False) -> None:
    findings = ()
    if state is not ValidationCurrentnessState.CURRENT:
        findings = (
            ValidationCurrentnessFinding(
                "validation_test_state",
                "blocker" if blocker else "warning",
                f".harness/tasks/{SLUG}/validation-report.md",
                "Validation currentness test state.",
            ),
        )
    result = ValidationCurrentnessResult(SLUG, state, findings)
    monkeypatch.setattr(workflow_module, "resolve_validation_currentness", lambda *_: result)


def _prepare_generated_user_flow(root: Path) -> None:
    assert init_project(root)[0] == 0
    assert start_task(root, "Workflow Task")[0] == 0
    _task_path(root, "task.md").write_text(
        """# Task: Workflow Task

Task slug: `workflow-task`

## Implementation Boundary

Make workflow status deterministic after normal user edits.

## Assumptions And Open Questions

No blocking questions remain.
""",
        encoding="utf-8",
    )
    _task_path(root, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- Status blocks when generated artifacts drift.

## Protected Behavior And Non-Goals

- Human-owned task sources remain directly editable.
""",
        encoding="utf-8",
    )
    _task_path(root, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Preserve strict generated-output integrity.

## Desired Behavior Tests

- Report drift before implementation guidance.

## Regression Tests

- Run the focused workflow tests.

## Negative And Edge Cases

- Missing human-owned sources remain a readiness concern.
""",
        encoding="utf-8",
    )
    _task_path(root, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run

py -m pytest tests/test_workflow.py

## Results

Not run because implementation has not started.

## Manual Review Notes

Confirm status remains read-only.
""",
        encoding="utf-8",
    )
    assert run_preflight(root, SLUG)[0] == 0
    assert run_spec(root, SLUG)[0] == 0
    assert run_test_contract_review(root, SLUG)[0] == 0
    assert run_generate(root, SLUG)[0] == 0


def _assert_integrity_blocked_without_implementation_guidance(output: list[str]) -> None:
    rendered = "\n".join(output)
    assert "phase: blocked" in rendered
    assert "outcome: BLOCKED" in rendered
    assert "Implement the task" not in rendered
    assert "agent-workset.md handoff" not in rendered


def test_generated_workset_drift_blocks_before_implementation(project_tmp):
    _prepare_generated_user_flow(project_tmp)
    target = _task_path(project_tmp, "generated/agent-workset.md")
    target.write_bytes(target.read_bytes() + b"\ngenerated drift")

    code, output = status_project(project_tmp, SLUG)

    assert code == 0
    _assert_integrity_blocked_without_implementation_guidance(output)


def test_context_manifest_drift_blocks_before_implementation(project_tmp):
    _prepare_generated_user_flow(project_tmp)
    target = _task_path(project_tmp, "generated/context-manifest.yaml")
    target.write_bytes(target.read_bytes() + b"\ngenerated_drift: true\n")

    code, output = status_project(project_tmp, SLUG)

    assert code == 0
    _assert_integrity_blocked_without_implementation_guidance(output)


def test_base_config_drift_blocks_before_workflow_progression(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Workflow Task")[0] == 0
    _task_path(project_tmp, "task.md").write_text(
        "# Task\n\n## Implementation Boundary\n\nReady for preflight.\n",
        encoding="utf-8",
    )
    _task_path(project_tmp, "acceptance.md").write_text(
        "# Acceptance\n\n## Requirements And Acceptance Criteria\n\n- Ready.\n",
        encoding="utf-8",
    )
    config_path = project_tmp / ".harness" / "config.yaml"
    config_path.write_bytes(config_path.read_bytes() + b"\ndrifted: true\n")

    code, output = status_project(project_tmp, SLUG)

    assert code == 0
    _assert_integrity_blocked_without_implementation_guidance(output)


def test_real_user_edit_flow_needs_no_manifest_refresh_helper(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Workflow Task")[0] == 0

    code, untouched = status_project(project_tmp, SLUG)
    assert code == 0
    assert "phase: task_definition" in untouched
    assert "outcome: REVIEW_REQUIRED" in untouched
    assert "outcome: BLOCKED" not in untouched

    _task_path(project_tmp, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- Status gives a deterministic next command.
- Normal source edits require no hidden manifest maintenance.

## Protected Behavior And Non-Goals

- Generated output integrity remains strict.
""",
        encoding="utf-8",
    )
    code, acceptance_only = status_project(project_tmp, SLUG)
    assert code == 0
    assert "phase: task_definition" in acceptance_only
    assert "outcome: REVIEW_REQUIRED" in acceptance_only
    assert any("task.md" in line for line in acceptance_only)

    _task_path(project_tmp, "task.md").write_text(
        """# Task: Workflow Task

Task slug: `workflow-task`

## Implementation Boundary

Make status usable after direct edits to human-owned task source files.

Preserve strict integrity for generated and Harness-owned outputs.

## Assumptions And Open Questions

No blocking questions remain.
""",
        encoding="utf-8",
    )
    code, sources_ready = status_project(project_tmp, SLUG)
    assert code == 0
    assert "phase: preflight" in sources_ready
    assert "outcome: BLOCKED" not in sources_ready
    assert f"  ai-sdlc preflight --task {SLUG}" in sources_ready

    _task_path(project_tmp, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Preserve generated-output hash enforcement.

## Desired Behavior Tests

- Direct source edits remain navigable.

## Regression Tests

- Run the focused workflow tests.

## Negative And Edge Cases

- Unsafe task paths remain blocked.
""",
        encoding="utf-8",
    )
    _task_path(project_tmp, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run

py -m pytest tests/test_workflow.py

## Results

Not run because implementation has not started.

## Manual Review Notes

Confirm status remains read-only.
""",
        encoding="utf-8",
    )

    assert run_preflight(project_tmp, SLUG)[0] == 0
    code, after_preflight = status_project(project_tmp, SLUG)
    assert code == 0
    assert "phase: specification" in after_preflight
    assert f"  ai-sdlc spec --task {SLUG}" in after_preflight

    assert run_spec(project_tmp, SLUG)[0] == 0
    code, after_spec = status_project(project_tmp, SLUG)
    assert code == 0
    assert "phase: test_contract" in after_spec
    assert f"  ai-sdlc test-contract --task {SLUG}" in after_spec

    assert run_test_contract_review(project_tmp, SLUG)[0] == 0
    code, after_test_contract = status_project(project_tmp, SLUG)
    assert code == 0
    assert "phase: implementation_handoff" in after_test_contract
    assert f"  ai-sdlc generate --task {SLUG}" in after_test_contract

    assert run_generate(project_tmp, SLUG)[0] == 0
    code, after_generate = status_project(project_tmp, SLUG)
    assert code == 0
    assert "phase: implementation" in after_generate
    assert "outcome: REVIEW_REQUIRED" in after_generate


def test_uninitialized_and_initialized_without_tasks_navigate_forward(project_tmp):
    uninitialized = resolve_workflow_state(project_tmp)
    assert uninitialized.phase is WorkflowPhase.UNINITIALIZED
    assert uninitialized.outcome is WorkflowOutcomeCategory.NEXT
    assert uninitialized.next_actions[0].command == "ai-sdlc init"

    assert init_project(project_tmp)[0] == 0
    no_tasks = resolve_workflow_state(project_tmp)
    assert no_tasks.phase is WorkflowPhase.TASK_SELECTION
    assert no_tasks.next_actions[0].command == 'ai-sdlc task start "<task>"'


def test_empty_or_incomplete_harness_is_not_reported_as_initialized(project_tmp):
    (project_tmp / ".harness").mkdir()
    empty = resolve_workflow_state(project_tmp)
    assert empty.initialized is False
    assert empty.phase is WorkflowPhase.UNINITIALIZED
    assert empty.next_actions[0].command == "ai-sdlc init"


@pytest.mark.parametrize("missing", ["config.yaml", "manifest.json"])
def test_missing_required_initialization_marker_returns_init_guidance(project_tmp, missing):
    assert init_project(project_tmp)[0] == 0
    (project_tmp / ".harness" / missing).unlink()

    state = resolve_workflow_state(project_tmp)

    assert state.initialized is False
    assert state.phase is WorkflowPhase.UNINITIALIZED
    assert state.outcome is WorkflowOutcomeCategory.NEXT
    assert state.next_actions[0].command == "ai-sdlc init"


@pytest.mark.parametrize("relative", [".harness", ".harness/config.yaml", ".harness/manifest.json"])
def test_unsafe_initialization_path_is_blocked(project_tmp, monkeypatch, relative):
    assert init_project(project_tmp)[0] == 0
    target = project_tmp / relative
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)
    state = resolve_workflow_state(project_tmp)

    assert state.initialized is False
    assert state.phase is WorkflowPhase.BLOCKED
    assert state.outcome is WorkflowOutcomeCategory.BLOCKED
    assert state.integrity_clean is False


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("config.yaml", "[unterminated\n"),
        ("manifest.json", "{}\n"),
    ],
)
def test_invalid_initialization_metadata_is_blocked(project_tmp, filename, content):
    assert init_project(project_tmp)[0] == 0
    (project_tmp / ".harness" / filename).write_text(content, encoding="utf-8")

    state = resolve_workflow_state(project_tmp)

    assert state.initialized is False
    assert state.phase is WorkflowPhase.BLOCKED
    assert state.outcome is WorkflowOutcomeCategory.BLOCKED


def test_single_task_is_auto_selected_and_multiple_tasks_require_choice(project_tmp):
    _prepare_task(project_tmp)
    single = resolve_workflow_state(project_tmp)
    assert single.selected_task == SLUG

    assert start_task(project_tmp, "Another Task")[0] == 0
    multiple = resolve_workflow_state(project_tmp)
    assert multiple.selected_task is None
    assert multiple.available_tasks == ("another-task", SLUG)
    assert multiple.next_actions[0].command == "ai-sdlc status --task <slug>"

    explicit = resolve_workflow_state(project_tmp, SLUG)
    assert explicit.selected_task == SLUG


def test_missing_unsafe_and_invalid_task_directories_are_not_selected(project_tmp):
    assert init_project(project_tmp)[0] == 0
    (project_tmp / ".harness" / "tasks" / "bad_name").mkdir()
    state = resolve_workflow_state(project_tmp)
    assert state.available_tasks == ()

    code, messages = status_project(project_tmp, "missing-task")
    assert code == 2
    assert messages[-1] == "error: Task folder does not exist: .harness/tasks/missing-task"
    code, messages = status_project(project_tmp, "../unsafe")
    assert code == 2
    assert "Task slug is unsafe" in messages[-1]


def test_filling_task_only_keeps_real_acceptance_template_in_task_definition(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Workflow Task")[0] == 0
    _task_path(project_tmp, "task.md").write_text(
        """# Task: Workflow Task

## Implementation Boundary

Implement real-template readiness checks.

## Assumptions And Open Questions

No blocking questions remain.
""",
        encoding="utf-8",
    )

    code, messages = status_project(project_tmp, SLUG)

    assert code == 0
    assert "phase: task_definition" in messages
    assert "outcome: REVIEW_REQUIRED" in messages
    assert any("acceptance.md" in line for line in messages)


def test_missing_task_source_is_recoverable_task_definition_work(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Workflow Task")[0] == 0
    _task_path(project_tmp, "task.md").unlink()

    state = resolve_workflow_state(project_tmp, SLUG)

    assert state.phase is WorkflowPhase.TASK_DEFINITION
    assert state.outcome is WorkflowOutcomeCategory.REVIEW_REQUIRED
    assert state.integrity_clean is None


def test_task_source_final_symlink_is_blocked_by_workflow(project_tmp, monkeypatch):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Workflow Task")[0] == 0
    target = _task_path(project_tmp, "task.md")
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)
    state = resolve_workflow_state(project_tmp, SLUG)

    assert state.phase is WorkflowPhase.BLOCKED
    assert state.outcome is WorkflowOutcomeCategory.BLOCKED
    assert state.findings[0].path == f".harness/tasks/{SLUG}/task.md"


@pytest.mark.parametrize("filename", ["task.md", "acceptance.md"])
def test_task_definition_gate_requires_only_task_and_acceptance(project_tmp, monkeypatch, filename):
    _prepare_task(project_tmp)
    _task_path(project_tmp, filename).write_text(f"# {filename}\n\nTODO\n", encoding="utf-8")
    write_manifest(project_tmp)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.TASK_DEFINITION
    assert state.outcome is WorkflowOutcomeCategory.REVIEW_REQUIRED
    assert filename in state.next_actions[0].text


def test_todo_evidence_does_not_prevent_preflight_navigation(project_tmp, monkeypatch):
    _prepare_task(project_tmp, evidence="# Evidence\n\nTODO\n")
    _patch_lineage(monkeypatch, preflight=FreshnessState.MISSING)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.PREFLIGHT
    assert state.next_actions[0].command == f"ai-sdlc preflight --task {SLUG}"


@pytest.mark.parametrize("freshness", [FreshnessState.MISSING, FreshnessState.STALE])
def test_preflight_missing_or_stale_is_the_first_producer(project_tmp, monkeypatch, freshness):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch, preflight=freshness, spec=FreshnessState.MISSING)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.PREFLIGHT
    assert tuple(action.command for action in state.next_actions) == (
        f"ai-sdlc preflight --task {SLUG}",
    )


@pytest.mark.parametrize("key", ["spec", "requirements_projection"])
@pytest.mark.parametrize("freshness", [FreshnessState.MISSING, FreshnessState.STALE])
def test_spec_pair_has_one_deduplicated_producer_action(project_tmp, monkeypatch, key, freshness):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch, **{key: freshness})
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.SPECIFICATION
    assert tuple(action.command for action in state.next_actions) == (
        f"ai-sdlc spec --task {SLUG}",
    )


@pytest.mark.parametrize("filename", ["test-contract.md", "verification.md"])
def test_test_definition_gate_precedes_test_contract_producer(project_tmp, monkeypatch, filename):
    _prepare_task(project_tmp)
    _task_path(project_tmp, filename).write_text(f"# {filename}\n\nTODO\n", encoding="utf-8")
    write_manifest(project_tmp)
    _patch_lineage(monkeypatch, test_contract_review=FreshnessState.MISSING)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.TEST_CONTRACT
    assert state.outcome is WorkflowOutcomeCategory.REVIEW_REQUIRED
    assert filename in state.next_actions[0].text


def test_test_contract_missing_or_stale_navigates_to_one_command(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch, test_contract_review=FreshnessState.STALE)
    state = resolve_workflow_state(project_tmp)
    assert state.next_actions[0].command == f"ai-sdlc test-contract --task {SLUG}"


def test_unknown_lineage_with_blocker_is_blocked(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    states = _lineage(preflight=FreshnessState.UNKNOWN)
    result = LineageResolutionResult(
        states.derived_freshness,
        (
            ContextFinding(
                "unsafe_lineage_input",
                FindingLevel.BLOCKER,
                ".harness/manifest.json",
                "Lineage cannot be trusted.",
            ),
        ),
    )
    monkeypatch.setattr(workflow_module, "resolve_derived_freshness", lambda *_: result)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.BLOCKED
    assert state.outcome is WorkflowOutcomeCategory.BLOCKED


@pytest.mark.parametrize(
    ("workset", "context", "expected_workset", "expected_context"),
    [
        (False, False, WorkflowArtifactState.MISSING, WorkflowArtifactState.MISSING),
        (True, False, WorkflowArtifactState.PRESENT, WorkflowArtifactState.MISSING),
        (False, True, WorkflowArtifactState.MISSING, WorkflowArtifactState.PRESENT),
    ],
)
def test_missing_handoff_member_navigates_to_generate(
    project_tmp, monkeypatch, workset, context, expected_workset, expected_context
):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch)
    _write_handoff(project_tmp, workset=workset, context=context)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.IMPLEMENTATION_HANDOFF
    assert state.generated_workset is expected_workset
    assert state.context_manifest is expected_context
    assert state.next_actions[0].command == f"ai-sdlc generate --task {SLUG}"


def test_nonregular_generated_output_is_blocked(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch)
    generated = _task_path(project_tmp, "generated")
    generated.mkdir()
    (generated / "agent-workset.md").write_text("ready\n", encoding="utf-8")
    (generated / "context-manifest.yaml").mkdir()
    state = resolve_workflow_state(project_tmp)
    assert state.outcome is WorkflowOutcomeCategory.BLOCKED
    assert state.context_manifest is WorkflowArtifactState.UNSAFE


@pytest.mark.parametrize(
    ("evidence", "verification"),
    [
        ("# Evidence\n\nTODO\n", "# Verification\n\npytest\n\nTests passed.\n"),
        ("# Evidence\n\nImplemented.\n", "# Verification\n\nManual only.\n\nTests passed.\n"),
        ("# Evidence\n\nImplemented.\n", "# Verification\n\npytest\n\nNot run yet.\n"),
    ],
)
def test_implementation_gate_requires_evidence_command_and_result(
    project_tmp, monkeypatch, evidence, verification
):
    _prepare_task(project_tmp, evidence=evidence, verification=verification)
    _patch_lineage(monkeypatch)
    _write_handoff(project_tmp)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.IMPLEMENTATION
    assert state.outcome is WorkflowOutcomeCategory.REVIEW_REQUIRED


def test_ready_implementation_without_evidence_report_navigates_to_evidence(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch)
    _write_handoff(project_tmp)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.EVIDENCE
    assert state.next_actions[0].command == f"ai-sdlc evidence --task {SLUG}"


def test_nonregular_evidence_report_is_blocked(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch)
    _write_handoff(project_tmp)
    _task_path(project_tmp, "evidence-report.md").mkdir()
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.BLOCKED
    assert state.evidence_report is WorkflowArtifactState.UNSAFE


@pytest.mark.parametrize(
    ("currentness", "blocker", "phase", "outcome"),
    [
        (ValidationCurrentnessState.MISSING, False, WorkflowPhase.VALIDATION, WorkflowOutcomeCategory.NEXT),
        (ValidationCurrentnessState.STALE, False, WorkflowPhase.VALIDATION, WorkflowOutcomeCategory.NEXT),
        (ValidationCurrentnessState.UNKNOWN, False, WorkflowPhase.VALIDATION, WorkflowOutcomeCategory.NEXT),
        (ValidationCurrentnessState.UNKNOWN, True, WorkflowPhase.BLOCKED, WorkflowOutcomeCategory.BLOCKED),
        (ValidationCurrentnessState.INVALID, True, WorkflowPhase.BLOCKED, WorkflowOutcomeCategory.BLOCKED),
    ],
)
def test_validation_currentness_navigation(
    project_tmp, monkeypatch, currentness, blocker, phase, outcome
):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch)
    _write_handoff(project_tmp)
    _write_evidence_report(project_tmp)
    _patch_currentness(monkeypatch, currentness, blocker=blocker)
    state = resolve_workflow_state(project_tmp)
    assert state.phase is phase
    assert state.outcome is outcome
    if outcome is WorkflowOutcomeCategory.NEXT:
        assert state.next_actions[0].command == f"ai-sdlc validate --task {SLUG}"


def test_current_semantic_blockers_warnings_and_clean_are_distinct(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _write_clean_semantic_outputs(project_tmp)
    _patch_lineage(monkeypatch)
    _patch_currentness(monkeypatch, ValidationCurrentnessState.CURRENT)

    clean = resolve_workflow_state(project_tmp)
    assert clean.phase is WorkflowPhase.COMPLETE
    assert clean.outcome is WorkflowOutcomeCategory.COMPLETE
    assert clean.validation_clean is True

    (project_tmp / ".github" / "workflows").rmdir()
    warning = resolve_workflow_state(project_tmp)
    assert warning.phase is WorkflowPhase.REVIEW
    assert warning.outcome is WorkflowOutcomeCategory.REVIEW_REQUIRED
    assert warning.validation_warning_count

    (project_tmp / ".github" / "workflows").mkdir()
    _task_path(project_tmp, "architecture-notes.md").write_text(
        "# Architecture\n\nTODO\n", encoding="utf-8"
    )
    write_manifest(project_tmp)
    blocker = resolve_workflow_state(project_tmp)
    assert blocker.phase is WorkflowPhase.BLOCKED
    assert blocker.outcome is WorkflowOutcomeCategory.BLOCKED
    assert blocker.validation_blocker_count


@pytest.mark.parametrize("key", ["spec", "preflight", "test_contract_review"])
def test_complete_is_prevented_by_any_stale_lineage_stage(project_tmp, monkeypatch, key):
    _prepare_task(project_tmp)
    _write_clean_semantic_outputs(project_tmp)
    _patch_lineage(monkeypatch, **{key: FreshnessState.STALE})
    _patch_currentness(monkeypatch, ValidationCurrentnessState.CURRENT)
    state = resolve_workflow_state(project_tmp)
    assert state.outcome is not WorkflowOutcomeCategory.COMPLETE


def test_integrity_failure_precedes_complete(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _write_clean_semantic_outputs(project_tmp)
    _patch_lineage(monkeypatch)
    _patch_currentness(monkeypatch, ValidationCurrentnessState.CURRENT)
    (project_tmp / ".harness" / "config.yaml").write_text("drifted: true\n", encoding="utf-8")
    state = resolve_workflow_state(project_tmp)
    assert state.phase is WorkflowPhase.BLOCKED
    assert state.integrity_clean is False


def test_complete_retains_final_full_verification(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _write_clean_semantic_outputs(project_tmp)
    _patch_lineage(monkeypatch)
    _patch_currentness(monkeypatch, ValidationCurrentnessState.CURRENT)
    real_verify = workflow_module.verify_project
    policy_calls: list[bool] = []

    def recording_verify(
        root: Path,
        *,
        allow_human_source_readiness: bool = False,
    ) -> tuple[int, list[str]]:
        policy_calls.append(allow_human_source_readiness)
        return real_verify(
            root,
            allow_human_source_readiness=allow_human_source_readiness,
        )

    monkeypatch.setattr(workflow_module, "verify_project", recording_verify)

    state = resolve_workflow_state(project_tmp)

    assert state.phase is WorkflowPhase.COMPLETE
    assert state.outcome is WorkflowOutcomeCategory.COMPLETE
    assert policy_calls == [True, False]


def test_complete_is_prevented_by_generated_output_integrity_drift(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _write_clean_semantic_outputs(project_tmp)
    write_manifest(
        project_tmp,
        extra_managed_paths=(
            PurePosixPath(
                f".harness/tasks/{SLUG}/generated/context-manifest.yaml"
            ),
        ),
    )
    _patch_lineage(monkeypatch)
    _patch_currentness(monkeypatch, ValidationCurrentnessState.CURRENT)
    _task_path(project_tmp, "generated/context-manifest.yaml").write_text(
        "schema_version: drifted\n", encoding="utf-8"
    )

    state = resolve_workflow_state(project_tmp)

    assert state.phase is WorkflowPhase.BLOCKED
    assert state.outcome is WorkflowOutcomeCategory.BLOCKED
    assert state.integrity_clean is False
    assert any("hash drift detected" in finding.message for finding in state.findings)


def test_workflow_models_are_frozen_and_lineage_mapping_is_immutable(project_tmp):
    state = resolve_workflow_state(project_tmp)
    with pytest.raises(FrozenInstanceError):
        state.phase = WorkflowPhase.COMPLETE
    with pytest.raises(TypeError):
        state.lineage_freshness["spec"] = FreshnessState.FRESH


def test_human_and_json_status_are_compact_deterministic_and_read_only(project_tmp, monkeypatch):
    _prepare_task(project_tmp)
    _patch_lineage(monkeypatch, preflight=FreshnessState.MISSING)
    before = _snapshot(project_tmp)

    human_code, human = status_project(project_tmp)
    first_code, first = status_project(project_tmp, json_output=True)
    second_code, second = status_project(project_tmp, json_output=True)

    assert human_code == first_code == second_code == 0
    assert len(human) <= 25
    assert "phase: preflight" in human
    assert "outcome: NEXT" in human
    assert sum(line.strip() == f"ai-sdlc preflight --task {SLUG}" for line in human) == 1
    assert first == second
    document = json.loads(first[0])
    assert document["workflow_status_schema_version"] == 1
    assert tuple(document["lineage"]) == tuple(sorted(LINEAGE_KEYS))
    assert document["next_actions"] == [
        {
            "command": f"ai-sdlc preflight --task {SLUG}",
            "kind": "command",
            "text": f"ai-sdlc preflight --task {SLUG}",
        }
    ]
    output = "\n".join((*human, *first))
    assert str(project_tmp) not in output
    assert "generated_at" not in first[0]
    assert "token-abcdefghijklmnop" not in output
    assert _snapshot(project_tmp) == before
