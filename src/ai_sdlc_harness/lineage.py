"""Closed provenance definitions, inspection, and freshness resolution.

This module owns the four selector-facing Lineage outputs, the shared closed
provenance primitives, and their bounded filesystem inspection. It loads the
main manifest read-only for repository-wide resolution but never modifies it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Iterable, Mapping

from .constants import (
    PREFLIGHT_FILENAME,
    REQUIREMENTS_FILENAME,
    SPEC_FILENAME,
    TASK_ARTIFACT_FILENAMES,
    TASK_SLUG_PATTERN,
    TEST_CONTRACT_REVIEW_FILENAME,
    VALIDATION_REPORT_FILENAME,
)
from .context import ContextFinding, FindingLevel, FreshnessState
from .files import (
    PathSafetyError,
    resolve_managed_output_under_root,
    resolve_under_root,
    sha256_bytes,
    sha256_file,
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


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INSPECTED_FILE_STATES = frozenset(
    {"present", "missing", "not_regular", "unreadable", "unsafe"}
)
_INSPECTED_OBSERVATION_STATES = frozenset(
    {"observed", "unavailable", "unsafe"}
)


class LineageValidationError(ValueError):
    """Raised when a record does not match the closed Lineage contract."""


@dataclass(frozen=True)
class LineageResolutionResult:
    """Immutable selector-facing freshness mapping and canonical findings."""

    derived_freshness: Mapping[str, FreshnessState]
    findings: tuple[ContextFinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.derived_freshness, Mapping):
            raise LineageValidationError(
                "derived_freshness must be a mapping"
            )
        expected_keys = tuple(
            definition.registry_id for definition in lineage_definitions()
        )
        supplied = dict(self.derived_freshness)
        if set(supplied) != set(expected_keys):
            raise LineageValidationError(
                "derived_freshness must contain exactly the four closed "
                "Lineage registry IDs"
            )
        allowed = {
            FreshnessState.FRESH,
            FreshnessState.STALE,
            FreshnessState.MISSING,
            FreshnessState.UNKNOWN,
        }
        canonical_mapping: dict[str, FreshnessState] = {}
        for key in expected_keys:
            state = supplied[key]
            if not isinstance(state, FreshnessState) or state not in allowed:
                raise LineageValidationError(
                    f"derived_freshness.{key} must be fresh, stale, missing, "
                    "or unknown"
                )
            canonical_mapping[key] = state

        if not isinstance(self.findings, tuple):
            raise LineageValidationError("findings must be a tuple")
        for finding in self.findings:
            if not isinstance(finding, ContextFinding):
                raise LineageValidationError(
                    "findings items must use ContextFinding"
                )
            if not isinstance(finding.level, FindingLevel):
                raise LineageValidationError(
                    "finding level must use FindingLevel"
                )
            if not isinstance(finding.code, str) or not finding.code:
                raise LineageValidationError(
                    "finding code must be a non-empty string"
                )
            if finding.path is not None and not isinstance(finding.path, str):
                raise LineageValidationError(
                    "finding path must be a string or None"
                )
            if not isinstance(finding.message, str) or not finding.message:
                raise LineageValidationError(
                    "finding message must be a non-empty string"
                )

        canonical_findings = tuple(
            sorted(set(self.findings), key=_finding_sort_key)
        )
        object.__setattr__(
            self,
            "derived_freshness",
            MappingProxyType(canonical_mapping),
        )
        object.__setattr__(self, "findings", canonical_findings)

    @property
    def blockers(self) -> tuple[ContextFinding, ...]:
        """Return only blocker findings from the canonical findings tuple."""

        return tuple(
            finding
            for finding in self.findings
            if finding.level == FindingLevel.BLOCKER
        )


@dataclass(frozen=True)
class DependencyDefinition:
    """One content-affecting dependency in a producer definition."""

    relative_path: str
    task_scoped: bool = True


@dataclass(frozen=True)
class RepositoryObservationDefinition:
    """One shallow repository predicate used by a producer."""

    relative_path: str
    predicate: str


@dataclass(frozen=True)
class ProducerOutputDefinition:
    """Closed producer contract before resolving a task slug."""

    registry_id: str
    producer_command: str
    producer_version: int
    output_filename: str
    dependencies: tuple[DependencyDefinition, ...]
    repository_observations: tuple[RepositoryObservationDefinition, ...]
    rerun_command: str


@dataclass(frozen=True)
class ResolvedProducerOutputDefinition:
    """Closed producer contract resolved to one task."""

    registry_id: str
    producer_command: str
    producer_version: int
    task_slug: str
    output_path: str
    dependencies: tuple[str, ...]
    repository_observations: tuple[RepositoryObservationDefinition, ...]
    rerun_command: str


@dataclass(frozen=True)
class CapturedDependency:
    """One dependency record bound to the exact bytes used by a producer."""

    record: ProvenanceDependency
    content: bytes | None


@dataclass(frozen=True)
class InspectedDependencyState:
    """Current read-only state of one exact dependency identity."""

    dependency_path: str
    dependency_state: str
    dependency_sha256: str | None

    def __post_init__(self) -> None:
        _validate_inspected_file_state(
            self.dependency_path,
            self.dependency_state,
            self.dependency_sha256,
            "inspected dependency",
        )


@dataclass(frozen=True)
class InspectedObservationState:
    """Current read-only result of one exact repository predicate."""

    observation_path: str
    predicate: str
    observation_state: str
    result: bool | None

    def __post_init__(self) -> None:
        _validate_repository_path(
            self.observation_path,
            "inspected observation path",
        )
        if (
            not isinstance(self.predicate, str)
            or self.predicate not in {"exists", "is_file", "is_dir"}
        ):
            raise LineageValidationError(
                "inspected observation predicate must be exists, is_file, or is_dir"
            )
        if (
            not isinstance(self.observation_state, str)
            or self.observation_state not in _INSPECTED_OBSERVATION_STATES
        ):
            raise LineageValidationError(
                "inspected observation state must be observed, unavailable, or unsafe"
            )
        if self.observation_state == "observed":
            if not isinstance(self.result, bool):
                raise LineageValidationError(
                    "observed repository observation requires a Boolean result"
                )
        elif self.result is not None:
            raise LineageValidationError(
                "unavailable or unsafe repository observation must not have a result"
            )


@dataclass(frozen=True)
class InspectedProvenanceState:
    """Current read-only state needed for one pure freshness decision."""

    output_path: str
    output_state: str
    output_sha256: str | None
    dependencies: tuple[InspectedDependencyState, ...]
    repository_observations: tuple[InspectedObservationState, ...]

    def __post_init__(self) -> None:
        _validate_inspected_file_state(
            self.output_path,
            self.output_state,
            self.output_sha256,
            "inspected output",
        )
        if not isinstance(self.dependencies, tuple):
            raise LineageValidationError(
                "inspected dependencies must be a tuple"
            )
        if not all(
            isinstance(item, InspectedDependencyState)
            for item in self.dependencies
        ):
            raise LineageValidationError(
                "inspected dependencies must use InspectedDependencyState"
            )
        dependency_paths = tuple(
            item.dependency_path for item in self.dependencies
        )
        if len(dependency_paths) != len(set(dependency_paths)):
            raise LineageValidationError(
                "inspected dependencies contain duplicate paths"
            )
        if dependency_paths != tuple(sorted(dependency_paths)):
            raise LineageValidationError(
                "inspected dependencies are not in canonical path order"
            )

        if not isinstance(self.repository_observations, tuple):
            raise LineageValidationError(
                "inspected repository observations must be a tuple"
            )
        if not all(
            isinstance(item, InspectedObservationState)
            for item in self.repository_observations
        ):
            raise LineageValidationError(
                "inspected repository observations must use "
                "InspectedObservationState"
            )
        observation_identities = tuple(
            (item.observation_path, item.predicate)
            for item in self.repository_observations
        )
        if len(observation_identities) != len(set(observation_identities)):
            raise LineageValidationError(
                "inspected repository observations contain duplicate identities"
            )
        if observation_identities != tuple(sorted(observation_identities)):
            raise LineageValidationError(
                "inspected repository observations are not in canonical "
                "path/predicate order"
            )


_TASK_SOURCE_DEPENDENCIES = tuple(
    DependencyDefinition(filename) for filename in TASK_ARTIFACT_FILENAMES
)
_PREFLIGHT_DEPENDENCY = DependencyDefinition(PREFLIGHT_FILENAME)
_SELECTED_PACKS_DEPENDENCY = DependencyDefinition(
    ".harness/packs/selected.yaml",
    task_scoped=False,
)

_PREFLIGHT_OBSERVATIONS = (
    RepositoryObservationDefinition(".git", "exists"),
    RepositoryObservationDefinition("pyproject.toml", "is_file"),
    RepositoryObservationDefinition("package.json", "is_file"),
    RepositoryObservationDefinition("Cargo.toml", "is_file"),
    RepositoryObservationDefinition("go.mod", "is_file"),
    RepositoryObservationDefinition("pytest.ini", "is_file"),
    RepositoryObservationDefinition("AGENTS.md", "is_file"),
    RepositoryObservationDefinition("CLAUDE.md", "is_file"),
    RepositoryObservationDefinition("tests", "is_dir"),
    RepositoryObservationDefinition(".github/workflows", "is_dir"),
)

# test-contract renders only the package-manager, test-framework, and CI
# projections from detect_project_signals(). Git and agent-file signals do not
# affect its report or findings.
_TEST_CONTRACT_OBSERVATIONS = (
    RepositoryObservationDefinition("pyproject.toml", "is_file"),
    RepositoryObservationDefinition("package.json", "is_file"),
    RepositoryObservationDefinition("Cargo.toml", "is_file"),
    RepositoryObservationDefinition("go.mod", "is_file"),
    RepositoryObservationDefinition("pytest.ini", "is_file"),
    RepositoryObservationDefinition("tests", "is_dir"),
    RepositoryObservationDefinition(".github/workflows", "is_dir"),
)

_LINEAGE_DEFINITIONS = (
    ProducerOutputDefinition(
        registry_id="spec",
        producer_command="spec",
        producer_version=1,
        output_filename=SPEC_FILENAME,
        dependencies=(*_TASK_SOURCE_DEPENDENCIES, _PREFLIGHT_DEPENDENCY),
        repository_observations=(),
        rerun_command="ai-sdlc spec --task {task_slug}",
    ),
    ProducerOutputDefinition(
        registry_id="requirements_projection",
        producer_command="spec",
        producer_version=1,
        output_filename=REQUIREMENTS_FILENAME,
        dependencies=(*_TASK_SOURCE_DEPENDENCIES, _PREFLIGHT_DEPENDENCY),
        repository_observations=(),
        rerun_command="ai-sdlc spec --task {task_slug}",
    ),
    ProducerOutputDefinition(
        registry_id="preflight",
        producer_command="preflight",
        producer_version=1,
        output_filename=PREFLIGHT_FILENAME,
        dependencies=(*_TASK_SOURCE_DEPENDENCIES, _SELECTED_PACKS_DEPENDENCY),
        repository_observations=_PREFLIGHT_OBSERVATIONS,
        rerun_command="ai-sdlc preflight --task {task_slug}",
    ),
    ProducerOutputDefinition(
        registry_id="test_contract_review",
        producer_command="test-contract",
        producer_version=1,
        output_filename=TEST_CONTRACT_REVIEW_FILENAME,
        dependencies=(
            DependencyDefinition("acceptance.md"),
            DependencyDefinition("test-contract.md"),
            DependencyDefinition("verification.md"),
            DependencyDefinition("evidence.md"),
            _PREFLIGHT_DEPENDENCY,
        ),
        repository_observations=_TEST_CONTRACT_OBSERVATIONS,
        rerun_command="ai-sdlc test-contract --task {task_slug}",
    ),
)


def lineage_definitions() -> tuple[ProducerOutputDefinition, ...]:
    """Return the immutable closed definitions in context-registry order."""

    return _LINEAGE_DEFINITIONS


def lineage_definition(registry_id: str) -> ProducerOutputDefinition:
    """Return one closed definition or reject an unsupported registry ID."""

    for definition in _LINEAGE_DEFINITIONS:
        if definition.registry_id == registry_id:
            return definition
    raise LineageValidationError(
        f"unknown Lineage registry ID: {registry_id!r}"
    )


def lineage_definitions_for_task(
    task_slug: str,
) -> tuple[ResolvedProducerOutputDefinition, ...]:
    """Resolve every closed definition for one safe task slug."""

    _validate_task_slug(task_slug)
    return tuple(
        _resolve_definition(definition, task_slug)
        for definition in _LINEAGE_DEFINITIONS
    )


def lineage_definition_for_task(
    registry_id: str,
    task_slug: str,
) -> ResolvedProducerOutputDefinition:
    """Resolve one closed definition for one safe task slug."""

    _validate_task_slug(task_slug)
    return _resolve_definition(lineage_definition(registry_id), task_slug)


def validate_provenance_record(
    record: GeneratedArtifactProvenance,
    expected: ResolvedProducerOutputDefinition,
) -> GeneratedArtifactProvenance:
    """Require an exact, canonically ordered match to a closed definition.

    Structural field and value validation remains owned by ``manifest.py``.
    This function validates only the supported producer/output relationship and
    the complete dependency and observation identities.
    """

    _validate_provenance_identity(
        record,
        expected,
        require_current_producer_version=True,
    )
    return record


def validate_manifest_lineage_provenance(
    manifest: ManifestV2,
) -> ManifestV2:
    """Validate selector-facing records and ignore validation workflow records.

    Producer-version mismatch is intentionally allowed here. A prior positive
    version is valid stored provenance and is handled as stale by freshness
    resolution. Validation-report provenance is deliberately isolated from the
    selector resolver and is validated by :func:`validate_manifest_provenance`.
    """

    if not isinstance(manifest, ManifestV2):
        raise LineageValidationError(
            "Lineage semantic validation requires ManifestV2"
        )
    for record in manifest.generated_artifact_provenance:
        if not isinstance(record, GeneratedArtifactProvenance):
            raise LineageValidationError(
                "provenance record must use GeneratedArtifactProvenance"
            )
        if _is_validation_report_output_path(record.output_path):
            continue
        expected = _lineage_definition_for_record(record)
        if expected is None:
            raise LineageValidationError(
                "provenance output path is not owned by the closed Lineage "
                f"registry: {record.output_path}"
            )
        _validate_provenance_identity(
            record,
            expected,
            require_current_producer_version=False,
        )
    return manifest


def validate_manifest_provenance(manifest: ManifestV2) -> ManifestV2:
    """Validate every record against the closed manifest-wide definitions.

    The accepted classes are exactly the four selector-facing Lineage outputs
    and the validation-report workflow output. Positive historical producer
    versions remain structurally valid so their resolvers can classify them as
    stale rather than corrupt.
    """

    if not isinstance(manifest, ManifestV2):
        raise LineageValidationError(
            "manifest-wide provenance validation requires ManifestV2"
        )
    for record in manifest.generated_artifact_provenance:
        if not isinstance(record, GeneratedArtifactProvenance):
            raise LineageValidationError(
                "provenance record must use GeneratedArtifactProvenance"
            )
        expected = _lineage_definition_for_record(record)
        if expected is None:
            from .validation import validation_provenance_definition_for_task

            validation_expected = validation_provenance_definition_for_task(
                record.task_slug
            )
            if record.output_path == validation_expected.output_path:
                expected = validation_expected
        if expected is None:
            raise LineageValidationError(
                "provenance output path is not owned by a closed producer "
                f"definition: {record.output_path}"
            )
        _validate_provenance_identity(
            record,
            expected,
            require_current_producer_version=False,
        )
    return manifest


def inspect_provenance_state(
    repository_root: Path,
    expected: ResolvedProducerOutputDefinition,
) -> InspectedProvenanceState:
    """Inspect exactly one closed output and its current direct inputs."""

    root = _validate_repository_root(repository_root)
    if not isinstance(expected, ResolvedProducerOutputDefinition):
        raise LineageValidationError(
            "expected definition must use ResolvedProducerOutputDefinition"
        )
    _require_closed_resolved_definition(expected)

    output_state, output_sha256 = _inspect_output(
        root,
        expected.output_path,
    )
    dependencies = tuple(
        _inspect_dependency(root, dependency_path)
        for dependency_path in sorted(expected.dependencies)
    )
    observations = tuple(
        _inspect_observation(root, observation)
        for observation in sorted(
            expected.repository_observations,
            key=lambda item: (item.relative_path, item.predicate),
        )
    )
    return InspectedProvenanceState(
        output_path=expected.output_path,
        output_state=output_state,
        output_sha256=output_sha256,
        dependencies=dependencies,
        repository_observations=observations,
    )


def resolve_derived_freshness(
    repository_root: Path,
    task_slug: str,
) -> LineageResolutionResult:
    """Resolve all four derived artifacts without modifying repository state."""

    root = _validate_repository_root(repository_root)
    _validate_task_slug(task_slug)
    definitions = lineage_definitions_for_task(task_slug)
    inspected_states = tuple(
        inspect_provenance_state(root, definition)
        for definition in definitions
    )
    findings = list(_current_state_findings(inspected_states))

    harness_root = root / ".harness"
    try:
        config_path = resolve_under_root(root, ".harness/config.yaml")
        initialized = harness_root.is_dir() and config_path.is_file()
    except (OSError, RuntimeError, PathSafetyError):
        initialized = False
    if not initialized:
        findings.append(
            ContextFinding(
                code="lineage_repository_uninitialized",
                level=FindingLevel.BLOCKER,
                path=".harness",
                message=(
                    "AI SDLC Harness is not initialized; run `ai-sdlc init` "
                    "from the project root."
                ),
            )
        )
        return LineageResolutionResult(
            _fallback_freshness(definitions, inspected_states),
            tuple(findings),
        )

    try:
        manifest_path = resolve_under_root(
            root,
            ".harness/manifest.json",
        )
    except (OSError, RuntimeError, PathSafetyError):
        findings.append(_manifest_unreadable_finding())
        return LineageResolutionResult(
            _fallback_freshness(definitions, inspected_states),
            tuple(findings),
        )

    try:
        manifest_exists = manifest_path.exists()
    except OSError:
        manifest_exists = True
    if not manifest_exists:
        findings.append(
            ContextFinding(
                code="lineage_manifest_missing",
                level=FindingLevel.BLOCKER,
                path=".harness/manifest.json",
                message=(
                    "Initialized repository is missing the main Harness "
                    "manifest: .harness/manifest.json."
                ),
            )
        )
        return LineageResolutionResult(
            _fallback_freshness(definitions, inspected_states),
            tuple(findings),
        )

    try:
        manifest = load_manifest_model(manifest_path)
    except ManifestValidationError:
        findings.append(
            ContextFinding(
                code="lineage_manifest_invalid",
                level=FindingLevel.BLOCKER,
                path=".harness/manifest.json",
                message=(
                    "The main Harness manifest is structurally invalid; "
                    "Lineage freshness cannot be trusted."
                ),
            )
        )
        return LineageResolutionResult(
            _fallback_freshness(definitions, inspected_states),
            tuple(findings),
        )
    except OSError:
        findings.append(_manifest_unreadable_finding())
        return LineageResolutionResult(
            _fallback_freshness(definitions, inspected_states),
            tuple(findings),
        )

    if isinstance(manifest, ManifestV1):
        findings.append(
            ContextFinding(
                code="legacy_lineage_manifest",
                level=FindingLevel.WARNING,
                path=".harness/manifest.json",
                message=(
                    "The versionless legacy manifest has no artifact "
                    "provenance; present derived artifacts remain unknown "
                    "until their producers rerun."
                ),
            )
        )
        return LineageResolutionResult(
            _fallback_freshness(definitions, inspected_states),
            tuple(findings),
        )

    try:
        validate_manifest_lineage_provenance(manifest)
    except LineageValidationError:
        findings.append(
            ContextFinding(
                code="lineage_provenance_invalid",
                level=FindingLevel.BLOCKER,
                path=".harness/manifest.json",
                message=(
                    "The main Harness manifest contains provenance that does "
                    "not match the closed Lineage contract."
                ),
            )
        )
        return LineageResolutionResult(
            _fallback_freshness(definitions, inspected_states),
            tuple(findings),
        )

    provenance_by_output = {
        record.output_path: record
        for record in manifest.generated_artifact_provenance
    }
    freshness: dict[str, FreshnessState] = {}
    for definition, inspected in zip(
        definitions,
        inspected_states,
        strict=True,
    ):
        record = provenance_by_output.get(definition.output_path)
        freshness[definition.registry_id] = decide_freshness(
            record,
            inspected,
            definition,
        )
        if record is None and inspected.output_state == "present":
            findings.append(
                ContextFinding(
                    code="missing_lineage_provenance",
                    level=FindingLevel.WARNING,
                    path=definition.output_path,
                    message=(
                        "Present derived output has no Lineage provenance and "
                        f"remains unknown: {definition.output_path}; rerun "
                        f"`{definition.rerun_command}`."
                    ),
                )
            )
    return LineageResolutionResult(freshness, tuple(findings))


def decide_freshness(
    record: GeneratedArtifactProvenance | None,
    inspected: InspectedProvenanceState,
    expected: ResolvedProducerOutputDefinition,
) -> FreshnessState:
    """Return one deterministic freshness state without filesystem access."""

    if not isinstance(expected, ResolvedProducerOutputDefinition):
        raise LineageValidationError(
            "expected definition must use ResolvedProducerOutputDefinition"
        )
    _require_closed_resolved_definition(expected)
    if not isinstance(inspected, InspectedProvenanceState):
        raise LineageValidationError(
            "inspected state must use InspectedProvenanceState"
        )
    if record is not None and not isinstance(
        record,
        GeneratedArtifactProvenance,
    ):
        raise LineageValidationError(
            "provenance record must use GeneratedArtifactProvenance or be None"
        )
    _validate_inspected_identity(inspected, expected)

    if inspected.output_state == "missing":
        return FreshnessState.MISSING
    if _has_unsafe_state(inspected):
        return FreshnessState.UNKNOWN
    if inspected.output_state in {"unreadable", "not_regular"}:
        return FreshnessState.UNKNOWN
    if record is None:
        return FreshnessState.UNKNOWN

    _validate_provenance_identity(
        record,
        expected,
        require_current_producer_version=False,
    )
    if record.producer_version != expected.producer_version:
        return FreshnessState.STALE

    definite_mismatch = record.output_sha256 != inspected.output_sha256
    indeterminate = False

    for recorded, current in zip(
        record.dependencies,
        inspected.dependencies,
        strict=True,
    ):
        if current.dependency_state == "unreadable":
            indeterminate = True
            continue
        if recorded.dependency_state != current.dependency_state:
            definite_mismatch = True
            continue
        if (
            recorded.dependency_state == "present"
            and recorded.dependency_sha256 != current.dependency_sha256
        ):
            definite_mismatch = True
        elif recorded.dependency_state == "unreadable":
            indeterminate = True

    for recorded, current in zip(
        record.repository_observations,
        inspected.repository_observations,
        strict=True,
    ):
        if current.observation_state == "unavailable":
            indeterminate = True
        elif recorded.result != current.result:
            definite_mismatch = True

    if definite_mismatch:
        return FreshnessState.STALE
    if indeterminate:
        return FreshnessState.UNKNOWN
    return FreshnessState.FRESH


def build_provenance_record(
    expected: ResolvedProducerOutputDefinition,
    *,
    output_sha256: str,
    dependencies: Iterable[ProvenanceDependency],
    repository_observations: Iterable[ProvenanceObservation],
) -> GeneratedArtifactProvenance:
    """Build one canonical provenance record without filesystem access."""

    if not isinstance(expected, ResolvedProducerOutputDefinition):
        raise LineageValidationError(
            "expected definition must use ResolvedProducerOutputDefinition"
        )
    _require_closed_resolved_definition(expected)
    if not isinstance(output_sha256, str) or SHA256_RE.fullmatch(output_sha256) is None:
        raise LineageValidationError(
            "output_sha256 must be 64 lowercase hexadecimal characters"
        )

    dependency_records = tuple(dependencies)
    observation_records = tuple(repository_observations)
    if not all(
        isinstance(dependency, ProvenanceDependency)
        for dependency in dependency_records
    ):
        raise LineageValidationError(
            "provenance dependencies must use ProvenanceDependency"
        )
    if not all(
        isinstance(observation, ProvenanceObservation)
        for observation in observation_records
    ):
        raise LineageValidationError(
            "repository observations must use ProvenanceObservation"
        )

    record = GeneratedArtifactProvenance(
        output_path=expected.output_path,
        producer_command=expected.producer_command,
        producer_version=expected.producer_version,
        task_slug=expected.task_slug,
        output_sha256=output_sha256,
        dependencies=tuple(
            sorted(
                dependency_records,
                key=lambda dependency: dependency.dependency_path,
            )
        ),
        repository_observations=tuple(
            sorted(
                observation_records,
                key=lambda observation: (
                    observation.observation_path,
                    observation.predicate,
                ),
            )
        ),
    )
    return validate_provenance_record(record, expected)


def snapshot_dependencies(
    root: Path,
    expected: ResolvedProducerOutputDefinition,
) -> tuple[ProvenanceDependency, ...]:
    """Project exact dependency records from a single-read byte capture."""

    return provenance_dependencies(capture_dependencies(root, expected))


def capture_dependencies(
    root: Path,
    expected: ResolvedProducerOutputDefinition,
) -> tuple[CapturedDependency, ...]:
    """Capture each closed dependency state and present raw bytes exactly once.

    Structural path-safety failures are intentionally allowed to propagate.
    Ordinary filesystem states are represented in the immutable records.
    """

    if not isinstance(expected, ResolvedProducerOutputDefinition):
        raise LineageValidationError(
            "expected definition must use ResolvedProducerOutputDefinition"
    )
    _require_closed_resolved_definition(expected)

    captures: list[CapturedDependency] = []
    for dependency_path in sorted(expected.dependencies):
        target = resolve_under_root(root, dependency_path)
        if not target.exists():
            captures.append(
                CapturedDependency(
                    ProvenanceDependency(dependency_path, "missing", None),
                    None,
                )
            )
            continue
        if not target.is_file():
            captures.append(
                CapturedDependency(
                    ProvenanceDependency(
                        dependency_path,
                        "not_regular",
                        None,
                    ),
                    None,
                )
            )
            continue
        try:
            content = target.read_bytes()
        except OSError:
            captures.append(
                CapturedDependency(
                    ProvenanceDependency(
                        dependency_path,
                        "unreadable",
                        None,
                    ),
                    None,
                )
            )
            continue
        captures.append(
            CapturedDependency(
                ProvenanceDependency(
                    dependency_path,
                    "present",
                    sha256_bytes(content),
                ),
                content,
            )
        )
    return tuple(captures)


def provenance_dependencies(
    captures: Iterable[CapturedDependency],
) -> tuple[ProvenanceDependency, ...]:
    """Validate and project captured dependencies for manifest persistence."""

    captured = tuple(captures)
    if not all(
        isinstance(item, CapturedDependency) for item in captured
    ):
        raise LineageValidationError(
            "dependency captures must use CapturedDependency"
        )

    records: list[ProvenanceDependency] = []
    for item in captured:
        record = item.record
        if not isinstance(record, ProvenanceDependency):
            raise LineageValidationError(
                "captured dependency record must use ProvenanceDependency"
            )
        if record.dependency_state == "present":
            if not isinstance(item.content, bytes):
                raise LineageValidationError(
                    "present captured dependency requires exact raw bytes"
                )
            if record.dependency_sha256 != sha256_bytes(item.content):
                raise LineageValidationError(
                    "captured dependency hash does not match exact raw bytes"
                )
        elif item.content is not None:
            raise LineageValidationError(
                "non-present captured dependency must not contain raw bytes"
            )
        records.append(record)

    paths = tuple(record.dependency_path for record in records)
    if len(paths) != len(set(paths)):
        raise LineageValidationError(
            "dependency captures contain duplicate paths"
        )
    if paths != tuple(sorted(paths)):
        raise LineageValidationError(
            "dependency captures are not in canonical path order"
        )
    return tuple(records)


def snapshot_repository_observations(
    root: Path,
    expected: ResolvedProducerOutputDefinition,
) -> tuple[ProvenanceObservation, ...]:
    """Evaluate exactly the closed definition's shallow path predicates."""

    if not isinstance(expected, ResolvedProducerOutputDefinition):
        raise LineageValidationError(
            "expected definition must use ResolvedProducerOutputDefinition"
        )
    _require_closed_resolved_definition(expected)

    records: list[ProvenanceObservation] = []
    for observation in sorted(
        expected.repository_observations,
        key=lambda item: (item.relative_path, item.predicate),
    ):
        target = resolve_under_root(root, observation.relative_path)
        if observation.predicate == "exists":
            result = target.exists()
        elif observation.predicate == "is_file":
            result = target.is_file()
        elif observation.predicate == "is_dir":
            result = target.is_dir()
        else:
            raise LineageValidationError(
                f"unsupported repository observation predicate: "
                f"{observation.predicate!r}"
            )
        records.append(
            ProvenanceObservation(
                observation.relative_path,
                observation.predicate,
                result,
            )
        )
    return tuple(records)


def _validate_repository_root(repository_root: Path) -> Path:
    if not isinstance(repository_root, Path):
        raise LineageValidationError("repository_root must be a pathlib.Path")
    try:
        resolved = repository_root.resolve()
        is_directory = resolved.is_dir()
    except (OSError, RuntimeError):
        raise LineageValidationError(
            "repository_root could not be resolved"
        ) from None
    if not is_directory:
        raise LineageValidationError(
            "repository_root must be an existing directory"
        )
    return resolved


def _fallback_freshness(
    definitions: tuple[ResolvedProducerOutputDefinition, ...],
    inspected_states: tuple[InspectedProvenanceState, ...],
) -> dict[str, FreshnessState]:
    return {
        definition.registry_id: (
            FreshnessState.MISSING
            if inspected.output_state == "missing"
            else FreshnessState.UNKNOWN
        )
        for definition, inspected in zip(
            definitions,
            inspected_states,
            strict=True,
        )
    }


def _current_state_findings(
    inspected_states: tuple[InspectedProvenanceState, ...],
) -> tuple[ContextFinding, ...]:
    findings: list[ContextFinding] = []
    for inspected in inspected_states:
        if inspected.output_state == "unsafe":
            findings.append(
                ContextFinding(
                    "unsafe_lineage_output",
                    FindingLevel.BLOCKER,
                    inspected.output_path,
                    "Derived output path is unsafe for Lineage inspection: "
                    f"{inspected.output_path}.",
                )
            )
        elif inspected.output_state == "unreadable":
            findings.append(
                ContextFinding(
                    "unreadable_lineage_output",
                    FindingLevel.WARNING,
                    inspected.output_path,
                    "Derived output cannot be read for Lineage freshness: "
                    f"{inspected.output_path}.",
                )
            )
        elif inspected.output_state == "not_regular":
            findings.append(
                ContextFinding(
                    "nonregular_lineage_output",
                    FindingLevel.WARNING,
                    inspected.output_path,
                    "Derived output is not a regular file: "
                    f"{inspected.output_path}.",
                )
            )

        for dependency in inspected.dependencies:
            if dependency.dependency_state == "unsafe":
                findings.append(
                    ContextFinding(
                        "unsafe_lineage_dependency",
                        FindingLevel.BLOCKER,
                        dependency.dependency_path,
                        "Lineage dependency path is unsafe: "
                        f"{dependency.dependency_path}.",
                    )
                )
            elif dependency.dependency_state == "unreadable":
                findings.append(
                    ContextFinding(
                        "unreadable_lineage_dependency",
                        FindingLevel.WARNING,
                        dependency.dependency_path,
                        "Lineage dependency cannot be read: "
                        f"{dependency.dependency_path}.",
                    )
                )

        for observation in inspected.repository_observations:
            if observation.observation_state == "unsafe":
                findings.append(
                    ContextFinding(
                        "unsafe_lineage_observation",
                        FindingLevel.BLOCKER,
                        observation.observation_path,
                        "Lineage repository-observation path is unsafe: "
                        f"{observation.observation_path}.",
                    )
                )
            elif observation.observation_state == "unavailable":
                findings.append(
                    ContextFinding(
                        "unavailable_lineage_observation",
                        FindingLevel.WARNING,
                        observation.observation_path,
                        "Lineage repository observation is unavailable: "
                        f"{observation.observation_path}.",
                    )
                )
    return tuple(findings)


def _manifest_unreadable_finding() -> ContextFinding:
    return ContextFinding(
        code="lineage_manifest_unreadable",
        level=FindingLevel.BLOCKER,
        path=".harness/manifest.json",
        message=(
            "The main Harness manifest could not be read; Lineage freshness "
            "cannot be trusted."
        ),
    )


def _finding_sort_key(
    finding: ContextFinding,
) -> tuple[int, str, str, str]:
    severity = {
        FindingLevel.BLOCKER: 0,
        FindingLevel.WARNING: 1,
        FindingLevel.INFO: 2,
    }
    return (
        severity[finding.level],
        finding.code,
        finding.path or "",
        finding.message,
    )


def _inspect_output(root: Path, output_path: str) -> tuple[str, str | None]:
    try:
        target = resolve_managed_output_under_root(root, output_path)
    except PathSafetyError:
        return "unsafe", None
    except (OSError, RuntimeError):
        return "unreadable", None
    return _inspect_regular_file(target)


def _inspect_dependency(
    root: Path,
    dependency_path: str,
) -> InspectedDependencyState:
    try:
        target = resolve_under_root(root, dependency_path)
    except PathSafetyError:
        return InspectedDependencyState(
            dependency_path,
            "unsafe",
            None,
        )
    except (OSError, RuntimeError):
        return InspectedDependencyState(
            dependency_path,
            "unreadable",
            None,
        )
    state, digest = _inspect_regular_file(target)
    return InspectedDependencyState(dependency_path, state, digest)


def _inspect_regular_file(target: Path) -> tuple[str, str | None]:
    try:
        if not target.exists():
            return "missing", None
        if not target.is_file():
            return "not_regular", None
        return "present", sha256_file(target)
    except OSError:
        return "unreadable", None


def _inspect_observation(
    root: Path,
    observation: RepositoryObservationDefinition,
) -> InspectedObservationState:
    try:
        target = resolve_under_root(root, observation.relative_path)
    except PathSafetyError:
        return InspectedObservationState(
            observation.relative_path,
            observation.predicate,
            "unsafe",
            None,
        )
    except (OSError, RuntimeError):
        return InspectedObservationState(
            observation.relative_path,
            observation.predicate,
            "unavailable",
            None,
        )

    try:
        if observation.predicate == "exists":
            result = target.exists()
        elif observation.predicate == "is_file":
            result = target.is_file()
        elif observation.predicate == "is_dir":
            result = target.is_dir()
        else:
            raise LineageValidationError(
                "unsupported repository observation predicate: "
                f"{observation.predicate!r}"
            )
    except OSError:
        return InspectedObservationState(
            observation.relative_path,
            observation.predicate,
            "unavailable",
            None,
        )
    return InspectedObservationState(
        observation.relative_path,
        observation.predicate,
        "observed",
        result,
    )


def _validate_provenance_identity(
    record: GeneratedArtifactProvenance,
    expected: ResolvedProducerOutputDefinition,
    *,
    require_current_producer_version: bool,
) -> None:
    if not isinstance(record, GeneratedArtifactProvenance):
        raise LineageValidationError(
            "provenance record must use GeneratedArtifactProvenance"
        )
    if not isinstance(expected, ResolvedProducerOutputDefinition):
        raise LineageValidationError(
            "expected definition must use ResolvedProducerOutputDefinition"
        )
    _require_closed_resolved_definition(expected)

    metadata = [
        ("output path", record.output_path, expected.output_path),
        ("producer command", record.producer_command, expected.producer_command),
        ("task slug", record.task_slug, expected.task_slug),
    ]
    if require_current_producer_version:
        metadata.insert(
            2,
            (
                "producer version",
                record.producer_version,
                expected.producer_version,
            ),
        )
    for label, actual, required in metadata:
        if actual != required:
            raise LineageValidationError(
                f"provenance {label} does not match the closed definition: "
                f"expected {required!r}, got {actual!r}"
            )

    if not isinstance(record.dependencies, tuple) or not all(
        isinstance(dependency, ProvenanceDependency)
        for dependency in record.dependencies
    ):
        raise LineageValidationError(
            "provenance dependencies must use a tuple of ProvenanceDependency"
        )
    actual_dependency_paths = tuple(
        dependency.dependency_path for dependency in record.dependencies
    )
    _validate_exact_identities(
        "dependency",
        actual_dependency_paths,
        expected.dependencies,
    )
    canonical_dependencies = tuple(
        sorted(
            record.dependencies,
            key=lambda dependency: dependency.dependency_path,
        )
    )
    if record.dependencies != canonical_dependencies:
        raise LineageValidationError(
            "provenance dependencies are not in canonical path order"
        )

    if not isinstance(record.repository_observations, tuple) or not all(
        isinstance(observation, ProvenanceObservation)
        for observation in record.repository_observations
    ):
        raise LineageValidationError(
            "repository observations must use a tuple of ProvenanceObservation"
        )
    actual_observation_identities = tuple(
        (observation.observation_path, observation.predicate)
        for observation in record.repository_observations
    )
    expected_observation_identities = tuple(
        (observation.relative_path, observation.predicate)
        for observation in expected.repository_observations
    )
    _validate_exact_identities(
        "repository observation",
        actual_observation_identities,
        expected_observation_identities,
    )
    canonical_observations = tuple(
        sorted(
            record.repository_observations,
            key=lambda observation: (
                observation.observation_path,
                observation.predicate,
            ),
        )
    )
    if record.repository_observations != canonical_observations:
        raise LineageValidationError(
            "repository observations are not in canonical path/predicate order"
        )


def _validate_inspected_identity(
    inspected: InspectedProvenanceState,
    expected: ResolvedProducerOutputDefinition,
) -> None:
    if inspected.output_path != expected.output_path:
        raise LineageValidationError(
            "inspected output path does not match the closed definition"
        )
    dependency_paths = tuple(
        dependency.dependency_path for dependency in inspected.dependencies
    )
    _validate_exact_inspected_identities(
        "dependency",
        dependency_paths,
        tuple(sorted(expected.dependencies)),
    )
    observation_identities = tuple(
        (observation.observation_path, observation.predicate)
        for observation in inspected.repository_observations
    )
    expected_observation_identities = tuple(
        sorted(
            (
                observation.relative_path,
                observation.predicate,
            )
            for observation in expected.repository_observations
        )
    )
    _validate_exact_inspected_identities(
        "repository observation",
        observation_identities,
        expected_observation_identities,
    )


def _validate_exact_inspected_identities(
    label: str,
    actual: tuple[object, ...],
    expected: tuple[object, ...],
) -> None:
    if actual != expected:
        raise LineageValidationError(
            f"inspected {label} identities do not match the closed definition"
        )


def _has_unsafe_state(inspected: InspectedProvenanceState) -> bool:
    return (
        inspected.output_state == "unsafe"
        or any(
            dependency.dependency_state == "unsafe"
            for dependency in inspected.dependencies
        )
        or any(
            observation.observation_state == "unsafe"
            for observation in inspected.repository_observations
        )
    )


def _validate_inspected_file_state(
    path: str,
    state: str,
    digest: str | None,
    label: str,
) -> None:
    _validate_repository_path(path, f"{label} path")
    if not isinstance(state, str) or state not in _INSPECTED_FILE_STATES:
        raise LineageValidationError(
            f"{label} state must be present, missing, not_regular, "
            "unreadable, or unsafe"
        )
    if state == "present":
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise LineageValidationError(
                f"present {label} requires a lowercase SHA-256"
            )
    elif digest is not None:
        raise LineageValidationError(
            f"non-present {label} must not have a SHA-256"
        )


def _validate_repository_path(path: str, label: str) -> None:
    if not isinstance(path, str) or not path:
        raise LineageValidationError(
            f"{label} must be a non-empty repository-relative path"
        )
    if "\x00" in path:
        raise LineageValidationError(f"{label} contains an unsafe NUL character")
    if "\\" in path:
        raise LineageValidationError(
            f"{label} must use POSIX separators, not backslashes"
        )
    posix_path = PurePosixPath(path)
    windows_path = PureWindowsPath(path)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise LineageValidationError(f"{label} must be repository-relative")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise LineageValidationError(
            f"{label} contains an unsafe or non-normalized segment"
        )
    if posix_path.as_posix() != path:
        raise LineageValidationError(f"{label} must already be normalized")


def _resolve_definition(
    definition: ProducerOutputDefinition,
    task_slug: str,
) -> ResolvedProducerOutputDefinition:
    task_root = f".harness/tasks/{task_slug}"
    return ResolvedProducerOutputDefinition(
        registry_id=definition.registry_id,
        producer_command=definition.producer_command,
        producer_version=definition.producer_version,
        task_slug=task_slug,
        output_path=f"{task_root}/{definition.output_filename}",
        dependencies=tuple(
            (
                f"{task_root}/{dependency.relative_path}"
                if dependency.task_scoped
                else dependency.relative_path
            )
            for dependency in definition.dependencies
        ),
        repository_observations=definition.repository_observations,
        rerun_command=definition.rerun_command.format(task_slug=task_slug),
    )


def _validate_task_slug(task_slug: str) -> None:
    if not isinstance(task_slug, str) or TASK_SLUG_RE.fullmatch(task_slug) is None:
        raise LineageValidationError("task_slug must be a safe task slug")


def _lineage_definition_for_record(
    record: GeneratedArtifactProvenance,
) -> ResolvedProducerOutputDefinition | None:
    return next(
        (
            definition
            for definition in lineage_definitions_for_task(record.task_slug)
            if definition.output_path == record.output_path
        ),
        None,
    )


def _is_validation_report_output_path(output_path: str) -> bool:
    path = PurePosixPath(output_path)
    return (
        len(path.parts) == 4
        and path.parts[0] == ".harness"
        and path.parts[1] == "tasks"
        and TASK_SLUG_RE.fullmatch(path.parts[2]) is not None
        and path.parts[3] == VALIDATION_REPORT_FILENAME
    )


def _require_closed_resolved_definition(
    expected: ResolvedProducerOutputDefinition,
) -> None:
    if expected.registry_id == "validation_report":
        from .validation import validation_provenance_definition_for_task

        canonical = validation_provenance_definition_for_task(
            expected.task_slug
        )
    else:
        canonical = lineage_definition_for_task(
            expected.registry_id,
            expected.task_slug,
        )
    if expected != canonical:
        raise LineageValidationError(
            "expected definition does not match the closed Lineage definition "
            "or closed validation definition"
        )


def _validate_exact_identities(
    label: str,
    actual: tuple[object, ...],
    expected: tuple[object, ...],
) -> None:
    actual_set = set(actual)
    expected_set = set(expected)
    if len(actual) != len(actual_set):
        raise LineageValidationError(f"provenance contains duplicate {label} identities")
    missing = sorted(expected_set - actual_set)
    if missing:
        raise LineageValidationError(
            f"provenance is missing expected {label} identities: {missing!r}"
        )
    unexpected = sorted(actual_set - expected_set)
    if unexpected:
        raise LineageValidationError(
            f"provenance contains unexpected {label} identities: {unexpected!r}"
        )
