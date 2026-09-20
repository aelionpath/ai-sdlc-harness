"""Implementation of ``ai-sdlc status``."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .constants import (
    AGENT_WORKSET_FILENAME,
    EVIDENCE_REPORT_FILENAME,
    HARNESS_DIR,
    PREFLIGHT_FILENAME,
    REQUIREMENTS_FILENAME,
    SPEC_FILENAME,
    TASK_GENERATED_DIRNAME,
    TEST_CONTRACT_REVIEW_FILENAME,
    VALIDATION_REPORT_FILENAME,
)
from .manifest import load_manifest
from .redact import redact_value
from .workflow import (
    LINEAGE_KEYS,
    WorkflowActionKind,
    WorkflowArtifactState,
    WorkflowFinding,
    WorkflowPhase,
    WorkflowResolutionError,
    WorkflowState,
    resolve_workflow_state,
)


WORKFLOW_STATUS_SCHEMA_VERSION = 1


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return yaml.safe_load(file_obj)


def _manifest_counts(root: Path) -> dict[str, int]:
    labels = {
        "preflight": 0,
        "spec": 0,
        "requirements": 0,
        "test_contract_review": 0,
        "generated_workset": 0,
        "evidence_report": 0,
        "validation_report": 0,
    }
    manifest_path = root / HARNESS_DIR / "manifest.json"
    if not manifest_path.is_file():
        return labels
    try:
        entries = load_manifest(manifest_path).get("managed_files", [])
    except (OSError, ValueError, UnicodeError):
        return labels
    if not isinstance(entries, list):
        return labels
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = PurePosixPath(str(entry.get("path", "")).replace("\\", "/"))
        direct = len(path.parts) == 4 and path.parts[:2] == (".harness", "tasks")
        generated = (
            len(path.parts) == 5
            and path.parts[:2] == (".harness", "tasks")
            and path.parts[3] == TASK_GENERATED_DIRNAME
        )
        if direct and path.name == PREFLIGHT_FILENAME:
            labels["preflight"] += 1
        elif direct and path.name == SPEC_FILENAME:
            labels["spec"] += 1
        elif direct and path.name == REQUIREMENTS_FILENAME:
            labels["requirements"] += 1
        elif direct and path.name == TEST_CONTRACT_REVIEW_FILENAME:
            labels["test_contract_review"] += 1
        elif generated and path.name == AGENT_WORKSET_FILENAME:
            labels["generated_workset"] += 1
        elif direct and path.name == EVIDENCE_REPORT_FILENAME:
            labels["evidence_report"] += 1
        elif direct and path.name == VALIDATION_REPORT_FILENAME:
            labels["validation_report"] += 1
    return labels


def _legacy_summary(root: Path, state: WorkflowState) -> list[str]:
    """Retain small, established status facts without overwhelming navigation."""

    if not state.initialized:
        return []
    if state.selected_task is not None:
        return []
    messages = [f"task folders: {len(state.available_tasks)}"]
    config_path = root / HARNESS_DIR / "config.yaml"
    if state.selected_task is None and config_path.is_file():
        try:
            loaded = _load_yaml(config_path)
            config = redact_value(loaded) if isinstance(loaded, dict) else {}
            messages.append(
                f"selected packs: {', '.join(config.get('selected_packs', [])) or 'none'}"
            )
            adapters = config.get("adapters", {})
            if isinstance(adapters, dict):
                adapter_text = ", ".join(
                    f"{key}={value}" for key, value in adapters.items()
                )
                messages.append(f"adapters: {adapter_text or 'none'}")
        except (OSError, TypeError, ValueError, UnicodeError, yaml.YAMLError):
            messages.append("config: unreadable")

    counts = _manifest_counts(root)
    count_labels = (
        ("preflight", "manifest-managed preflight reports"),
        ("spec", "manifest-managed spec reports"),
        ("requirements", "manifest-managed requirements files"),
        ("test_contract_review", "manifest-managed test-contract review reports"),
        ("generated_workset", "manifest-managed generated worksets"),
        ("evidence_report", "manifest-managed evidence reports"),
        ("validation_report", "manifest-managed validation reports"),
    )
    messages.extend(
        f"{label}: {counts[key]}"
        for key, label in count_labels
        if counts[key]
    )
    return messages


def _lineage_text(state: WorkflowState, *keys: str) -> str:
    values = [state.lineage_freshness[key] for key in keys]
    if all(value is values[0] for value in values):
        value = values[0]
        return value.value if value is not None else "not inspected"
    return ", ".join(
        f"{key}={value.value if value is not None else 'not inspected'}"
        for key, value in zip(keys, values, strict=True)
    )


def _render_findings(findings: tuple[WorkflowFinding, ...]) -> list[str]:
    if not findings:
        return []
    lines = ["", "findings:"]
    lines.extend(f"  {finding.level.value}: {finding.message}" for finding in findings)
    return lines


def render_workflow_status(state: WorkflowState) -> list[str]:
    """Render compact, developer-first status text."""

    messages = ["AI SDLC Harness", f"initialized: {'yes' if state.initialized else 'no'}"]
    if state.selected_task is not None:
        messages.extend(
            (
                f"task: {state.selected_task}",
                f"phase: {state.phase.value}",
                f"outcome: {state.outcome.value}",
            )
        )
    elif state.available_tasks:
        messages.extend(
            (
                f"tasks: {', '.join(state.available_tasks)}",
                f"outcome: {state.outcome.value}",
            )
        )
    else:
        messages.append(f"outcome: {state.outcome.value}")

    if state.selected_task is not None and state.integrity_clean is not False:
        messages.extend(
            (
                "",
                "current:",
                f"  preflight: {_lineage_text(state, 'preflight')}",
                f"  specification: {_lineage_text(state, 'spec', 'requirements_projection')}",
                f"  test contract: {_lineage_text(state, 'test_contract_review')}",
            )
        )
        if state.validation_currentness is not None:
            validation = state.validation_currentness.value
            if state.validation_clean:
                validation += " and CLEAN"
            elif (
                state.validation_blocker_count is not None
                and state.validation_warning_count is not None
            ):
                validation += (
                    f" ({state.validation_blocker_count} blocker(s), "
                    f"{state.validation_warning_count} warning(s))"
                )
            messages.append(f"  validation: {validation}")
        if state.integrity_clean is True:
            messages.append("  repository integrity: clean")

    if state.phase is WorkflowPhase.IMPLEMENTATION and state.selected_task:
        messages.extend(
            (
                "",
                "handoff:",
                f"  .harness/tasks/{state.selected_task}/generated/{AGENT_WORKSET_FILENAME}",
            )
        )

    messages.extend(_render_findings(state.findings))
    if state.next_actions:
        messages.extend(("", "next:"))
        for action in state.next_actions:
            if action.kind is WorkflowActionKind.COMMAND:
                messages.append(f"  {action.command}")
            else:
                messages.append(f"  {action.text}")
    return messages


def _artifact_value(value: WorkflowArtifactState | None) -> str | None:
    return value.value if value is not None else None


def workflow_status_document(state: WorkflowState) -> dict[str, Any]:
    """Return the bounded deterministic adapter-facing status document."""

    return {
        "workflow_status_schema_version": WORKFLOW_STATUS_SCHEMA_VERSION,
        "initialized": state.initialized,
        "available_tasks": list(state.available_tasks),
        "selected_task": state.selected_task,
        "phase": state.phase.value,
        "outcome": state.outcome.value,
        "next_actions": [
            {
                "kind": action.kind.value,
                "text": action.text,
                "command": action.command,
            }
            for action in state.next_actions
        ],
        "lineage": {
            key: (
                state.lineage_freshness[key].value
                if state.lineage_freshness[key] is not None
                else None
            )
            for key in LINEAGE_KEYS
        },
        "generated_handoff": {
            AGENT_WORKSET_FILENAME: _artifact_value(state.generated_workset),
            "context-manifest.yaml": _artifact_value(state.context_manifest),
        },
        "evidence_report_status": _artifact_value(state.evidence_report),
        "validation_currentness": (
            state.validation_currentness.value
            if state.validation_currentness is not None
            else None
        ),
        "validation_clean": state.validation_clean,
        "validation_blocker_count": state.validation_blocker_count,
        "validation_warning_count": state.validation_warning_count,
        "integrity_clean": state.integrity_clean,
        "findings": [
            {
                "code": finding.code,
                "level": finding.level.value,
                "path": finding.path,
                "message": finding.message,
            }
            for finding in state.findings
        ],
    }


def render_workflow_json(state: WorkflowState) -> str:
    return json.dumps(
        workflow_status_document(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _error_json(error: WorkflowResolutionError) -> str:
    return json.dumps(
        {
            "workflow_status_schema_version": WORKFLOW_STATUS_SCHEMA_VERSION,
            "error": {"code": error.code, "message": error.message},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def status_project(
    root: Path,
    task_slug: str | None = None,
    *,
    json_output: bool = False,
) -> tuple[int, list[str]]:
    """Resolve and render status without modifying repository state."""

    try:
        state = resolve_workflow_state(root, task_slug)
    except WorkflowResolutionError as exc:
        if json_output:
            return 2, [_error_json(exc)]
        return 2, ["AI SDLC Harness", f"error: {exc.message}"]

    if json_output:
        return 0, [render_workflow_json(state)]
    rendered = render_workflow_status(state)
    legacy = _legacy_summary(root, state)
    if not legacy:
        return 0, rendered
    next_index = rendered.index("next:") - 1 if "next:" in rendered else len(rendered)
    return 0, [
        *rendered[:next_index],
        "",
        "details:",
        *legacy,
        *rendered[next_index:],
    ]
