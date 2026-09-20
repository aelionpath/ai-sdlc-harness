"""Pure, repository-derived workflow navigation.

The resolver in this module is deliberately read-only.  It derives workflow
state from the current repository artifacts and the existing integrity,
Lineage, and validation subsystems; it never records a selected task or a
last-command marker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

import yaml

from .constants import (
    AGENT_WORKSET_FILENAME,
    CONTEXT_MANIFEST_FILENAME,
    EVIDENCE_REPORT_FILENAME,
    HARNESS_DIR,
    TASK_ARTIFACT_FILENAMES,
    TASK_GENERATED_DIRNAME,
    TASK_SLUG_PATTERN,
)
from .context import FindingLevel, FreshnessState
from .files import PathSafetyError, resolve_managed_output_under_root
from .lineage import LineageResolutionResult, resolve_derived_freshness
from .manifest import ManifestValidationError, load_manifest_model
from .validation import (
    ArtifactStatus,
    ValidationCurrentnessResult,
    ValidationCurrentnessState,
    ValidationInspectionError,
    ValidationInspectionResult,
    capture_validation_snapshot,
    inspect_validation_snapshot,
    resolve_validation_currentness,
)
from .verify import verify_project


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
LINEAGE_KEYS = (
    "spec",
    "requirements_projection",
    "preflight",
    "test_contract_review",
)
_FINDING_LIMIT = 5


class WorkflowResolutionError(ValueError):
    """Raised for a deterministic user-facing status selection error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class WorkflowOutcomeCategory(str, Enum):
    NEXT = "NEXT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"
    COMPLETE = "COMPLETE"


class WorkflowPhase(str, Enum):
    UNINITIALIZED = "uninitialized"
    TASK_SELECTION = "task_selection"
    TASK_DEFINITION = "task_definition"
    PREFLIGHT = "preflight"
    SPECIFICATION = "specification"
    TEST_CONTRACT = "test_contract"
    IMPLEMENTATION_HANDOFF = "implementation_handoff"
    IMPLEMENTATION = "implementation"
    EVIDENCE = "evidence"
    VALIDATION = "validation"
    REVIEW = "review"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class WorkflowActionKind(str, Enum):
    COMMAND = "command"
    HUMAN = "human"


class WorkflowArtifactState(str, Enum):
    MISSING = "missing"
    PRESENT = "present"
    UNSAFE = "unsafe"


class WorkflowFindingLevel(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"


@dataclass(frozen=True)
class WorkflowAction:
    kind: WorkflowActionKind
    text: str
    command: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WorkflowActionKind):
            raise TypeError("workflow action kind must use WorkflowActionKind")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("workflow action text must be non-empty")
        if self.kind is WorkflowActionKind.COMMAND:
            if not isinstance(self.command, str) or not self.command:
                raise ValueError("command workflow action requires a command")
        elif self.command is not None:
            raise ValueError("human workflow action must not contain a command")


@dataclass(frozen=True)
class WorkflowFinding:
    code: str
    level: WorkflowFindingLevel
    message: str
    path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("workflow finding code must be non-empty")
        if not isinstance(self.level, WorkflowFindingLevel):
            raise TypeError("workflow finding level must use WorkflowFindingLevel")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("workflow finding message must be non-empty")
        if self.path is not None:
            path = PurePosixPath(self.path)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError("workflow finding path must be repository-relative")


@dataclass(frozen=True)
class WorkflowState:
    initialized: bool
    available_tasks: tuple[str, ...]
    selected_task: str | None
    phase: WorkflowPhase
    outcome: WorkflowOutcomeCategory
    next_actions: tuple[WorkflowAction, ...]
    lineage_freshness: Mapping[str, FreshnessState | None]
    generated_workset: WorkflowArtifactState | None
    context_manifest: WorkflowArtifactState | None
    evidence_report: WorkflowArtifactState | None
    validation_currentness: ValidationCurrentnessState | None
    validation_clean: bool | None
    validation_blocker_count: int | None
    validation_warning_count: int | None
    integrity_clean: bool | None
    findings: tuple[WorkflowFinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.available_tasks, tuple):
            raise TypeError("available tasks must be an immutable tuple")
        if self.available_tasks != tuple(sorted(set(self.available_tasks))):
            raise ValueError("available tasks must be unique and sorted")
        if self.selected_task is not None and self.selected_task not in self.available_tasks:
            raise ValueError("selected task must be one of the available tasks")
        if not isinstance(self.phase, WorkflowPhase):
            raise TypeError("workflow phase must use WorkflowPhase")
        if not isinstance(self.outcome, WorkflowOutcomeCategory):
            raise TypeError("workflow outcome must use WorkflowOutcomeCategory")
        if not isinstance(self.next_actions, tuple) or not all(
            isinstance(item, WorkflowAction) for item in self.next_actions
        ):
            raise TypeError("workflow next actions must be an immutable tuple")
        supplied = dict(self.lineage_freshness)
        if set(supplied) != set(LINEAGE_KEYS):
            raise ValueError("workflow Lineage mapping must contain exactly four keys")
        canonical: dict[str, FreshnessState | None] = {}
        for key in LINEAGE_KEYS:
            value = supplied[key]
            if value is not None and value not in {
                FreshnessState.FRESH,
                FreshnessState.STALE,
                FreshnessState.MISSING,
                FreshnessState.UNKNOWN,
            }:
                raise ValueError("workflow Lineage value is not selector-facing")
            canonical[key] = value
        object.__setattr__(self, "lineage_freshness", MappingProxyType(canonical))
        if not isinstance(self.findings, tuple) or not all(
            isinstance(item, WorkflowFinding) for item in self.findings
        ):
            raise TypeError("workflow findings must be an immutable tuple")


def _empty_lineage() -> dict[str, None]:
    return {key: None for key in LINEAGE_KEYS}


def _action(command: str, text: str | None = None) -> WorkflowAction:
    return WorkflowAction(WorkflowActionKind.COMMAND, text or command, command)


def _human(text: str) -> WorkflowAction:
    return WorkflowAction(WorkflowActionKind.HUMAN, text)


def _state(
    *,
    initialized: bool,
    available_tasks: tuple[str, ...],
    selected_task: str | None,
    phase: WorkflowPhase,
    outcome: WorkflowOutcomeCategory,
    next_actions: tuple[WorkflowAction, ...],
    lineage_freshness: Mapping[str, FreshnessState | None] | None = None,
    generated_workset: WorkflowArtifactState | None = None,
    context_manifest: WorkflowArtifactState | None = None,
    evidence_report: WorkflowArtifactState | None = None,
    validation_currentness: ValidationCurrentnessState | None = None,
    validation_clean: bool | None = None,
    validation_blocker_count: int | None = None,
    validation_warning_count: int | None = None,
    integrity_clean: bool | None = None,
    findings: tuple[WorkflowFinding, ...] = (),
) -> WorkflowState:
    return WorkflowState(
        initialized=initialized,
        available_tasks=available_tasks,
        selected_task=selected_task,
        phase=phase,
        outcome=outcome,
        next_actions=next_actions,
        lineage_freshness=lineage_freshness or _empty_lineage(),
        generated_workset=generated_workset,
        context_manifest=context_manifest,
        evidence_report=evidence_report,
        validation_currentness=validation_currentness,
        validation_clean=validation_clean,
        validation_blocker_count=validation_blocker_count,
        validation_warning_count=validation_warning_count,
        integrity_clean=integrity_clean,
        findings=findings,
    )


def _discover_tasks(root: Path) -> tuple[str, ...]:
    tasks_root = root / HARNESS_DIR / "tasks"
    try:
        if not tasks_root.is_dir():
            return ()
        tasks = tuple(
            sorted(
                child.name
                for child in tasks_root.iterdir()
                if child.is_dir()
                and not child.is_symlink()
                and TASK_SLUG_RE.fullmatch(child.name) is not None
            )
        )
    except OSError:
        raise WorkflowResolutionError(
            "task_discovery_failed",
            "Task directories could not be inspected safely.",
        ) from None
    return tasks


def _inspect_initialization(root: Path) -> tuple[str, WorkflowFinding | None]:
    """Classify absent/incomplete initialization separately from unsafe state."""

    harness_path = root / HARNESS_DIR
    try:
        if harness_path.is_symlink():
            raise PathSafetyError("Harness directory must not be a symlink")
        if not harness_path.exists():
            return "incomplete", None
        if not harness_path.is_dir():
            raise PathSafetyError("Harness path must be a directory")
    except (OSError, RuntimeError, PathSafetyError):
        return (
            "unsafe",
            WorkflowFinding(
                "initialization_path_unsafe",
                WorkflowFindingLevel.BLOCKER,
                "Existing .harness path is unsafe or not a directory.",
                HARNESS_DIR,
            ),
        )

    marker_paths = (
        PurePosixPath(HARNESS_DIR) / "config.yaml",
        PurePosixPath(HARNESS_DIR) / "manifest.json",
    )
    resolved_markers: dict[str, Path] = {}
    for marker in marker_paths:
        try:
            target = resolve_managed_output_under_root(root, marker)
            if not target.exists():
                return "incomplete", None
            if not target.is_file():
                raise PathSafetyError("Initialization marker must be a regular file")
            target.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return "incomplete", None
        except (OSError, RuntimeError, UnicodeError, PathSafetyError):
            return (
                "unsafe",
                WorkflowFinding(
                    "initialization_marker_unsafe",
                    WorkflowFindingLevel.BLOCKER,
                    f"Initialization marker is unsafe or unreadable: {marker.as_posix()}.",
                    marker.as_posix(),
                ),
            )
        resolved_markers[marker.name] = target

    try:
        config = yaml.safe_load(
            resolved_markers["config.yaml"].read_text(encoding="utf-8")
        )
        if not isinstance(config, dict):
            raise ValueError("Harness config must be a mapping")
        load_manifest_model(resolved_markers["manifest.json"])
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, ManifestValidationError):
        return (
            "unsafe",
            WorkflowFinding(
                "initialization_metadata_invalid",
                WorkflowFindingLevel.BLOCKER,
                "Initialization config or manifest is unreadable or structurally invalid.",
            ),
        )
    return "ready", None


def _task_source_safety_findings(
    root: Path, task_slug: str
) -> tuple[WorkflowFinding, ...]:
    """Reject unsafe/non-regular source paths without treating absence as drift."""

    findings: list[WorkflowFinding] = []
    task_root = PurePosixPath(HARNESS_DIR) / "tasks" / task_slug
    for filename in TASK_ARTIFACT_FILENAMES:
        path = task_root / filename
        try:
            target = resolve_managed_output_under_root(root, path)
            if not target.exists():
                continue
            if not target.is_file():
                raise PathSafetyError("Task source must be a regular file")
            target.read_bytes().decode("utf-8")
        except (OSError, RuntimeError, UnicodeError, PathSafetyError):
            findings.append(
                WorkflowFinding(
                    "task_source_unsafe",
                    WorkflowFindingLevel.BLOCKER,
                    f"Task source is unsafe, non-regular, or unreadable: {path.as_posix()}.",
                    path.as_posix(),
                )
            )
    return _bounded_findings(tuple(findings))


def _sanitize_integrity_message(root: Path, message: str) -> str:
    cleaned = message.removeprefix("- ")
    root_texts = {str(root), str(root.resolve())}
    for root_text in sorted(root_texts, key=len, reverse=True):
        cleaned = cleaned.replace(root_text, ".")
        cleaned = cleaned.replace(root_text.replace("\\", "/"), ".")
    return cleaned


def _bounded_findings(findings: tuple[WorkflowFinding, ...]) -> tuple[WorkflowFinding, ...]:
    if len(findings) <= _FINDING_LIMIT:
        return findings
    remaining = len(findings) - _FINDING_LIMIT
    return (
        *findings[:_FINDING_LIMIT],
        WorkflowFinding(
            "additional_findings",
            WorkflowFindingLevel.WARNING,
            f"{remaining} additional finding(s) omitted.",
        ),
    )


def _integrity_findings(root: Path, messages: list[str]) -> tuple[WorkflowFinding, ...]:
    details = messages[1:] if messages and messages[0].startswith("AI SDLC Harness") else messages
    findings = tuple(
        WorkflowFinding(
            f"repository_integrity_{index}",
            WorkflowFindingLevel.BLOCKER,
            _sanitize_integrity_message(root, message),
        )
        for index, message in enumerate(details, start=1)
    )
    if not findings:
        findings = (
            WorkflowFinding(
                "repository_integrity_failed",
                WorkflowFindingLevel.BLOCKER,
                "Repository integrity verification failed.",
            ),
        )
    return _bounded_findings(findings)


def _artifact_state(root: Path, path: PurePosixPath) -> WorkflowArtifactState:
    try:
        target = resolve_managed_output_under_root(root, path)
        if not target.exists():
            return WorkflowArtifactState.MISSING
        if not target.is_file():
            return WorkflowArtifactState.UNSAFE
        target.read_bytes().decode("utf-8")
    except (OSError, RuntimeError, UnicodeError, PathSafetyError):
        return WorkflowArtifactState.UNSAFE
    return WorkflowArtifactState.PRESENT


def _artifact_finding(code: str, path: PurePosixPath) -> WorkflowFinding:
    return WorkflowFinding(
        code,
        WorkflowFindingLevel.BLOCKER,
        f"Artifact is unsafe, non-regular, or unreadable: {path.as_posix()}.",
        path.as_posix(),
    )


def _artifact_by_name(result: ValidationInspectionResult, name: str) -> ArtifactStatus:
    return next(item for item in result.artifact_statuses if item.filename == name)


def _section_has_substantive_content(text: str, heading: str) -> bool:
    """Return whether one level-two Markdown section contains user intent."""

    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                break
            in_section = stripped[3:].strip().casefold() == heading.casefold()
            continue
        if not in_section:
            continue
        if (
            not stripped
            or stripped.startswith("#")
            or TODO_RE.search(stripped) is not None
            or stripped.startswith(("- [ ]", "* [ ]"))
            or stripped in {"-", "*", "[]"}
        ):
            continue
        return True
    return False


def _workflow_source_substantive(status: ArtifactStatus) -> bool:
    if not status.present or not status.readable:
        return False
    if status.filename == "task.md":
        return _section_has_substantive_content(
            status.text, "Implementation Boundary"
        )
    if status.filename == "acceptance.md":
        return _section_has_substantive_content(
            status.text, "Requirements And Acceptance Criteria"
        )
    return status.substantive


def _human_gate_actions(
    filenames: tuple[str, ...], future_command: str
) -> tuple[WorkflowAction, ...]:
    joined = " and ".join(filenames)
    return (
        _human(f"Complete the substantive source content in {joined}."),
        _action(future_command, f"Then run {future_command}"),
    )


def _lineage_findings(result: LineageResolutionResult) -> tuple[WorkflowFinding, ...]:
    converted = tuple(
        WorkflowFinding(
            finding.code,
            (
                WorkflowFindingLevel.BLOCKER
                if finding.level is FindingLevel.BLOCKER
                else WorkflowFindingLevel.WARNING
            ),
            finding.message,
            finding.path,
        )
        for finding in result.findings
        if finding.level in {FindingLevel.BLOCKER, FindingLevel.WARNING}
    )
    return _bounded_findings(converted)


def _validation_findings(
    result: ValidationCurrentnessResult,
) -> tuple[WorkflowFinding, ...]:
    return _bounded_findings(
        tuple(
            WorkflowFinding(
                finding.code,
                (
                    WorkflowFindingLevel.BLOCKER
                    if finding.level == "blocker"
                    else WorkflowFindingLevel.WARNING
                ),
                finding.message,
                finding.path,
            )
            for finding in result.findings
        )
    )


def resolve_workflow_state(
    repository_root: Path,
    task_slug: str | None = None,
) -> WorkflowState:
    """Resolve immutable workflow state from current repository artifacts."""

    if not isinstance(repository_root, Path):
        raise WorkflowResolutionError(
            "invalid_repository_root", "Repository root must be a pathlib.Path."
        )
    try:
        root = repository_root.resolve()
        root_is_dir = root.is_dir()
    except (OSError, RuntimeError):
        root_is_dir = False
    if not root_is_dir:
        raise WorkflowResolutionError(
            "invalid_repository_root", "Repository root is not an inspectable directory."
        )

    initialization, initialization_finding = _inspect_initialization(root)
    if initialization == "incomplete":
        return _state(
            initialized=False,
            available_tasks=(),
            selected_task=None,
            phase=WorkflowPhase.UNINITIALIZED,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(_action("ai-sdlc init"),),
        )
    if initialization == "unsafe":
        assert initialization_finding is not None
        return _state(
            initialized=False,
            available_tasks=(),
            selected_task=None,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(
                _human("Correct the unsafe existing Harness initialization state."),
            ),
            integrity_clean=False,
            findings=(initialization_finding,),
        )

    available_tasks = _discover_tasks(root)
    selected_task = task_slug
    if selected_task is not None:
        if TASK_SLUG_RE.fullmatch(selected_task) is None:
            raise WorkflowResolutionError(
                "unsafe_task_slug",
                "Task slug is unsafe; use the safe slug created by `ai-sdlc task start`.",
            )
        if selected_task not in available_tasks:
            raise WorkflowResolutionError(
                "task_not_found",
                f"Task folder does not exist: .harness/tasks/{selected_task}",
            )
    elif len(available_tasks) == 1:
        selected_task = available_tasks[0]
    elif not available_tasks:
        return _state(
            initialized=True,
            available_tasks=(),
            selected_task=None,
            phase=WorkflowPhase.TASK_SELECTION,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(
                _action(
                    'ai-sdlc task start "<task>"',
                    "Start a task with a descriptive title.",
                ),
            ),
        )
    else:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=None,
            phase=WorkflowPhase.TASK_SELECTION,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(
                _action(
                    "ai-sdlc status --task <slug>",
                    "Choose one of the listed task slugs.",
                ),
            ),
        )

    assert selected_task is not None
    source_safety_findings = _task_source_safety_findings(root, selected_task)
    if source_safety_findings:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the unsafe task source path."),),
            integrity_clean=False,
            findings=source_safety_findings,
        )

    try:
        inspection = inspect_validation_snapshot(
            capture_validation_snapshot(root, selected_task)
        )
    except ValidationInspectionError:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the task artifacts so they can be inspected safely."),),
            integrity_clean=None,
            findings=(
                WorkflowFinding(
                    "validation_inspection_unavailable",
                    WorkflowFindingLevel.BLOCKER,
                    "Task artifacts could not be inspected under the closed validation contract.",
                ),
            ),
        )

    task_gate = tuple(
        name
        for name in ("task.md", "acceptance.md")
        if not _workflow_source_substantive(_artifact_by_name(inspection, name))
    )
    if task_gate:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.TASK_DEFINITION,
            outcome=WorkflowOutcomeCategory.REVIEW_REQUIRED,
            next_actions=_human_gate_actions(
                task_gate, f"ai-sdlc preflight --task {selected_task}"
            ),
            integrity_clean=None,
        )

    try:
        verify_code, verify_messages = verify_project(
            root,
            allow_human_source_readiness=True,
        )
    except (OSError, RuntimeError, ValueError):
        verify_code, verify_messages = 1, [
            "Repository integrity could not be inspected safely."
        ]
    if verify_code != 0:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(
                _human("Correct the reported repository integrity findings."),
            ),
            integrity_clean=False,
            findings=_integrity_findings(root, verify_messages),
        )

    try:
        lineage = resolve_derived_freshness(root, selected_task)
    except (OSError, RuntimeError, ValueError):
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the repository state so Lineage can be resolved."),),
            integrity_clean=None,
            findings=(
                WorkflowFinding(
                    "lineage_resolution_failed",
                    WorkflowFindingLevel.BLOCKER,
                    "Lineage freshness could not be resolved under the closed contract.",
                ),
            ),
        )
    freshness = lineage.derived_freshness
    lineage_findings = _lineage_findings(lineage)
    if any(value is FreshnessState.UNKNOWN for value in freshness.values()) and any(
        finding.level is WorkflowFindingLevel.BLOCKER for finding in lineage_findings
    ):
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the reported Lineage blocker, then rerun status."),),
            lineage_freshness=freshness,
            integrity_clean=None,
            findings=lineage_findings,
        )

    if freshness["preflight"] is not FreshnessState.FRESH:
        command = f"ai-sdlc preflight --task {selected_task}"
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.PREFLIGHT,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(_action(command),),
            lineage_freshness=freshness,
            integrity_clean=None,
            findings=lineage_findings,
        )

    if any(
        freshness[key] is not FreshnessState.FRESH
        for key in ("spec", "requirements_projection")
    ):
        command = f"ai-sdlc spec --task {selected_task}"
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.SPECIFICATION,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(_action(command),),
            lineage_freshness=freshness,
            integrity_clean=None,
            findings=lineage_findings,
        )

    test_gate = tuple(
        name
        for name in ("test-contract.md", "verification.md")
        if not _artifact_by_name(inspection, name).substantive
    )
    if test_gate:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.TEST_CONTRACT,
            outcome=WorkflowOutcomeCategory.REVIEW_REQUIRED,
            next_actions=_human_gate_actions(
                test_gate, f"ai-sdlc test-contract --task {selected_task}"
            ),
            lineage_freshness=freshness,
            integrity_clean=None,
            findings=lineage_findings,
        )

    if freshness["test_contract_review"] is not FreshnessState.FRESH:
        command = f"ai-sdlc test-contract --task {selected_task}"
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.TEST_CONTRACT,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(_action(command),),
            lineage_freshness=freshness,
            integrity_clean=None,
            findings=lineage_findings,
        )

    task_root = PurePosixPath(HARNESS_DIR) / "tasks" / selected_task
    workset_path = task_root / TASK_GENERATED_DIRNAME / AGENT_WORKSET_FILENAME
    context_path = task_root / TASK_GENERATED_DIRNAME / CONTEXT_MANIFEST_FILENAME
    workset_state = _artifact_state(root, workset_path)
    context_state = _artifact_state(root, context_path)
    unsafe_handoff = tuple(
        (code, path)
        for code, path, state in (
            ("generated_workset_unsafe", workset_path, workset_state),
            ("context_manifest_unsafe", context_path, context_state),
        )
        if state is WorkflowArtifactState.UNSAFE
    )
    if unsafe_handoff:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the unsafe generated handoff output."),),
            lineage_freshness=freshness,
            generated_workset=workset_state,
            context_manifest=context_state,
            integrity_clean=None,
            findings=tuple(_artifact_finding(code, path) for code, path in unsafe_handoff),
        )
    if WorkflowArtifactState.MISSING in {workset_state, context_state}:
        command = f"ai-sdlc generate --task {selected_task}"
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.IMPLEMENTATION_HANDOFF,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(_action(command),),
            lineage_freshness=freshness,
            generated_workset=workset_state,
            context_manifest=context_state,
            integrity_clean=None,
            findings=lineage_findings,
        )

    evidence = _artifact_by_name(inspection, "evidence.md")
    implementation_ready = (
        evidence.substantive
        and inspection.verification_command == "present"
        and inspection.test_result_evidence == "present"
    )
    if not implementation_ready:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.IMPLEMENTATION,
            outcome=WorkflowOutcomeCategory.REVIEW_REQUIRED,
            next_actions=(
                _human(
                    "Implement the task using the generated agent-workset.md handoff."
                ),
                _human(
                    "Record implementation evidence in evidence.md and verification results in verification.md."
                ),
            ),
            lineage_freshness=freshness,
            generated_workset=workset_state,
            context_manifest=context_state,
            integrity_clean=None,
            findings=lineage_findings,
        )

    evidence_path = task_root / EVIDENCE_REPORT_FILENAME
    evidence_state = _artifact_state(root, evidence_path)
    if evidence_state is WorkflowArtifactState.UNSAFE:
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the unsafe evidence report."),),
            lineage_freshness=freshness,
            generated_workset=workset_state,
            context_manifest=context_state,
            evidence_report=evidence_state,
            integrity_clean=None,
            findings=(_artifact_finding("evidence_report_unsafe", evidence_path),),
        )
    if evidence_state is WorkflowArtifactState.MISSING:
        command = f"ai-sdlc evidence --task {selected_task}"
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.EVIDENCE,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(_action(command),),
            lineage_freshness=freshness,
            generated_workset=workset_state,
            context_manifest=context_state,
            evidence_report=evidence_state,
            integrity_clean=None,
            findings=lineage_findings,
        )

    try:
        currentness = resolve_validation_currentness(root, selected_task)
    except (OSError, RuntimeError, ValueError):
        return _state(
            initialized=True,
            available_tasks=available_tasks,
            selected_task=selected_task,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(
                _human("Correct the repository state so validation currentness can be inspected."),
            ),
            lineage_freshness=freshness,
            generated_workset=workset_state,
            context_manifest=context_state,
            evidence_report=evidence_state,
            integrity_clean=None,
            findings=(
                WorkflowFinding(
                    "validation_currentness_unavailable",
                    WorkflowFindingLevel.BLOCKER,
                    "Validation currentness could not be resolved under the closed contract.",
                ),
            ),
        )
    currentness_findings = _validation_findings(currentness)
    common = dict(
        initialized=True,
        available_tasks=available_tasks,
        selected_task=selected_task,
        lineage_freshness=freshness,
        generated_workset=workset_state,
        context_manifest=context_state,
        evidence_report=evidence_state,
        validation_currentness=currentness.state,
        integrity_clean=True,
    )
    validate_command = f"ai-sdlc validate --task {selected_task}"
    if currentness.state in {
        ValidationCurrentnessState.MISSING,
        ValidationCurrentnessState.STALE,
    }:
        return _state(
            **common,
            phase=WorkflowPhase.VALIDATION,
            outcome=WorkflowOutcomeCategory.NEXT,
            next_actions=(_action(validate_command),),
            findings=currentness_findings,
        )
    if currentness.state is ValidationCurrentnessState.UNKNOWN:
        has_blocker = any(
            finding.level is WorkflowFindingLevel.BLOCKER
            for finding in currentness_findings
        )
        return _state(
            **common,
            phase=WorkflowPhase.BLOCKED if has_blocker else WorkflowPhase.VALIDATION,
            outcome=(
                WorkflowOutcomeCategory.BLOCKED
                if has_blocker
                else WorkflowOutcomeCategory.NEXT
            ),
            next_actions=(
                _human("Correct the validation currentness blocker.")
                if has_blocker
                else _action(validate_command)
            ,),
            findings=currentness_findings,
        )
    if currentness.state is ValidationCurrentnessState.INVALID:
        return _state(
            **common,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the invalid validation or integrity state."),),
            findings=currentness_findings,
        )

    semantic_findings = tuple(
        WorkflowFinding(
            f"validation_semantic_{index}",
            (
                WorkflowFindingLevel.BLOCKER
                if finding.level == "blocker"
                else WorkflowFindingLevel.WARNING
            ),
            finding.message,
        )
        for index, finding in enumerate(inspection.findings, start=1)
        if finding.level in {"blocker", "warning"}
    )
    semantic_findings = _bounded_findings(semantic_findings)
    semantic_common = dict(
        **common,
        validation_clean=inspection.clean,
        validation_blocker_count=inspection.blocker_count,
        validation_warning_count=inspection.warning_count,
    )
    if inspection.blocker_count:
        return _state(
            **semantic_common,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(_human("Correct the validation blockers, then rerun validation."),),
            findings=semantic_findings,
        )
    if inspection.warning_count:
        return _state(
            **semantic_common,
            phase=WorkflowPhase.REVIEW,
            outcome=WorkflowOutcomeCategory.REVIEW_REQUIRED,
            next_actions=(
                _human(
                    "Review and resolve or accept the current validation "
                    + ("warning." if inspection.warning_count == 1 else "warnings.")
                ),
            ),
            findings=semantic_findings,
        )

    try:
        verify_code, verify_messages = verify_project(root)
    except (OSError, RuntimeError, ValueError):
        verify_code, verify_messages = 1, [
            "Repository integrity could not be inspected safely."
        ]
    if verify_code != 0:
        blocked_common = dict(semantic_common)
        blocked_common["integrity_clean"] = False
        return _state(
            **blocked_common,
            phase=WorkflowPhase.BLOCKED,
            outcome=WorkflowOutcomeCategory.BLOCKED,
            next_actions=(
                _human("Correct the reported repository integrity findings."),
            ),
            findings=_integrity_findings(root, verify_messages),
        )

    complete_common = dict(semantic_common)
    complete_common["integrity_clean"] = True
    return _state(
        **complete_common,
        phase=WorkflowPhase.COMPLETE,
        outcome=WorkflowOutcomeCategory.COMPLETE,
        next_actions=(_human("Human review / normal delivery process."),),
    )
