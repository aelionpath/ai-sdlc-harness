"""Pure validation snapshot capture and semantic inspection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

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
from .context import FreshnessState
from .detect import detect_documented_test_frameworks
from .files import (
    PathSafetyError,
    resolve_managed_output_under_root,
    resolve_under_root,
    sha256_bytes,
)
from .lineage import (
    InspectedDependencyState,
    InspectedObservationState,
    InspectedProvenanceState,
    LineageValidationError,
    RepositoryObservationDefinition,
    ResolvedProducerOutputDefinition,
    build_provenance_record,
    decide_freshness,
    validate_manifest_provenance,
)
from .manifest import (
    GeneratedArtifactProvenance,
    ManifestV1,
    ManifestV2,
    ManifestValidationError,
    ProvenanceDependency,
    ProvenanceObservation,
    load_manifest_model,
)
from .requirements import RequirementsReadResult, parse_requirements_text


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
FINDING_LINE_RE = re.compile(
    r"^\s*-\s*(blocker|warning|info):\s+(.+?)\s*$",
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

_GENERATED_DEPENDENCY_NAMES = (
    PREFLIGHT_FILENAME,
    SPEC_FILENAME,
    REQUIREMENTS_FILENAME,
    TEST_CONTRACT_REVIEW_FILENAME,
    f"{TASK_GENERATED_DIRNAME}/{AGENT_WORKSET_FILENAME}",
    EVIDENCE_REPORT_FILENAME,
)
_PRIOR_FINDING_REPORTS = frozenset(
    {PREFLIGHT_FILENAME, TEST_CONTRACT_REVIEW_FILENAME, EVIDENCE_REPORT_FILENAME}
)
_FIXED_INFO_MESSAGES = (
    "validation generated deterministically.",
    "no optional packs selected.",
    "validate does not run tests.",
    "validate does not inspect target code deeply.",
    "validate does not prove correctness, security, or compliance.",
    "validate does not call AI models.",
    "validate does not enforce packs.",
)
VALIDATION_PRODUCER_VERSION = 1


class ValidationInspectionError(ValueError):
    """Raised when a validation snapshot or result violates its contract."""


class ValidationDependencyState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    NOT_REGULAR = "not_regular"


class ValidationObservationPredicate(str, Enum):
    IS_FILE = "is_file"
    IS_DIR = "is_dir"


@dataclass(frozen=True)
class CapturedValidationDependency:
    path: PurePosixPath
    state: ValidationDependencyState
    raw_bytes: bytes | None
    decoded_text: str | None
    sha256: str | None

    def __post_init__(self) -> None:
        _require_safe_path(self.path, "validation dependency path")
        if not isinstance(self.state, ValidationDependencyState):
            raise ValidationInspectionError(
                "validation dependency state must be a ValidationDependencyState"
            )
        if self.state is ValidationDependencyState.PRESENT:
            if not isinstance(self.raw_bytes, bytes):
                raise ValidationInspectionError(
                    "present validation dependency requires exact raw bytes"
                )
            if not isinstance(self.decoded_text, str):
                raise ValidationInspectionError(
                    "present validation dependency requires decoded UTF-8 text"
                )
            if not isinstance(self.sha256, str):
                raise ValidationInspectionError(
                    "present validation dependency requires a SHA-256 digest"
                )
            try:
                decoded = self.raw_bytes.decode("utf-8")
            except UnicodeError as exc:
                raise ValidationInspectionError(
                    "present validation dependency raw bytes must be valid UTF-8"
                ) from exc
            if decoded != self.decoded_text:
                raise ValidationInspectionError(
                    "validation dependency decoded text does not match exact raw bytes"
                )
            if sha256_bytes(self.raw_bytes) != self.sha256:
                raise ValidationInspectionError(
                    "validation dependency digest does not match exact raw bytes"
                )
        elif any(
            value is not None
            for value in (self.raw_bytes, self.decoded_text, self.sha256)
        ):
            raise ValidationInspectionError(
                "non-present validation dependency must not contain bytes, text, or hash"
            )


@dataclass(frozen=True)
class CapturedValidationObservation:
    path: PurePosixPath
    predicate: ValidationObservationPredicate
    result: bool
    inspectable: bool = True

    def __post_init__(self) -> None:
        _require_safe_path(self.path, "validation observation path")
        if not isinstance(self.predicate, ValidationObservationPredicate):
            raise ValidationInspectionError(
                "validation observation predicate must be a ValidationObservationPredicate"
            )
        if not isinstance(self.result, bool):
            raise ValidationInspectionError("validation observation result must be Boolean")
        if not isinstance(self.inspectable, bool):
            raise ValidationInspectionError(
                "validation observation inspectable state must be Boolean"
            )


@dataclass(frozen=True)
class ValidationInspectionSnapshot:
    task_slug: str
    dependencies: tuple[CapturedValidationDependency, ...]
    repository_observations: tuple[CapturedValidationObservation, ...]

    def __post_init__(self) -> None:
        _validate_task_slug(self.task_slug)
        if not isinstance(self.dependencies, tuple):
            raise ValidationInspectionError("validation snapshot dependencies must be a tuple")
        if not all(
            isinstance(item, CapturedValidationDependency)
            for item in self.dependencies
        ):
            raise ValidationInspectionError(
                "validation snapshot dependencies must contain captured dependencies"
            )
        expected_paths = validation_dependency_paths(self.task_slug)
        actual_paths = tuple(item.path for item in self.dependencies)
        if actual_paths != expected_paths:
            raise ValidationInspectionError(
                "validation snapshot dependencies must match the closed canonical order"
            )
        if not isinstance(self.repository_observations, tuple):
            raise ValidationInspectionError(
                "validation snapshot repository observations must be a tuple"
            )
        if not all(
            isinstance(item, CapturedValidationObservation)
            for item in self.repository_observations
        ):
            raise ValidationInspectionError(
                "validation snapshot repository observations must contain captured observations"
            )
        expected_observations = validation_observation_definitions(self.task_slug)
        actual_observations = tuple(
            (item.path, item.predicate) for item in self.repository_observations
        )
        if actual_observations != expected_observations:
            raise ValidationInspectionError(
                "validation snapshot repository observations must match the closed canonical order"
            )


class ValidationCurrentnessState(str, Enum):
    MISSING = "missing"
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"
    INVALID = "invalid"


@dataclass(frozen=True)
class ValidationCurrentnessFinding:
    code: str
    level: str
    path: str | None
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValidationInspectionError(
                "validation currentness finding code must be non-empty text"
            )
        if self.level not in {"warning", "blocker"}:
            raise ValidationInspectionError(
                "validation currentness finding level must be warning or blocker"
            )
        if self.path is not None:
            _require_safe_path(
                PurePosixPath(self.path),
                "validation currentness finding path",
            )
        if not isinstance(self.message, str) or not self.message:
            raise ValidationInspectionError(
                "validation currentness finding message must be non-empty text"
            )


@dataclass(frozen=True)
class ValidationCurrentnessResult:
    task_slug: str
    state: ValidationCurrentnessState
    findings: tuple[ValidationCurrentnessFinding, ...]

    def __post_init__(self) -> None:
        _validate_task_slug(self.task_slug)
        if not isinstance(self.state, ValidationCurrentnessState):
            raise ValidationInspectionError(
                "validation currentness state must use ValidationCurrentnessState"
            )
        if not isinstance(self.findings, tuple) or not all(
            isinstance(item, ValidationCurrentnessFinding)
            for item in self.findings
        ):
            raise ValidationInspectionError(
                "validation currentness findings must be an immutable tuple"
            )
        object.__setattr__(
            self,
            "findings",
            tuple(sorted(set(self.findings), key=_currentness_finding_sort_key)),
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
class Finding:
    level: str
    message: str

    def __post_init__(self) -> None:
        if self.level not in {"blocker", "warning", "info"}:
            raise ValidationInspectionError("finding level must be blocker, warning, or info")
        if not isinstance(self.message, str) or not self.message:
            raise ValidationInspectionError("finding message must be non-empty text")


@dataclass(frozen=True)
class ValidationRequirementsStatus:
    present: bool
    readable: bool
    schema_valid: bool
    message: str
    requirement_count: int
    finding_count: int
    problems: tuple[str, ...]


@dataclass(frozen=True)
class ValidationInspectionResult:
    task_slug: str
    artifact_statuses: tuple[ArtifactStatus, ...]
    task_artifacts: tuple[ArtifactStatus, ...]
    generated_reports: tuple[ArtifactStatus, ...]
    findings: tuple[Finding, ...]
    blocker_count: int
    warning_count: int
    info_count: int
    clean: bool
    verification_command: str
    test_result_evidence: str
    todo_sources: tuple[str, ...]
    report_findings: tuple[ReportFinding, ...]
    workflow_checks: tuple[tuple[str, str], ...]
    structured_requirements: ValidationRequirementsStatus
    review_readiness: str
    review_readiness_reason: str
    recommended_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_task_slug(self.task_slug)
        tuple_fields = (
            self.artifact_statuses,
            self.task_artifacts,
            self.generated_reports,
            self.findings,
            self.todo_sources,
            self.report_findings,
            self.workflow_checks,
            self.recommended_actions,
        )
        if not all(isinstance(value, tuple) for value in tuple_fields):
            raise ValidationInspectionError(
                "validation inspection result collections must be tuples"
            )
        if self.artifact_statuses != (*self.task_artifacts, *self.generated_reports):
            raise ValidationInspectionError(
                "validation artifact statuses must contain task artifacts then generated reports"
            )
        counts = {
            "blocker": sum(item.level == "blocker" for item in self.findings),
            "warning": sum(item.level == "warning" for item in self.findings),
            "info": sum(item.level == "info" for item in self.findings),
        }
        if (self.blocker_count, self.warning_count, self.info_count) != (
            counts["blocker"],
            counts["warning"],
            counts["info"],
        ):
            raise ValidationInspectionError("validation finding counts are inconsistent")
        if self.clean is not (
            self.blocker_count == 0 and self.warning_count == 0
        ):
            raise ValidationInspectionError(
                "validation CLEAN must mean zero blockers and zero warnings"
            )


def _validate_task_slug(task_slug: str) -> None:
    if not isinstance(task_slug, str) or not task_slug.strip():
        raise ValidationInspectionError("task slug must not be empty")
    if "\x00" in task_slug or TASK_SLUG_RE.fullmatch(task_slug) is None:
        raise ValidationInspectionError(
            "task slug is unsafe; use the safe slug created by `ai-sdlc task start`"
        )


def _require_safe_path(path: PurePosixPath, label: str) -> None:
    if not isinstance(path, PurePosixPath):
        raise ValidationInspectionError(f"{label} must be a PurePosixPath")
    text = path.as_posix()
    if path.is_absolute() or text in {"", "."} or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ValidationInspectionError(f"{label} must be a safe repository-relative path")


def validation_dependency_paths(task_slug: str) -> tuple[PurePosixPath, ...]:
    """Return the exact thirteen semantic dependency paths in canonical order."""

    _validate_task_slug(task_slug)
    task_root = PurePosixPath(".harness") / "tasks" / task_slug
    return (
        *(task_root / filename for filename in TASK_ARTIFACT_FILENAMES),
        *(task_root / filename for filename in _GENERATED_DEPENDENCY_NAMES),
    )


def validation_observation_definitions(
    task_slug: str,
) -> tuple[tuple[PurePosixPath, ValidationObservationPredicate], ...]:
    """Return the exact seven repository observations in canonical order."""

    _validate_task_slug(task_slug)
    return (
        (PurePosixPath(".harness"), ValidationObservationPredicate.IS_DIR),
        (
            PurePosixPath(".harness/config.yaml"),
            ValidationObservationPredicate.IS_FILE,
        ),
        (
            PurePosixPath(".harness/manifest.json"),
            ValidationObservationPredicate.IS_FILE,
        ),
        (
            PurePosixPath(".harness") / "tasks" / task_slug,
            ValidationObservationPredicate.IS_DIR,
        ),
        (PurePosixPath("tests"), ValidationObservationPredicate.IS_DIR),
        (PurePosixPath("pytest.ini"), ValidationObservationPredicate.IS_FILE),
        (
            PurePosixPath(".github/workflows"),
            ValidationObservationPredicate.IS_DIR,
        ),
    )


def validation_provenance_definition_for_task(
    task_slug: str,
) -> ResolvedProducerOutputDefinition:
    """Return the separate closed validation workflow provenance definition."""

    _validate_task_slug(task_slug)
    report_path = (
        PurePosixPath(".harness")
        / "tasks"
        / task_slug
        / VALIDATION_REPORT_FILENAME
    )
    return ResolvedProducerOutputDefinition(
        registry_id="validation_report",
        producer_command="validate",
        producer_version=VALIDATION_PRODUCER_VERSION,
        task_slug=task_slug,
        output_path=report_path.as_posix(),
        dependencies=tuple(
            path.as_posix() for path in validation_dependency_paths(task_slug)
        ),
        repository_observations=tuple(
            RepositoryObservationDefinition(path.as_posix(), predicate.value)
            for path, predicate in validation_observation_definitions(task_slug)
        ),
        rerun_command=f"ai-sdlc validate --task {task_slug}",
    )


def validation_provenance_from_snapshot(
    snapshot: ValidationInspectionSnapshot,
    *,
    output_sha256: str,
) -> GeneratedArtifactProvenance:
    """Project exact provenance from one captured validation snapshot."""

    if not isinstance(snapshot, ValidationInspectionSnapshot):
        raise ValidationInspectionError(
            "snapshot must be a ValidationInspectionSnapshot"
        )
    uninspectable_observation = next(
        (
            item.path.as_posix()
            for item in snapshot.repository_observations
            if not item.inspectable
        ),
        None,
    )
    if uninspectable_observation is not None:
        raise ValidationInspectionError(
            "validation provenance requires a deterministic repository "
            f"observation: {uninspectable_observation}"
        )
    dependencies = tuple(
        ProvenanceDependency(
            item.path.as_posix(),
            item.state.value,
            item.sha256,
        )
        for item in snapshot.dependencies
    )
    observations = tuple(
        ProvenanceObservation(
            item.path.as_posix(),
            item.predicate.value,
            item.result,
        )
        for item in snapshot.repository_observations
    )
    try:
        return build_provenance_record(
            validation_provenance_definition_for_task(snapshot.task_slug),
            output_sha256=output_sha256,
            dependencies=dependencies,
            repository_observations=observations,
        )
    except LineageValidationError as exc:
        raise ValidationInspectionError(str(exc)) from None


def _capture_dependency(
    root: Path, path: PurePosixPath
) -> CapturedValidationDependency:
    try:
        target = resolve_managed_output_under_root(root, path)
    except (OSError, RuntimeError, PathSafetyError):
        return CapturedValidationDependency(
            path, ValidationDependencyState.UNREADABLE, None, None, None
        )
    try:
        if not target.exists():
            return CapturedValidationDependency(
                path, ValidationDependencyState.MISSING, None, None, None
            )
        if not target.is_file():
            return CapturedValidationDependency(
                path, ValidationDependencyState.NOT_REGULAR, None, None, None
            )
        raw_bytes = target.read_bytes()
    except OSError:
        return CapturedValidationDependency(
            path, ValidationDependencyState.UNREADABLE, None, None, None
        )
    try:
        decoded_text = raw_bytes.decode("utf-8")
    except UnicodeError:
        return CapturedValidationDependency(
            path, ValidationDependencyState.UNREADABLE, None, None, None
        )
    return CapturedValidationDependency(
        path,
        ValidationDependencyState.PRESENT,
        raw_bytes,
        decoded_text,
        sha256_bytes(raw_bytes),
    )


def _capture_observation(
    root: Path,
    path: PurePosixPath,
    predicate: ValidationObservationPredicate,
) -> CapturedValidationObservation:
    inspectable = True
    try:
        target = resolve_managed_output_under_root(root, path)
        if predicate is ValidationObservationPredicate.IS_FILE:
            result = target.is_file()
        else:
            result = target.is_dir()
    except (OSError, RuntimeError, PathSafetyError):
        result = False
        inspectable = False
    return CapturedValidationObservation(path, predicate, result, inspectable)


def capture_validation_snapshot(
    repository_root: Path, task_slug: str
) -> ValidationInspectionSnapshot:
    """Capture the closed validation input surface without writing anything."""

    if not isinstance(repository_root, Path):
        raise ValidationInspectionError("repository_root must be a pathlib.Path")
    _validate_task_slug(task_slug)
    try:
        root = repository_root.resolve()
        if not root.is_dir():
            raise ValidationInspectionError(
                "repository_root must be an existing directory"
            )
    except ValidationInspectionError:
        raise
    except (OSError, RuntimeError):
        raise ValidationInspectionError(
            "repository_root could not be resolved"
        ) from None

    dependencies = tuple(
        _capture_dependency(root, path)
        for path in validation_dependency_paths(task_slug)
    )
    observations = tuple(
        _capture_observation(root, path, predicate)
        for path, predicate in validation_observation_definitions(task_slug)
    )
    return ValidationInspectionSnapshot(task_slug, dependencies, observations)


def resolve_validation_currentness(
    repository_root: Path,
    task_slug: str,
) -> ValidationCurrentnessResult:
    """Resolve persisted validation-report currentness without writing state."""

    root = _validate_currentness_repository_root(repository_root)
    _validate_task_slug(task_slug)
    expected = validation_provenance_definition_for_task(task_slug)
    report_path = PurePosixPath(expected.output_path)
    path_text = report_path.as_posix()

    try:
        report_target = resolve_managed_output_under_root(root, report_path)
    except (OSError, RuntimeError, PathSafetyError):
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_report_path_unsafe",
            "blocker",
            path_text,
            f"Validation report path is unsafe: {path_text}.",
        )

    try:
        if not report_target.exists():
            return _currentness_result(
                task_slug,
                ValidationCurrentnessState.MISSING,
                "validation_report_missing",
                "warning",
                path_text,
                f"Validation report is missing; rerun `{expected.rerun_command}`.",
            )
        if not report_target.is_file():
            return _currentness_result(
                task_slug,
                ValidationCurrentnessState.INVALID,
                "validation_report_not_regular",
                "blocker",
                path_text,
                f"Validation report is not a regular file: {path_text}.",
            )
        report_bytes = report_target.read_bytes()
    except OSError:
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_report_unreadable",
            "blocker",
            path_text,
            f"Validation report cannot be read safely: {path_text}.",
        )
    report_sha256 = sha256_bytes(report_bytes)

    manifest_path_text = ".harness/manifest.json"
    try:
        manifest_path = resolve_under_root(root, manifest_path_text)
        manifest = load_manifest_model(manifest_path)
    except (OSError, RuntimeError, PathSafetyError, ManifestValidationError):
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_manifest_invalid",
            "blocker",
            manifest_path_text,
            "The main Harness manifest is missing, unreadable, or invalid.",
        )

    managed = next(
        (record for record in manifest.managed_files if record.path == path_text),
        None,
    )
    if managed is None:
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_report_unmanaged",
            "blocker",
            path_text,
            f"Existing validation report is not manifest-managed: {path_text}.",
        )
    if (
        not managed.protected
        or managed.hash_algorithm != "sha256"
        or managed.sha256 is None
    ):
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_report_management_invalid",
            "blocker",
            path_text,
            f"Validation report managed-file record is invalid: {path_text}.",
        )
    if managed.sha256 != report_sha256:
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_report_hash_drift",
            "blocker",
            path_text,
            f"Validation report bytes do not match the trusted hash: {path_text}.",
        )

    if isinstance(manifest, ManifestV1):
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.UNKNOWN,
            "legacy_validation_manifest",
            "warning",
            manifest_path_text,
            (
                "The legacy manifest cannot establish validation provenance; "
                f"rerun `{expected.rerun_command}`."
            ),
        )

    assert isinstance(manifest, ManifestV2)
    try:
        validate_manifest_provenance(manifest)
    except LineageValidationError:
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_provenance_invalid",
            "blocker",
            manifest_path_text,
            "The main Harness manifest contains invalid closed provenance.",
        )

    record = next(
        (
            item
            for item in manifest.generated_artifact_provenance
            if item.output_path == path_text
        ),
        None,
    )
    if record is None:
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.UNKNOWN,
            "validation_provenance_missing",
            "warning",
            path_text,
            (
                "Validation report has no workflow-currentness provenance; "
                f"rerun `{expected.rerun_command}`."
            ),
        )

    try:
        snapshot = capture_validation_snapshot(root, task_slug)
        inspected = _inspected_validation_snapshot(
            snapshot,
            output_sha256=report_sha256,
        )
        freshness = decide_freshness(record, inspected, expected)
    except (ValidationInspectionError, LineageValidationError):
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.INVALID,
            "validation_currentness_contract_invalid",
            "blocker",
            manifest_path_text,
            "Validation currentness inputs violate the closed contract.",
        )

    if freshness is FreshnessState.FRESH:
        return ValidationCurrentnessResult(
            task_slug,
            ValidationCurrentnessState.CURRENT,
            (),
        )
    if freshness is FreshnessState.STALE:
        return _currentness_result(
            task_slug,
            ValidationCurrentnessState.STALE,
            "validation_report_stale",
            "warning",
            path_text,
            (
                "Validation report no longer matches current validation inputs; "
                f"rerun `{expected.rerun_command}`."
            ),
        )

    findings: list[ValidationCurrentnessFinding] = []
    for dependency in snapshot.dependencies:
        if dependency.state is ValidationDependencyState.UNREADABLE:
            dependency_path = dependency.path.as_posix()
            findings.append(
                ValidationCurrentnessFinding(
                    "validation_dependency_uninspectable",
                    "blocker",
                    dependency_path,
                    (
                        "Validation dependency cannot be inspected "
                        f"deterministically: {dependency_path}."
                    ),
                )
            )
    for observation in snapshot.repository_observations:
        if not observation.inspectable:
            observation_path = observation.path.as_posix()
            findings.append(
                ValidationCurrentnessFinding(
                    "validation_observation_uninspectable",
                    "blocker",
                    observation_path,
                    (
                        "Validation repository observation cannot be inspected "
                        f"deterministically: {observation_path}."
                    ),
                )
            )
    if not findings:
        findings.append(
            ValidationCurrentnessFinding(
                "validation_currentness_unknown",
                "blocker",
                path_text,
                "Validation report currentness cannot be established.",
            )
        )
    return ValidationCurrentnessResult(
        task_slug,
        ValidationCurrentnessState.UNKNOWN,
        tuple(findings),
    )


def _inspected_validation_snapshot(
    snapshot: ValidationInspectionSnapshot,
    *,
    output_sha256: str,
) -> InspectedProvenanceState:
    dependencies = tuple(
        sorted(
            (
                InspectedDependencyState(
                    item.path.as_posix(),
                    item.state.value,
                    item.sha256,
                )
                for item in snapshot.dependencies
            ),
            key=lambda item: item.dependency_path,
        )
    )
    observations = tuple(
        sorted(
            (
                InspectedObservationState(
                    item.path.as_posix(),
                    item.predicate.value,
                    "observed" if item.inspectable else "unavailable",
                    item.result if item.inspectable else None,
                )
                for item in snapshot.repository_observations
            ),
            key=lambda item: (item.observation_path, item.predicate),
        )
    )
    return InspectedProvenanceState(
        output_path=validation_provenance_definition_for_task(
            snapshot.task_slug
        ).output_path,
        output_state="present",
        output_sha256=output_sha256,
        dependencies=dependencies,
        repository_observations=observations,
    )


def _validate_currentness_repository_root(repository_root: Path) -> Path:
    if not isinstance(repository_root, Path):
        raise ValidationInspectionError("repository_root must be a pathlib.Path")
    try:
        root = repository_root.resolve()
        is_directory = root.is_dir()
    except (OSError, RuntimeError):
        raise ValidationInspectionError(
            "repository_root could not be resolved"
        ) from None
    if not is_directory:
        raise ValidationInspectionError(
            "repository_root must be an existing directory"
        )
    return root


def _currentness_result(
    task_slug: str,
    state: ValidationCurrentnessState,
    code: str,
    level: str,
    path: str | None,
    message: str,
) -> ValidationCurrentnessResult:
    return ValidationCurrentnessResult(
        task_slug,
        state,
        (ValidationCurrentnessFinding(code, level, path, message),),
    )


def _currentness_finding_sort_key(
    finding: ValidationCurrentnessFinding,
) -> tuple[int, str, str, str]:
    return (
        0 if finding.level == "blocker" else 1,
        finding.code,
        finding.path or "",
        finding.message,
    )


def _line_is_placeholder(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("#")
        or TODO_RE.search(stripped) is not None
        or stripped.startswith(("- [ ]", "* [ ]"))
        or stripped in {"-", "*", "[]"}
    )


def _readiness_state(text: str) -> str:
    if not text.strip():
        return "empty"
    if not any(not _line_is_placeholder(line) for line in text.splitlines()):
        return "TODO-only"
    return "substantive"


def _artifact_status(dependency: CapturedValidationDependency) -> ArtifactStatus:
    task_parts = dependency.path.parts
    filename = dependency.path.name
    if len(task_parts) >= 2 and task_parts[-2:] == (
        TASK_GENERATED_DIRNAME,
        AGENT_WORKSET_FILENAME,
    ):
        filename = f"{TASK_GENERATED_DIRNAME}/{AGENT_WORKSET_FILENAME}"
    if dependency.state is ValidationDependencyState.PRESENT:
        assert dependency.decoded_text is not None
        state = _readiness_state(dependency.decoded_text)
        return ArtifactStatus(
            filename,
            dependency.path,
            True,
            True,
            state,
            state,
            dependency.decoded_text,
        )
    if dependency.state is ValidationDependencyState.MISSING:
        return ArtifactStatus(
            filename, dependency.path, False, False, "missing", "missing", ""
        )
    if dependency.state is ValidationDependencyState.NOT_REGULAR:
        return ArtifactStatus(
            filename,
            dependency.path,
            True,
            False,
            "not_regular",
            "not a regular file",
            "",
        )
    return ArtifactStatus(
        filename,
        dependency.path,
        True,
        False,
        "unreadable",
        "unreadable",
        "",
    )


def _verification_command_status(verification: ArtifactStatus) -> str:
    if not verification.readable:
        return "missing"
    return "present" if TEST_COMMAND_RE.search(verification.text) else "missing"


def _test_result_status(
    evidence: ArtifactStatus, verification: ArtifactStatus
) -> str:
    text = "\n".join(
        item.text for item in (evidence, verification) if item.readable
    )
    return "present" if TEST_RESULT_RE.search(text) else "missing"


def _todo_sources(task_artifacts: tuple[ArtifactStatus, ...]) -> tuple[str, ...]:
    return tuple(
        item.filename
        for item in task_artifacts
        if item.readable and TODO_RE.search(item.text)
    )


def _report_findings(
    generated_reports: tuple[ArtifactStatus, ...]
) -> tuple[ReportFinding, ...]:
    findings: list[ReportFinding] = []
    for item in generated_reports:
        if item.filename not in _PRIOR_FINDING_REPORTS or not item.readable:
            continue
        in_findings = False
        for line in item.text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip().casefold()
                if in_findings:
                    break
                in_findings = heading == "findings"
                continue
            if not in_findings:
                continue
            match = FINDING_LINE_RE.match(line)
            if not match:
                continue
            level = match.group(1).lower()
            message = match.group(2).strip()
            findings.append(ReportFinding(item.filename, level, message))
            if len(findings) >= 30:
                return tuple(findings)
    return tuple(findings)


def _requirements_status(
    dependency: CapturedValidationDependency, task_slug: str
) -> ValidationRequirementsStatus:
    if dependency.state is ValidationDependencyState.MISSING:
        result = RequirementsReadResult(
            False, False, False, "missing", 0, 0, [], [], []
        )
    elif dependency.state is not ValidationDependencyState.PRESENT:
        message = (
            "not a regular file"
            if dependency.state is ValidationDependencyState.NOT_REGULAR
            else "unreadable"
        )
        result = RequirementsReadResult(
            True,
            False,
            False,
            message,
            0,
            0,
            [],
            [],
            [f"requirements.yaml is {message}."],
        )
    else:
        assert dependency.decoded_text is not None
        result = parse_requirements_text(
            dependency.decoded_text, expected_slug=task_slug
        )
    return ValidationRequirementsStatus(
        present=result.present,
        readable=result.readable,
        schema_valid=result.schema_valid,
        message=result.message,
        requirement_count=result.requirement_count,
        finding_count=result.finding_count,
        problems=tuple(result.problems),
    )


def _observation_result(
    observations: tuple[CapturedValidationObservation, ...],
    path: str,
    predicate: ValidationObservationPredicate,
) -> bool:
    for observation in observations:
        if observation.path.as_posix() == path and observation.predicate is predicate:
            return observation.result
    raise ValidationInspectionError(
        f"closed validation observation is missing: {path} {predicate.value}"
    )


def _workflow_checks(
    task_artifacts: tuple[ArtifactStatus, ...],
    generated_reports: tuple[ArtifactStatus, ...],
) -> tuple[tuple[str, str], ...]:
    by_name = {item.filename: item for item in generated_reports}

    def generated(filename: str) -> str:
        item = by_name[filename]
        return "yes" if item.present and item.readable else "no"

    return (
        (
            "task was started",
            "yes" if any(item.present for item in task_artifacts) else "no",
        ),
        ("preflight was generated", generated(PREFLIGHT_FILENAME)),
        ("spec was generated", generated(SPEC_FILENAME)),
        ("requirements.yaml was generated", generated(REQUIREMENTS_FILENAME)),
        (
            "test-contract review was generated",
            generated(TEST_CONTRACT_REVIEW_FILENAME),
        ),
        (
            "workset was generated",
            generated(f"{TASK_GENERATED_DIRNAME}/{AGENT_WORKSET_FILENAME}"),
        ),
        ("evidence report was generated", generated(EVIDENCE_REPORT_FILENAME)),
    )


def _key_workflow_outputs(
    generated_reports: tuple[ArtifactStatus, ...]
) -> tuple[ArtifactStatus, ...]:
    return tuple(
        item
        for item in generated_reports
        if item.filename not in {SPEC_FILENAME, REQUIREMENTS_FILENAME}
    )


def _review_readiness(
    findings: tuple[Finding, ...],
    generated_reports: tuple[ArtifactStatus, ...],
) -> tuple[str, str]:
    if any(finding.level == "blocker" for finding in findings):
        return "No", "blocker findings must be resolved before human review."
    missing_outputs = tuple(
        item.filename
        for item in _key_workflow_outputs(generated_reports)
        if not (item.present and item.readable)
    )
    if missing_outputs:
        return (
            "No",
            "key workflow outputs are missing or unreadable: "
            f"{', '.join(missing_outputs)}.",
        )
    if any(finding.level == "warning" for finding in findings):
        return (
            "Caution",
            "no blockers were found, but warning findings remain for human review.",
        )
    return "Yes", "recorded workflow signals show no blocker or warning findings."


def _recommended_actions(findings: tuple[Finding, ...]) -> tuple[str, ...]:
    messages = tuple(
        finding.message
        for finding in findings
        if finding.level in {"blocker", "warning"}
    )
    actions: list[str] = []
    if any("acceptance" in message for message in messages):
        actions.append(
            "Add substantive acceptance criteria for the behavior a human should review."
        )
    if any("test-contract" in message for message in messages):
        actions.append(
            "Add substantive test-contract content for expected characterization, regression, edge, and desired-behavior checks."
        )
    if any("evidence" in message for message in messages):
        actions.append(
            "Record implementation evidence, verification results, and unresolved risks in evidence.md."
        )
    if any(
        "verification" in message or "test result" in message
        for message in messages
    ):
        actions.append(
            "Record verification commands and results in verification.md or evidence.md."
        )
    if any("TODO" in message for message in messages):
        actions.append(
            "Resolve or explicitly carry forward unresolved TODOs before relying on these findings."
        )
    if not actions:
        actions.append(
            "Review the validation findings and source task artifacts before accepting the work."
        )
    return tuple(actions[:5])


def inspect_validation_snapshot(
    snapshot: ValidationInspectionSnapshot,
) -> ValidationInspectionResult:
    """Derive semantic validation solely from one immutable snapshot."""

    if not isinstance(snapshot, ValidationInspectionSnapshot):
        raise ValidationInspectionError(
            "snapshot must be a ValidationInspectionSnapshot"
        )

    artifact_statuses = tuple(
        _artifact_status(dependency) for dependency in snapshot.dependencies
    )
    task_artifacts = artifact_statuses[: len(TASK_ARTIFACT_FILENAMES)]
    generated_reports = artifact_statuses[len(TASK_ARTIFACT_FILENAMES) :]
    by_task_name = {item.filename: item for item in task_artifacts}
    evidence = by_task_name["evidence.md"]
    verification = by_task_name["verification.md"]
    verification_command = _verification_command_status(verification)
    test_result_evidence = _test_result_status(evidence, verification)
    todo_sources = _todo_sources(task_artifacts)
    report_findings = _report_findings(generated_reports)
    requirements_dependency = snapshot.dependencies[
        len(TASK_ARTIFACT_FILENAMES) + _GENERATED_DEPENDENCY_NAMES.index(
            REQUIREMENTS_FILENAME
        )
    ]
    structured_requirements = _requirements_status(
        requirements_dependency, snapshot.task_slug
    )

    findings: list[Finding] = []
    for item in task_artifacts:
        if not item.present:
            findings.append(Finding("blocker", f"{item.filename} is missing."))
        elif not item.readable:
            findings.append(Finding("blocker", f"{item.filename} is unreadable."))
        elif item.state in {"empty", "TODO-only"}:
            findings.append(Finding("blocker", f"{item.filename} is {item.state}."))

    acceptance = by_task_name["acceptance.md"]
    test_contract = by_task_name["test-contract.md"]
    if not acceptance.substantive and not test_contract.substantive:
        findings.append(
            Finding(
                "blocker",
                "no usable acceptance criteria and no usable test-contract content are available.",
            )
        )
    if not evidence.substantive and not verification.substantive:
        findings.append(
            Finding(
                "blocker",
                "no usable evidence and no usable verification content are available.",
            )
        )

    for item in generated_reports:
        if not item.present:
            findings.append(Finding("warning", f"{item.filename} is missing."))
        elif not item.readable:
            findings.append(Finding("warning", f"{item.filename} is unreadable."))

    if todo_sources:
        findings.append(
            Finding(
                "warning",
                f"unresolved TODOs remain in: {', '.join(todo_sources)}.",
            )
        )
    if verification_command == "missing":
        findings.append(Finding("warning", "no verification command recorded."))
    if test_result_evidence == "missing":
        findings.append(Finding("warning", "no test result evidence recorded."))
    if not _observation_result(
        snapshot.repository_observations,
        ".github/workflows",
        ValidationObservationPredicate.IS_DIR,
    ):
        findings.append(Finding("warning", "no CI detected."))
    tests_directory = _observation_result(
        snapshot.repository_observations,
        "tests",
        ValidationObservationPredicate.IS_DIR,
    )
    pytest_config = _observation_result(
        snapshot.repository_observations,
        "pytest.ini",
        ValidationObservationPredicate.IS_FILE,
    )
    documented_test_frameworks = detect_documented_test_frameworks(
        test_contract.text,
        verification.text,
    )
    if not (tests_directory or pytest_config or documented_test_frameworks):
        findings.append(
            Finding("warning", "no recognized test-framework signal detected.")
        )
    if (
        structured_requirements.present
        and structured_requirements.readable
        and not structured_requirements.schema_valid
    ):
        findings.extend(
            Finding("blocker", f"requirements.yaml schema invalid: {problem}")
            for problem in structured_requirements.problems
        )
    findings.extend(Finding("info", message) for message in _FIXED_INFO_MESSAGES)
    canonical_findings = tuple(findings)
    blocker_count = sum(
        finding.level == "blocker" for finding in canonical_findings
    )
    warning_count = sum(
        finding.level == "warning" for finding in canonical_findings
    )
    info_count = sum(finding.level == "info" for finding in canonical_findings)
    clean = blocker_count == 0 and warning_count == 0
    review_readiness, review_reason = _review_readiness(
        canonical_findings, generated_reports
    )
    return ValidationInspectionResult(
        task_slug=snapshot.task_slug,
        artifact_statuses=artifact_statuses,
        task_artifacts=task_artifacts,
        generated_reports=generated_reports,
        findings=canonical_findings,
        blocker_count=blocker_count,
        warning_count=warning_count,
        info_count=info_count,
        clean=clean,
        verification_command=verification_command,
        test_result_evidence=test_result_evidence,
        todo_sources=todo_sources,
        report_findings=report_findings,
        workflow_checks=_workflow_checks(task_artifacts, generated_reports),
        structured_requirements=structured_requirements,
        review_readiness=review_readiness,
        review_readiness_reason=review_reason,
        recommended_actions=_recommended_actions(canonical_findings),
    )
