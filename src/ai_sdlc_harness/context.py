"""Context schema, registry, and deterministic selection foundations.

This module intentionally contains no provenance freshness calculation, command
integration, or file writing. It defines the closed registry, validated
manifest model, and reusable measured read-only selector used by later
context-discipline phases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

import yaml

from .constants import TASK_SLUG_PATTERN
from .files import sha256_bytes
from .redact import redact_text
from .requirements import RequirementsReadResult, validate_requirements_data


SCHEMA_VERSION = 1
ARTIFACT_ROLE = "context_selection_manifest"
AUTHORITY = "non_authoritative"
EDIT_MODEL = "generated_do_not_edit"
WORKSET_ARTIFACT_ROLE = "agent_handoff"

ENTRY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FINDING_CODE_RE = ENTRY_ID_RE
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
ENV_LINE_RE = re.compile(
    r"^\s*(?:export\s+|\$env:)?[A-Z_][A-Z0-9_]{1,}\s*=",
    re.IGNORECASE,
)
FINDING_LINE_RE = re.compile(r"^\s*-?\s*(blocker|warning|info):\s+(.+?)\s*$", re.IGNORECASE)

MAX_SOURCE_EXCERPT_CHARS = 1200
MAX_DERIVED_EXCERPT_CHARS = 900
MAX_EXCERPT_LINES = 40
MAX_REPORT_FINDINGS = 20

_DERIVED_PRODUCERS = {
    "spec": "spec",
    "requirements_projection": "spec",
    "preflight": "preflight",
    "test_contract_review": "test-contract",
    "agent_workset_output": "generate",
    "context_manifest_output": "generate",
    "evidence_report": "evidence",
    "validation_report": "validate",
}

_PROTECTED_INPUT_ENTRY_IDS = {
    "agent_workset_output",
    "context_manifest_output",
    "evidence_report",
    "validation_report",
    "global_agent_instructions",
}

_KNOWN_REPORT_TITLES = {
    "spec": "# Specification",
    "preflight": "# Preflight Report",
    "test_contract_review": "# Test-Contract Readiness Review",
}


class ContextValidationError(ValueError):
    """Raised when a context schema or registry is structurally invalid."""


class _ContextYamlStructureError(ValueError):
    """Raised internally for YAML mapping structures rejected before parsing."""


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _safe_yaml_key_description(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        rendered = repr(value)
        return rendered if len(rendered) <= 80 else f"{rendered[:77]}..."
    return f"<{type(value).__name__}>"


def _yaml_mapping_location(node: yaml.nodes.MappingNode) -> str:
    return f"line {node.start_mark.line + 1}, column {node.start_mark.column + 1}"


def _construct_strict_mapping(
    loader: _StrictSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if not isinstance(node, yaml.nodes.MappingNode):
        raise _ContextYamlStructureError("YAML mapping construction received a non-mapping node")
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError:
            raise _ContextYamlStructureError(
                "YAML mapping at "
                f"{_yaml_mapping_location(node)} has a non-string, unhashable key "
                f"{_safe_yaml_key_description(key)}"
            ) from None
        if duplicate:
            raise _ContextYamlStructureError(
                "duplicate YAML key "
                f"{_safe_yaml_key_description(key)} in mapping at {_yaml_mapping_location(node)}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_strict_mapping,
)


class Classification(str, Enum):
    SOURCE = "source"
    DERIVED = "derived"
    CONFIGURATION = "configuration"
    REPOSITORY_SIGNAL = "repository_signal"


class AuthorityLevel(str, Enum):
    TASK_SOURCE = "task_source"
    HARNESS_CONFIGURATION = "harness_configuration"
    REPOSITORY_OBSERVATION = "repository_observation"
    DERIVED_PROJECTION = "derived_projection"
    ADVISORY_REPORT = "advisory_report"
    HANDOFF_OUTPUT = "handoff_output"
    POST_IMPLEMENTATION_REPORT = "post_implementation_report"


class InclusionMode(str, Enum):
    READ_FIRST = "read_first"
    READ_IF_NEEDED = "read_if_needed"
    EXCLUDED = "excluded"


class ExistenceState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    NOT_APPLICABLE = "not_applicable"


class FreshnessState(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class FindingLevel(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class BudgetStatus(str, Enum):
    WITHIN_BUDGET = "within_budget"
    WARNING = "warning"
    HIGH_RISK = "high_risk"


@dataclass(frozen=True)
class SizeValues:
    bytes: int
    characters: int
    lines: int
    approximate_tokens: int


@dataclass(frozen=True)
class SizeEstimate:
    source: SizeValues
    selected: SizeValues


@dataclass(frozen=True)
class SourceArtifact:
    path: str
    authority_level: AuthorityLevel
    existence: ExistenceState
    sha256: str | None


@dataclass(frozen=True)
class GeneratedArtifact:
    path: str
    artifact_role: str
    authority_level: AuthorityLevel
    sha256: str


@dataclass(frozen=True)
class ContextEntry:
    entry_id: str
    path: str | None
    classification: Classification
    authority_level: AuthorityLevel
    inclusion_mode: InclusionMode
    order: int
    existence: ExistenceState
    freshness_state: FreshnessState
    size_estimate: SizeEstimate
    exclusion_reason: str | None


@dataclass(frozen=True)
class ContextFinding:
    code: str
    level: FindingLevel
    path: str | None
    message: str


@dataclass(frozen=True)
class BudgetConfiguration:
    per_file_warning_approximate_tokens: int
    per_file_high_risk_approximate_tokens: int
    total_warning_approximate_tokens: int
    total_high_risk_approximate_tokens: int


@dataclass(frozen=True)
class BudgetResult:
    selected_entry_count: int
    selected_bytes: int
    selected_characters: int
    selected_lines: int
    selected_approximate_tokens: int
    status: BudgetStatus


@dataclass(frozen=True)
class ContextBudget:
    configuration: BudgetConfiguration
    result: BudgetResult


@dataclass(frozen=True)
class ContextRegistryEntry:
    entry_id: str
    path: str | None
    classification: Classification
    authority_level: AuthorityLevel
    inclusion_mode: InclusionMode
    order: int
    exclusion_reason: str | None = None
    synthetic: bool = False
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryObservations:
    """Caller-supplied shallow repository observations; no detection occurs here."""

    available: bool = True
    git_repo: bool = False
    detected_languages: tuple[str, ...] = ()
    detected_package_managers: tuple[str, ...] = ()
    detected_test_frameworks: tuple[str, ...] = ()
    detected_ci: tuple[str, ...] = ()
    existing_agent_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class InspectedContextArtifact:
    """Read-only inspection state with source text kept separate from selection."""

    entry_id: str
    path: str | None
    classification: Classification
    authority_level: AuthorityLevel
    inclusion_mode: InclusionMode
    order: int
    existence: ExistenceState
    freshness_state: FreshnessState
    source_content: str | None
    source_size: SizeValues
    source_sha256: str | None = None


@dataclass(frozen=True)
class SelectedContextEntry:
    """One selected entry and its bounded, display-ready deterministic content."""

    entry_id: str
    path: str | None
    classification: Classification
    authority_level: AuthorityLevel
    inclusion_mode: InclusionMode
    order: int
    freshness_state: FreshnessState
    source_content: str | None
    selected_content: str
    report_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextSelectionResult:
    """Immutable output of deterministic inspection, gating, and selection."""

    task_slug: str
    registry: tuple[ContextRegistryEntry, ...]
    artifacts: tuple[InspectedContextArtifact, ...]
    selected_entries: tuple[SelectedContextEntry, ...]
    findings: tuple[ContextFinding, ...]
    repository_observations: RepositoryObservations
    context_entries: tuple[ContextEntry, ...]
    budget: ContextBudget

    def __post_init__(self) -> None:
        _validate_selection_result(self)

    @property
    def source_artifacts(self) -> tuple[InspectedContextArtifact, ...]:
        return tuple(item for item in self.artifacts if item.classification == Classification.SOURCE)

    @property
    def selected_content_by_entry(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.entry_id, item.selected_content) for item in self.selected_entries)


@dataclass(frozen=True)
class ContextManifest:
    schema_version: int
    artifact_path: str
    artifact_role: str
    authority: str
    edit_model: str
    task_slug: str
    source_artifacts: tuple[SourceArtifact, ...]
    generated_artifacts: tuple[GeneratedArtifact, ...]
    context_entries: tuple[ContextEntry, ...]
    findings: tuple[ContextFinding, ...]
    budget: ContextBudget


ZERO_SIZE = SizeValues(bytes=0, characters=0, lines=0, approximate_tokens=0)
_DEFAULT_BUDGET_CONFIGURATION = BudgetConfiguration(
    per_file_warning_approximate_tokens=4000,
    per_file_high_risk_approximate_tokens=8000,
    total_warning_approximate_tokens=12000,
    total_high_risk_approximate_tokens=24000,
)
_CONTEXT_BUDGET_FIELDS = (
    "per_file_warning_approximate_tokens",
    "per_file_high_risk_approximate_tokens",
    "total_warning_approximate_tokens",
    "total_high_risk_approximate_tokens",
)


def resolve_budget_configuration(
    configuration_data: Mapping[str, Any],
) -> tuple[BudgetConfiguration, tuple[ContextFinding, ...]]:
    """Resolve advisory context-budget settings from already-loaded configuration."""

    if not isinstance(configuration_data, Mapping):
        raise ContextValidationError("repository configuration must be a mapping")

    if "context_budget" not in configuration_data:
        return _DEFAULT_BUDGET_CONFIGURATION, (
            ContextFinding(
                code="context_budget_defaults_applied",
                level=FindingLevel.INFO,
                path=".harness/config.yaml",
                message=(
                    "The context_budget section is absent; the complete default set of "
                    "advisory model-agnostic context-budget thresholds was applied."
                ),
            ),
        )

    section = configuration_data["context_budget"]
    if isinstance(section, Mapping):
        keys = tuple(section.keys())
        if (
            all(isinstance(key, str) for key in keys)
            and len(keys) == len(_CONTEXT_BUDGET_FIELDS)
            and set(keys) == set(_CONTEXT_BUDGET_FIELDS)
        ):
            candidate = BudgetConfiguration(
                **{field_name: section[field_name] for field_name in _CONTEXT_BUDGET_FIELDS}
            )
            try:
                return _validate_budget_configuration(candidate), ()
            except ContextValidationError:
                pass

    return _DEFAULT_BUDGET_CONFIGURATION, (
        ContextFinding(
            code="malformed_context_budget_configuration",
            level=FindingLevel.WARNING,
            path=".harness/config.yaml",
            message=(
                "The context_budget section is malformed; the complete default set of "
                "advisory model-agnostic context-budget thresholds was used."
            ),
        ),
    )


def context_manifest_path(task_slug: str) -> str:
    _validate_task_slug(task_slug)
    return f".harness/tasks/{task_slug}/generated/context-manifest.yaml"


def agent_workset_path(task_slug: str) -> str:
    _validate_task_slug(task_slug)
    return f".harness/tasks/{task_slug}/generated/agent-workset.md"


def build_context_registry(task_slug: str) -> tuple[ContextRegistryEntry, ...]:
    """Return the closed registry for the approved task context surface."""

    _validate_task_slug(task_slug)
    task_root = f".harness/tasks/{task_slug}"
    entries = (
        ContextRegistryEntry("task", f"{task_root}/task.md", Classification.SOURCE, AuthorityLevel.TASK_SOURCE, InclusionMode.READ_FIRST, 10),
        ContextRegistryEntry("acceptance", f"{task_root}/acceptance.md", Classification.SOURCE, AuthorityLevel.TASK_SOURCE, InclusionMode.READ_FIRST, 20),
        ContextRegistryEntry("architecture_notes", f"{task_root}/architecture-notes.md", Classification.SOURCE, AuthorityLevel.TASK_SOURCE, InclusionMode.READ_FIRST, 30),
        ContextRegistryEntry("coupling_notes", f"{task_root}/coupling-notes.md", Classification.SOURCE, AuthorityLevel.TASK_SOURCE, InclusionMode.READ_FIRST, 40),
        ContextRegistryEntry("test_contract", f"{task_root}/test-contract.md", Classification.SOURCE, AuthorityLevel.TASK_SOURCE, InclusionMode.READ_FIRST, 50),
        ContextRegistryEntry("verification", f"{task_root}/verification.md", Classification.SOURCE, AuthorityLevel.TASK_SOURCE, InclusionMode.READ_IF_NEEDED, 60),
        ContextRegistryEntry("evidence", f"{task_root}/evidence.md", Classification.SOURCE, AuthorityLevel.TASK_SOURCE, InclusionMode.READ_IF_NEEDED, 70),
        ContextRegistryEntry("spec", f"{task_root}/spec.md", Classification.DERIVED, AuthorityLevel.DERIVED_PROJECTION, InclusionMode.READ_IF_NEEDED, 80),
        ContextRegistryEntry("requirements_projection", f"{task_root}/requirements.yaml", Classification.DERIVED, AuthorityLevel.DERIVED_PROJECTION, InclusionMode.READ_IF_NEEDED, 90),
        ContextRegistryEntry("preflight", f"{task_root}/preflight.md", Classification.DERIVED, AuthorityLevel.ADVISORY_REPORT, InclusionMode.READ_IF_NEEDED, 100),
        ContextRegistryEntry("test_contract_review", f"{task_root}/test-contract-review.md", Classification.DERIVED, AuthorityLevel.ADVISORY_REPORT, InclusionMode.READ_IF_NEEDED, 110),
        ContextRegistryEntry("selected_packs", ".harness/packs/selected.yaml", Classification.CONFIGURATION, AuthorityLevel.HARNESS_CONFIGURATION, InclusionMode.READ_IF_NEEDED, 120),
        ContextRegistryEntry("repository_signals", None, Classification.REPOSITORY_SIGNAL, AuthorityLevel.REPOSITORY_OBSERVATION, InclusionMode.READ_IF_NEEDED, 130, synthetic=True),
        ContextRegistryEntry("agent_workset_output", agent_workset_path(task_slug), Classification.DERIVED, AuthorityLevel.HANDOFF_OUTPUT, InclusionMode.EXCLUDED, 200, exclusion_reason="generated_output_not_input"),
        ContextRegistryEntry("context_manifest_output", context_manifest_path(task_slug), Classification.DERIVED, AuthorityLevel.DERIVED_PROJECTION, InclusionMode.EXCLUDED, 210, exclusion_reason="self_generated_output_not_input"),
        ContextRegistryEntry("evidence_report", f"{task_root}/evidence-report.md", Classification.DERIVED, AuthorityLevel.POST_IMPLEMENTATION_REPORT, InclusionMode.EXCLUDED, 220, exclusion_reason="post_implementation_artifact"),
        ContextRegistryEntry("validation_report", f"{task_root}/validation-report.md", Classification.DERIVED, AuthorityLevel.POST_IMPLEMENTATION_REPORT, InclusionMode.EXCLUDED, 230, exclusion_reason="post_implementation_artifact"),
        ContextRegistryEntry("global_agent_instructions", ".harness/generated/agent-instructions.md", Classification.DERIVED, AuthorityLevel.HANDOFF_OUTPUT, InclusionMode.EXCLUDED, 240, exclusion_reason="global_generated_output_not_task_input"),
    )
    return validate_registry(entries)


def validate_registry(entries: Iterable[ContextRegistryEntry]) -> tuple[ContextRegistryEntry, ...]:
    """Validate and deterministically order a context registry."""

    normalized = tuple(entries)
    by_id: dict[str, ContextRegistryEntry] = {}
    by_path: dict[str, ContextRegistryEntry] = {}
    for entry in normalized:
        _validate_registry_entry(entry)
        if entry.entry_id in by_id:
            raise ContextValidationError(f"duplicate context registry entry_id: {entry.entry_id}")
        by_id[entry.entry_id] = entry
        if entry.path is not None:
            prior = by_path.get(entry.path)
            if prior is not None:
                if prior.classification != entry.classification:
                    raise ContextValidationError(
                        f"conflicting registry classifications for {entry.path}: "
                        f"{prior.classification.value} and {entry.classification.value}"
                    )
                raise ContextValidationError(f"duplicate context registry path: {entry.path}")
            by_path[entry.path] = entry

    for entry in normalized:
        for dependency in entry.dependencies:
            if dependency == entry.entry_id:
                raise ContextValidationError(f"self-dependency is not allowed for registry entry {entry.entry_id}")
            if dependency not in by_id:
                raise ContextValidationError(
                    f"registry entry {entry.entry_id} depends on unknown entry {dependency}"
                )
    _validate_acyclic_registry(by_id)
    return tuple(sorted(normalized, key=lambda item: (item.order, item.entry_id)))


def select_context(
    repository_root: Path,
    task_slug: str,
    *,
    derived_freshness: Mapping[str, FreshnessState],
    repository_observations: RepositoryObservations | None = None,
    registry: Iterable[ContextRegistryEntry] | None = None,
    budget_configuration: BudgetConfiguration | None = None,
) -> ContextSelectionResult:
    """Inspect and deterministically select registered task context without writes."""

    _validate_task_slug(task_slug)
    root = _validate_repository_root(repository_root)
    validated_registry = (
        build_context_registry(task_slug) if registry is None else validate_registry(registry)
    )
    _validate_selection_registry(validated_registry, task_slug)
    freshness = _validate_derived_freshness(derived_freshness, validated_registry)
    observations = _validate_repository_observations(
        repository_observations or RepositoryObservations(available=False)
    )
    configuration = _validate_budget_configuration(
        budget_configuration or _DEFAULT_BUDGET_CONFIGURATION
    )

    artifacts: list[InspectedContextArtifact] = []
    selected: list[SelectedContextEntry] = []
    findings: list[ContextFinding] = []

    for entry in validated_registry:
        if entry.synthetic:
            observation_content = (
                _render_repository_observations(observations)
                if observations.available
                else None
            )
            artifact = InspectedContextArtifact(
                entry_id=entry.entry_id,
                path=None,
                classification=entry.classification,
                authority_level=entry.authority_level,
                inclusion_mode=entry.inclusion_mode,
                order=entry.order,
                existence=ExistenceState.NOT_APPLICABLE,
                freshness_state=FreshnessState.NOT_APPLICABLE,
                source_content=observation_content,
                source_size=(
                    _measure_text(observation_content)
                    if observation_content is not None
                    else ZERO_SIZE
                ),
                source_sha256=None,
            )
            artifacts.append(artifact)
            if entry.inclusion_mode != InclusionMode.EXCLUDED:
                if observations.available:
                    selected.append(
                        _selected_entry(
                            artifact,
                            observation_content or "",
                        )
                    )
                else:
                    findings.append(
                        ContextFinding(
                            code="repository_observation_unavailable",
                            level=FindingLevel.WARNING,
                            path=None,
                            message="Current repository observations were not supplied to context selection.",
                        )
                    )
            continue

        artifact, inspection_findings = _inspect_registered_artifact(root, entry, freshness)
        artifacts.append(artifact)
        findings.extend(inspection_findings)
        if entry.inclusion_mode == InclusionMode.EXCLUDED:
            continue
        if artifact.existence != ExistenceState.PRESENT:
            continue
        if entry.classification == Classification.DERIVED:
            if artifact.freshness_state != FreshnessState.FRESH:
                findings.append(_derived_exclusion_finding(entry, artifact.freshness_state, task_slug))
                continue

        content, content_findings, report_findings = _process_selected_content(
            task_slug,
            artifact,
        )
        findings.extend(content_findings)
        if content is None:
            continue
        selected.append(_selected_entry(artifact, content, report_findings))

    selected.sort(key=_selection_sort_key)
    _validate_selected_entries(selected, task_slug)
    context_entries = _build_measured_context_entries(
        validated_registry,
        artifacts,
        selected,
        task_slug,
    )
    budget, budget_findings = _evaluate_context_budget(context_entries, configuration)
    findings.extend(budget_findings)
    ordered_findings = tuple(sorted((_validate_finding(item) for item in findings), key=_finding_sort_key))
    return ContextSelectionResult(
        task_slug=task_slug,
        registry=validated_registry,
        artifacts=tuple(artifacts),
        selected_entries=tuple(selected),
        findings=ordered_findings,
        repository_observations=observations,
        context_entries=context_entries,
        budget=budget,
    )


def _validate_repository_root(repository_root: Path) -> Path:
    if not isinstance(repository_root, Path):
        raise ContextValidationError("repository_root must be a pathlib.Path")
    try:
        resolved = repository_root.resolve()
        is_directory = resolved.is_dir()
    except (OSError, RuntimeError):
        raise ContextValidationError("repository_root could not be resolved") from None
    if not is_directory:
        raise ContextValidationError("repository_root must be an existing directory")
    return resolved


def _validate_selection_registry(
    registry: tuple[ContextRegistryEntry, ...],
    task_slug: str,
) -> None:
    protected_paths = {
        agent_workset_path(task_slug),
        context_manifest_path(task_slug),
        f".harness/tasks/{task_slug}/evidence-report.md",
        f".harness/tasks/{task_slug}/validation-report.md",
        ".harness/generated/agent-instructions.md",
    }
    for entry in registry:
        if (
            entry.classification == Classification.DERIVED
            and entry.inclusion_mode != InclusionMode.EXCLUDED
            and entry.entry_id not in _DERIVED_PRODUCERS
        ):
            raise ContextValidationError(
                f"selected derived registry entry has no producer mapping: {entry.entry_id}"
            )
        if entry.entry_id in _PROTECTED_INPUT_ENTRY_IDS and entry.inclusion_mode != InclusionMode.EXCLUDED:
            raise ContextValidationError(f"protected context output cannot be selected: {entry.entry_id}")
        if entry.path in protected_paths and entry.inclusion_mode != InclusionMode.EXCLUDED:
            raise ContextValidationError(f"protected context output cannot be selected: {entry.path}")
        if entry.inclusion_mode != InclusionMode.EXCLUDED:
            protected_dependencies = sorted(set(entry.dependencies) & _PROTECTED_INPUT_ENTRY_IDS)
            if protected_dependencies:
                raise ContextValidationError(
                    f"selected entry {entry.entry_id} cannot depend on protected output "
                    f"{protected_dependencies[0]}"
                )


def _validate_derived_freshness(
    supplied: Mapping[str, FreshnessState],
    registry: tuple[ContextRegistryEntry, ...],
) -> dict[str, FreshnessState]:
    if not isinstance(supplied, Mapping):
        raise ContextValidationError("derived_freshness must be a mapping")
    registry_by_id = {entry.entry_id: entry for entry in registry}
    validated: dict[str, FreshnessState] = {}
    allowed = {
        FreshnessState.FRESH,
        FreshnessState.STALE,
        FreshnessState.UNKNOWN,
        FreshnessState.MISSING,
    }
    for entry_id, state in supplied.items():
        if not isinstance(entry_id, str):
            raise ContextValidationError("derived_freshness keys must be entry_id strings")
        entry = registry_by_id.get(entry_id)
        if entry is None:
            raise ContextValidationError(f"derived_freshness references unknown entry: {entry_id}")
        if entry.classification != Classification.DERIVED:
            raise ContextValidationError(f"derived_freshness entry is not derived: {entry_id}")
        if not isinstance(state, FreshnessState) or state not in allowed:
            raise ContextValidationError(
                f"derived_freshness.{entry_id} must be fresh, stale, unknown, or missing"
            )
        validated[entry_id] = state
    return validated


def _validate_repository_observations(
    observations: RepositoryObservations,
) -> RepositoryObservations:
    if not isinstance(observations, RepositoryObservations):
        raise ContextValidationError("repository_observations must use RepositoryObservations")
    if not isinstance(observations.available, bool):
        raise ContextValidationError("repository_observations.available must be a boolean")
    if not observations.available:
        return RepositoryObservations(available=False)
    if not isinstance(observations.git_repo, bool):
        raise ContextValidationError("repository_observations.git_repo must be a boolean")

    def normalized(values: tuple[str, ...], label: str) -> tuple[str, ...]:
        if not isinstance(values, tuple) or any(not isinstance(item, str) or not item for item in values):
            raise ContextValidationError(f"repository_observations.{label} must be a tuple of strings")
        return tuple(sorted(set(values)))

    return RepositoryObservations(
        available=True,
        git_repo=observations.git_repo,
        detected_languages=normalized(observations.detected_languages, "detected_languages"),
        detected_package_managers=normalized(
            observations.detected_package_managers,
            "detected_package_managers",
        ),
        detected_test_frameworks=normalized(
            observations.detected_test_frameworks,
            "detected_test_frameworks",
        ),
        detected_ci=normalized(observations.detected_ci, "detected_ci"),
        existing_agent_files=normalized(observations.existing_agent_files, "existing_agent_files"),
    )


def _inspect_registered_artifact(
    root: Path,
    entry: ContextRegistryEntry,
    freshness: Mapping[str, FreshnessState],
) -> tuple[InspectedContextArtifact, tuple[ContextFinding, ...]]:
    assert entry.path is not None
    try:
        target = (root / Path(*PurePosixPath(entry.path).parts)).resolve()
    except (OSError, RuntimeError):
        return _unsafe_artifact(entry), (_unsafe_path_finding(entry),)
    if target != root and root not in target.parents:
        return _unsafe_artifact(entry), (_unsafe_path_finding(entry),)

    try:
        exists = target.exists()
        regular_file = target.is_file() if exists else False
    except OSError:
        return _unreadable_artifact(entry, freshness), (_unreadable_finding(entry),)

    if not exists:
        state = (
            FreshnessState.MISSING
            if entry.classification == Classification.DERIVED
            else FreshnessState.NOT_APPLICABLE
        )
        artifact = _artifact_record(entry, ExistenceState.MISSING, state, None)
        finding = _missing_finding(entry)
        return artifact, (() if finding is None else (finding,))
    if not regular_file:
        return _unreadable_artifact(entry, freshness), (_unreadable_finding(entry),)
    if entry.inclusion_mode == InclusionMode.EXCLUDED:
        return _artifact_record(entry, ExistenceState.PRESENT, FreshnessState.NOT_APPLICABLE, None), ()

    state = (
        freshness.get(entry.entry_id, FreshnessState.UNKNOWN)
        if entry.classification == Classification.DERIVED
        else FreshnessState.NOT_APPLICABLE
    )
    if entry.classification == Classification.DERIVED and state != FreshnessState.FRESH:
        return _artifact_record(entry, ExistenceState.PRESENT, state, None), ()

    try:
        raw_content = target.read_bytes()
        source_content = raw_content.decode("utf-8")
    except (OSError, UnicodeError):
        return _unreadable_artifact(entry, freshness), (_unreadable_finding(entry),)
    return _artifact_record(
        entry,
        ExistenceState.PRESENT,
        state,
        source_content,
        _measure_source(raw_content, source_content),
        sha256_bytes(raw_content),
    ), ()


def _artifact_record(
    entry: ContextRegistryEntry,
    existence: ExistenceState,
    freshness_state: FreshnessState,
    source_content: str | None,
    source_size: SizeValues = ZERO_SIZE,
    source_sha256: str | None = None,
) -> InspectedContextArtifact:
    return InspectedContextArtifact(
        entry_id=entry.entry_id,
        path=entry.path,
        classification=entry.classification,
        authority_level=entry.authority_level,
        inclusion_mode=entry.inclusion_mode,
        order=entry.order,
        existence=existence,
        freshness_state=freshness_state,
        source_content=source_content,
        source_size=source_size,
        source_sha256=source_sha256,
    )


def _unsafe_artifact(entry: ContextRegistryEntry) -> InspectedContextArtifact:
    state = (
        FreshnessState.UNKNOWN
        if entry.classification == Classification.DERIVED
        else FreshnessState.NOT_APPLICABLE
    )
    return _artifact_record(entry, ExistenceState.UNREADABLE, state, None)


def _unreadable_artifact(
    entry: ContextRegistryEntry,
    freshness: Mapping[str, FreshnessState],
) -> InspectedContextArtifact:
    state = (
        freshness.get(entry.entry_id, FreshnessState.UNKNOWN)
        if entry.classification == Classification.DERIVED
        else FreshnessState.NOT_APPLICABLE
    )
    return _artifact_record(entry, ExistenceState.UNREADABLE, state, None)


def _missing_finding(entry: ContextRegistryEntry) -> ContextFinding | None:
    if entry.inclusion_mode == InclusionMode.EXCLUDED:
        return None
    if entry.classification == Classification.SOURCE:
        return ContextFinding(
            code="missing_required_source",
            level=FindingLevel.BLOCKER,
            path=entry.path,
            message=f"Required task source is missing: {entry.path}.",
        )
    return ContextFinding(
        code="missing_optional_artifact",
        level=FindingLevel.WARNING,
        path=entry.path,
        message=f"Optional context artifact is missing: {entry.path}.",
    )


def _unreadable_finding(entry: ContextRegistryEntry) -> ContextFinding:
    if entry.classification == Classification.SOURCE:
        return ContextFinding(
            code="unreadable_required_source",
            level=FindingLevel.BLOCKER,
            path=entry.path,
            message=f"Required task source is not a readable UTF-8 regular file: {entry.path}.",
        )
    return ContextFinding(
        code="unreadable_optional_artifact",
        level=FindingLevel.WARNING,
        path=entry.path,
        message=f"Optional context artifact is not a readable UTF-8 regular file: {entry.path}.",
    )


def _unsafe_path_finding(entry: ContextRegistryEntry) -> ContextFinding:
    return ContextFinding(
        code="unsafe_resolved_path",
        level=FindingLevel.BLOCKER,
        path=entry.path,
        message=f"Registered context path resolves outside the repository root: {entry.path}.",
    )


def _derived_exclusion_finding(
    entry: ContextRegistryEntry,
    state: FreshnessState,
    task_slug: str,
) -> ContextFinding:
    producer = _DERIVED_PRODUCERS[entry.entry_id]
    rerun = f"ai-sdlc {producer} --task {task_slug}"
    if state == FreshnessState.STALE:
        code = "stale_derived_artifact_excluded"
        message = f"Stale derived artifact was excluded: {entry.path}; rerun `{rerun}`."
    elif state == FreshnessState.MISSING:
        code = "missing_optional_artifact"
        message = f"Derived artifact is marked missing and was excluded: {entry.path}; rerun `{rerun}`."
    else:
        code = "unknown_derived_artifact_excluded"
        message = (
            f"Derived artifact has unknown freshness and was excluded: {entry.path}; "
            f"rerun `{rerun}`."
        )
    return ContextFinding(code=code, level=FindingLevel.WARNING, path=entry.path, message=message)


def _process_selected_content(
    task_slug: str,
    artifact: InspectedContextArtifact,
) -> tuple[str | None, tuple[ContextFinding, ...], tuple[str, ...]]:
    assert artifact.source_content is not None
    if artifact.entry_id == "requirements_projection":
        return _process_requirements(task_slug, artifact)
    if artifact.entry_id == "selected_packs":
        return _process_selected_packs(artifact)
    if artifact.entry_id in _KNOWN_REPORT_TITLES:
        return _process_known_report(artifact)
    max_chars = (
        MAX_SOURCE_EXCERPT_CHARS
        if artifact.classification == Classification.SOURCE
        else MAX_DERIVED_EXCERPT_CHARS
    )
    return _bounded_selected_text(artifact.source_content, max_chars=max_chars), (), ()


def _process_requirements(
    task_slug: str,
    artifact: InspectedContextArtifact,
) -> tuple[str | None, tuple[ContextFinding, ...], tuple[str, ...]]:
    assert artifact.source_content is not None
    try:
        data = yaml.load(artifact.source_content, Loader=_StrictSafeLoader)
    except (_ContextYamlStructureError, yaml.YAMLError):
        finding = ContextFinding(
            code="malformed_requirements_projection",
            level=FindingLevel.WARNING,
            path=artifact.path,
            message="requirements.yaml is malformed YAML and was excluded.",
        )
        return None, (finding,), ()
    result = validate_requirements_data(data, expected_slug=task_slug)
    if not result.schema_valid:
        finding = ContextFinding(
            code="malformed_requirements_projection",
            level=FindingLevel.WARNING,
            path=artifact.path,
            message="requirements.yaml has an invalid schema and was excluded.",
        )
        return None, (finding,), ()
    rendered = _render_requirements_selection(result)
    return _bounded_selected_text(rendered, max_chars=MAX_DERIVED_EXCERPT_CHARS), (), ()


def _render_requirements_selection(result: RequirementsReadResult) -> str:
    lines = [
        "Artifact role: advisory projection.",
        "Authority: non-authoritative.",
        f"Requirement count: {result.requirement_count}",
        f"Finding count: {result.finding_count}",
        "Requirements:",
    ]
    if result.requirements:
        for requirement in result.requirements:
            lines.append(f"- {requirement['id']} [{requirement['status']}]: {requirement['statement']}")
            lines.append(f"  - acceptance_criteria: {requirement['acceptance_criteria']}")
            lines.append(f"  - verification: {requirement['verification']}")
    else:
        lines.append("- none recorded")
    lines.append("Findings:")
    if result.findings:
        for finding in result.findings:
            lines.append(f"- {finding['level']}: {finding['message']}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def _process_selected_packs(
    artifact: InspectedContextArtifact,
) -> tuple[str | None, tuple[ContextFinding, ...], tuple[str, ...]]:
    assert artifact.source_content is not None
    try:
        data = yaml.load(artifact.source_content, Loader=_StrictSafeLoader)
    except (_ContextYamlStructureError, yaml.YAMLError):
        data = None
    if not isinstance(data, Mapping) or not isinstance(data.get("selected_packs"), list):
        finding = ContextFinding(
            code="malformed_selected_packs",
            level=FindingLevel.WARNING,
            path=artifact.path,
            message="Selected-pack configuration is malformed and was excluded.",
        )
        return None, (finding,), ()
    lines: list[str] = []
    for item in data["selected_packs"]:
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        enabled = "yes" if bool(item.get("enabled", False)) else "no"
        lines.append(
            f"- selected record: {item['id']} "
            f"(version: {item.get('version', 'unknown')}, enabled: {enabled})"
        )
    rendered = "\n".join(lines) if lines else "No optional packs selected."
    return _bounded_selected_text(rendered, max_chars=MAX_DERIVED_EXCERPT_CHARS), (), ()


def _process_known_report(
    artifact: InspectedContextArtifact,
) -> tuple[str | None, tuple[ContextFinding, ...], tuple[str, ...]]:
    assert artifact.source_content is not None
    normalized = artifact.source_content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    first_nonempty = next((line for line in normalized.split("\n") if line.strip()), None)
    if first_nonempty != _KNOWN_REPORT_TITLES[artifact.entry_id]:
        finding = ContextFinding(
            code="malformed_known_report",
            level=FindingLevel.WARNING,
            path=artifact.path,
            message=f"Known report format is malformed and was excluded: {artifact.path}.",
        )
        return None, (finding,), ()
    extracted: list[str] = []
    for line in artifact.source_content.splitlines():
        match = FINDING_LINE_RE.match(line)
        if match:
            extracted.append(f"{match.group(1).lower()}: {redact_text(match.group(2))}")
        if len(extracted) == MAX_REPORT_FINDINGS:
            break
    if not extracted:
        extracted.append(f"info: {PurePosixPath(artifact.path or '').name} has no findings.")
    content = _bounded_selected_text(
        artifact.source_content,
        max_chars=MAX_DERIVED_EXCERPT_CHARS,
    )
    return content, (), tuple(extracted)


def _bounded_selected_text(text: str, *, max_chars: int) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    redacted = redact_text(normalized)
    kept: list[str] = []
    removed_environment_lines = 0
    for line in redacted.split("\n"):
        if ENV_LINE_RE.match(line):
            removed_environment_lines += 1
        else:
            kept.append(line)

    lines = "\n".join(kept).strip().splitlines()
    truncated = len(lines) > MAX_EXCERPT_LINES
    lines = lines[:MAX_EXCERPT_LINES]
    excerpt = "\n".join(lines).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip()
        truncated = True
    notes: list[str] = []
    if removed_environment_lines:
        notes.append(f"[{removed_environment_lines} environment-style line(s) omitted.]")
    if truncated:
        notes.append("[Excerpt truncated; see the source artifact for full content.]")
    combined = (excerpt + "\n\n" if excerpt and notes else excerpt) + "\n".join(notes)
    combined_lines = combined.splitlines()[:MAX_EXCERPT_LINES]
    bounded = "\n".join(combined_lines).strip()
    if len(bounded) > max_chars:
        bounded = bounded[:max_chars].rstrip()
    return bounded or "[No excerptable content.]"


def _render_repository_observations(observations: RepositoryObservations) -> str:
    def formatted(values: tuple[str, ...]) -> str:
        return ", ".join(values) if values else "none"

    return "\n".join(
        (
            f"- Git repo detected: {'yes' if observations.git_repo else 'no'}",
            f"- Detected languages: {formatted(observations.detected_languages)}",
            f"- Detected package managers: {formatted(observations.detected_package_managers)}",
            f"- Detected test frameworks: {formatted(observations.detected_test_frameworks)}",
            f"- Detected CI: {formatted(observations.detected_ci)}",
            f"- Existing agent files: {formatted(observations.existing_agent_files)}",
        )
    )


def _selected_entry(
    artifact: InspectedContextArtifact,
    selected_content: str,
    report_findings: tuple[str, ...] = (),
) -> SelectedContextEntry:
    return SelectedContextEntry(
        entry_id=artifact.entry_id,
        path=artifact.path,
        classification=artifact.classification,
        authority_level=artifact.authority_level,
        inclusion_mode=artifact.inclusion_mode,
        order=artifact.order,
        freshness_state=artifact.freshness_state,
        source_content=artifact.source_content,
        selected_content=selected_content,
        report_findings=report_findings,
    )


def _selection_sort_key(entry: SelectedContextEntry) -> tuple[int, int, str]:
    inclusion_rank = {
        InclusionMode.READ_FIRST: 0,
        InclusionMode.READ_IF_NEEDED: 1,
        InclusionMode.EXCLUDED: 2,
    }
    return inclusion_rank[entry.inclusion_mode], entry.order, entry.entry_id


def _validate_selected_entries(
    selected: Iterable[SelectedContextEntry],
    task_slug: str,
) -> None:
    seen_paths: set[str] = set()
    protected_paths = {
        agent_workset_path(task_slug),
        context_manifest_path(task_slug),
        f".harness/tasks/{task_slug}/evidence-report.md",
        f".harness/tasks/{task_slug}/validation-report.md",
        ".harness/generated/agent-instructions.md",
    }
    for entry in selected:
        if entry.entry_id in _PROTECTED_INPUT_ENTRY_IDS or entry.path in protected_paths:
            raise ContextValidationError(f"protected context output was selected: {entry.entry_id}")
        if entry.path is not None:
            if entry.path in seen_paths:
                raise ContextValidationError(f"duplicate selected context path: {entry.path}")
            seen_paths.add(entry.path)


def _logical_line_count(text: str) -> int:
    """Count normalized logical lines without adding a terminal empty line."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return 0
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    return normalized.count("\n") + 1


def _approximate_tokens(character_count: int) -> int:
    """Return the model-agnostic ceil(characters / 4) heuristic."""

    _non_negative_integer(character_count, "character_count")
    return (character_count + 3) // 4


def _measure_source(raw_content: bytes, decoded_content: str) -> SizeValues:
    return SizeValues(
        bytes=len(raw_content),
        characters=len(decoded_content),
        lines=_logical_line_count(decoded_content),
        approximate_tokens=_approximate_tokens(len(decoded_content)),
    )


def measure_text(text: str) -> SizeValues:
    """Measure one exact text value with the canonical context size model."""

    if not isinstance(text, str):
        raise ContextValidationError("measured context must be text")
    return SizeValues(
        bytes=len(text.encode("utf-8")),
        characters=len(text),
        lines=_logical_line_count(text),
        approximate_tokens=_approximate_tokens(len(text)),
    )


def _measure_text(text: str) -> SizeValues:
    return measure_text(text)


def _build_measured_context_entries(
    registry: tuple[ContextRegistryEntry, ...],
    artifacts: Iterable[InspectedContextArtifact],
    selected: Iterable[SelectedContextEntry],
    task_slug: str,
) -> tuple[ContextEntry, ...]:
    artifacts_by_id = {item.entry_id: item for item in artifacts}
    selected_by_id = {item.entry_id: item for item in selected}
    registry_by_id = {item.entry_id: item for item in registry}
    entries: list[ContextEntry] = []
    for registered in registry:
        artifact = artifacts_by_id.get(registered.entry_id)
        if artifact is None:
            raise ContextValidationError(
                f"missing inspection record for registry entry: {registered.entry_id}"
            )
        selected_entry = selected_by_id.get(registered.entry_id)
        selected_size = (
            _measure_text(selected_entry.selected_content)
            if selected_entry is not None
            else ZERO_SIZE
        )
        context_entry = ContextEntry(
            entry_id=registered.entry_id,
            path=registered.path,
            classification=registered.classification,
            authority_level=registered.authority_level,
            inclusion_mode=registered.inclusion_mode,
            order=registered.order,
            existence=artifact.existence,
            freshness_state=artifact.freshness_state,
            size_estimate=SizeEstimate(
                source=artifact.source_size,
                selected=selected_size,
            ),
            exclusion_reason=registered.exclusion_reason,
        )
        entries.append(
            _validate_context_entry(
                context_entry,
                registry_by_id=registry_by_id,
                artifact_path=context_manifest_path(task_slug),
                generated_paths=set(),
            )
        )
    return tuple(sorted(entries, key=lambda item: (item.order, item.entry_id)))


def _evaluate_context_budget(
    entries: tuple[ContextEntry, ...],
    configuration: BudgetConfiguration,
) -> tuple[ContextBudget, tuple[ContextFinding, ...]]:
    configuration = _validate_budget_configuration(configuration)
    selected = [item for item in entries if item.size_estimate.selected != ZERO_SIZE]
    totals = SizeValues(
        bytes=sum(item.size_estimate.selected.bytes for item in selected),
        characters=sum(item.size_estimate.selected.characters for item in selected),
        lines=sum(item.size_estimate.selected.lines for item in selected),
        approximate_tokens=sum(
            item.size_estimate.selected.approximate_tokens for item in selected
        ),
    )
    status = _budget_status(selected, totals.approximate_tokens, configuration)
    budget = ContextBudget(
        configuration=configuration,
        result=BudgetResult(
            selected_entry_count=len(selected),
            selected_bytes=totals.bytes,
            selected_characters=totals.characters,
            selected_lines=totals.lines,
            selected_approximate_tokens=totals.approximate_tokens,
            status=status,
        ),
    )

    findings: list[ContextFinding] = []
    for entry in selected:
        estimate = entry.size_estimate.selected.approximate_tokens
        if estimate >= configuration.per_file_high_risk_approximate_tokens:
            findings.append(
                _file_budget_finding(
                    entry,
                    estimate,
                    configuration.per_file_high_risk_approximate_tokens,
                    high_risk=True,
                )
            )
        elif estimate >= configuration.per_file_warning_approximate_tokens:
            findings.append(
                _file_budget_finding(
                    entry,
                    estimate,
                    configuration.per_file_warning_approximate_tokens,
                    high_risk=False,
                )
            )

    total_estimate = totals.approximate_tokens
    if total_estimate >= configuration.total_high_risk_approximate_tokens:
        findings.append(
            _total_budget_finding(
                total_estimate,
                configuration.total_high_risk_approximate_tokens,
                high_risk=True,
            )
        )
    elif total_estimate >= configuration.total_warning_approximate_tokens:
        findings.append(
            _total_budget_finding(
                total_estimate,
                configuration.total_warning_approximate_tokens,
                high_risk=False,
            )
        )
    elif status == BudgetStatus.WITHIN_BUDGET:
        findings.append(
            ContextFinding(
                code="context_within_budget",
                level=FindingLevel.INFO,
                path=None,
                message=(
                    "Total approximate selected-context estimate is below the advisory "
                    "model-agnostic warning threshold."
                ),
            )
        )
    _validate_budget_consistency(entries, budget)
    return budget, tuple(findings)


def _budget_status(
    selected: Iterable[ContextEntry],
    total_approximate_tokens: int,
    configuration: BudgetConfiguration,
) -> BudgetStatus:
    estimates = [item.size_estimate.selected.approximate_tokens for item in selected]
    if (
        any(value >= configuration.per_file_high_risk_approximate_tokens for value in estimates)
        or total_approximate_tokens >= configuration.total_high_risk_approximate_tokens
    ):
        return BudgetStatus.HIGH_RISK
    if (
        any(value >= configuration.per_file_warning_approximate_tokens for value in estimates)
        or total_approximate_tokens >= configuration.total_warning_approximate_tokens
    ):
        return BudgetStatus.WARNING
    return BudgetStatus.WITHIN_BUDGET


def _file_budget_finding(
    entry: ContextEntry,
    estimate: int,
    threshold: int,
    *,
    high_risk: bool,
) -> ContextFinding:
    label = "high-risk" if high_risk else "warning"
    return ContextFinding(
        code=("context_file_budget_high_risk" if high_risk else "context_file_budget_warning"),
        level=FindingLevel.WARNING,
        path=entry.path,
        message=(
            f"Context entry {entry.entry_id} has an approximate selected-token estimate of "
            f"{estimate}, reaching the advisory model-agnostic {label} threshold of {threshold}."
        ),
    )


def _total_budget_finding(
    estimate: int,
    threshold: int,
    *,
    high_risk: bool,
) -> ContextFinding:
    label = "high-risk" if high_risk else "warning"
    return ContextFinding(
        code=("context_total_budget_high_risk" if high_risk else "context_total_budget_warning"),
        level=FindingLevel.WARNING,
        path=None,
        message=(
            f"Total approximate selected-token estimate is {estimate}, reaching the advisory "
            f"model-agnostic {label} threshold of {threshold}."
        ),
    )


def _validate_selection_result(result: ContextSelectionResult) -> None:
    if not isinstance(result, ContextSelectionResult):
        raise ContextValidationError("selection result must use ContextSelectionResult")
    _validate_task_slug(result.task_slug)

    collection_contracts = (
        ("registry", result.registry, ContextRegistryEntry),
        ("artifacts", result.artifacts, InspectedContextArtifact),
        ("selected_entries", result.selected_entries, SelectedContextEntry),
        ("context_entries", result.context_entries, ContextEntry),
        ("findings", result.findings, ContextFinding),
    )
    for field_name, items, item_type in collection_contracts:
        if not isinstance(items, tuple):
            raise ContextValidationError(f"selection result {field_name} must be a tuple")
        if any(not isinstance(item, item_type) for item in items):
            raise ContextValidationError(
                f"selection result {field_name} items must use {item_type.__name__}"
            )

    validated_findings = tuple(_validate_finding(item) for item in result.findings)
    if validated_findings != tuple(sorted(validated_findings, key=_finding_sort_key)):
        raise ContextValidationError(
            "selection result findings must be in deterministic order"
        )

    canonical_registry = validate_registry(result.registry)
    if result.registry != canonical_registry:
        raise ContextValidationError("selection result registry must be in canonical registry order")

    registry_ids = tuple(item.entry_id for item in canonical_registry)
    artifact_ids = tuple(item.entry_id for item in result.artifacts)
    context_ids = tuple(item.entry_id for item in result.context_entries)
    if artifact_ids != registry_ids:
        raise ContextValidationError(
            "selection result must contain one inspection artifact per registry entry in order"
        )
    if context_ids != registry_ids:
        raise ContextValidationError(
            "selection result must contain one measured context entry per registry entry in order"
        )

    registry_by_id = {item.entry_id: item for item in canonical_registry}
    artifacts_by_id: dict[str, InspectedContextArtifact] = {}
    context_by_id: dict[str, ContextEntry] = {}
    for registered, artifact, context_entry in zip(
        canonical_registry,
        result.artifacts,
        result.context_entries,
    ):
        _validate_registry_policy_match(artifact, registered, "inspection artifact")
        _validate_registry_policy_match(context_entry, registered, "context entry")
        _validate_size_values(artifact.source_size, f"{artifact.entry_id}.source_size")
        if artifact.path is None:
            if artifact.source_sha256 is not None:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} synthetic artifact must have null source_sha256"
                )
            expected_source_size = (
                ZERO_SIZE
                if artifact.source_content is None
                else measure_text(artifact.source_content)
            )
            if artifact.source_size != expected_source_size:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} synthetic source-content measurement mismatch"
                )
        elif _file_inspection_requires_read(artifact):
            if artifact.source_content is None:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} file-backed read artifact requires source_content"
                )
            if artifact.source_sha256 is None:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} file-backed read artifact requires source_sha256"
                )
            _validate_sha256(
                artifact.source_sha256,
                f"{artifact.entry_id}.source_sha256",
            )
            source_bytes = artifact.source_content.encode("utf-8")
            expected_source_size = _measure_source(
                source_bytes,
                artifact.source_content,
            )
            if artifact.source_size != expected_source_size:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} source-content measurement mismatch"
                )
            if artifact.source_sha256 != sha256_bytes(source_bytes):
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} readable artifact source_sha256 mismatch"
                )
        else:
            if artifact.source_content is not None:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} non-read file-backed artifact must have null source_content"
                )
            if artifact.source_sha256 is not None:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} non-read file-backed artifact must have null source_sha256"
                )
            if artifact.source_size != ZERO_SIZE:
                raise ContextValidationError(
                    f"selection result {artifact.entry_id} non-read file-backed artifact must have zero source_size"
                )
        _validate_context_entry(
            context_entry,
            registry_by_id=registry_by_id,
            artifact_path=context_manifest_path(result.task_slug),
            generated_paths=set(),
        )
        if artifact.existence != context_entry.existence:
            raise ContextValidationError(
                f"selection result {artifact.entry_id} artifact/context existence mismatch"
            )
        if artifact.freshness_state != context_entry.freshness_state:
            raise ContextValidationError(
                f"selection result {artifact.entry_id} artifact/context freshness mismatch"
            )
        if artifact.source_size != context_entry.size_estimate.source:
            raise ContextValidationError(
                f"selection result {artifact.entry_id} artifact/context source-size mismatch"
            )
        artifacts_by_id[artifact.entry_id] = artifact
        context_by_id[context_entry.entry_id] = context_entry

    for selected in result.selected_entries:
        _validate_entry_id(selected.entry_id, "selected entry ID")
        registered = registry_by_id.get(selected.entry_id)
        if registered is None:
            raise ContextValidationError(
                f"selection result selected entry is not registered: {selected.entry_id}"
            )
        _validate_registry_policy_match(selected, registered, "selected entry")

    selected_ids = tuple(item.entry_id for item in result.selected_entries)
    if len(selected_ids) != len(set(selected_ids)):
        raise ContextValidationError("selection result selected_entries must have unique entry IDs")
    expected_selected_ids = tuple(
        item.entry_id
        for item in result.context_entries
        if item.size_estimate.selected != ZERO_SIZE
    )
    if set(selected_ids) != set(expected_selected_ids):
        raise ContextValidationError(
            "selection result selected_entries must correspond exactly to nonzero selected measurements"
        )
    if tuple(result.selected_entries) != tuple(sorted(result.selected_entries, key=_selection_sort_key)):
        raise ContextValidationError("selection result selected_entries must be in deterministic order")

    _validate_selected_entries(result.selected_entries, result.task_slug)
    for selected in result.selected_entries:
        registered = registry_by_id.get(selected.entry_id)
        artifact = artifacts_by_id.get(selected.entry_id)
        context_entry = context_by_id.get(selected.entry_id)
        if registered is None or artifact is None or context_entry is None:
            raise ContextValidationError(
                f"selection result selected entry is not registered: {selected.entry_id}"
            )
        if selected.freshness_state != artifact.freshness_state:
            raise ContextValidationError(
                f"selection result {selected.entry_id} selected/artifact freshness mismatch"
            )
        if selected.source_content != artifact.source_content:
            raise ContextValidationError(
                f"selection result {selected.entry_id} selected/artifact source-content mismatch"
            )
        if artifact.inclusion_mode == InclusionMode.EXCLUDED:
            raise ContextValidationError(
                f"excluded entry cannot appear in selected_entries: {selected.entry_id}"
            )
        if artifact.existence not in {ExistenceState.PRESENT, ExistenceState.NOT_APPLICABLE}:
            raise ContextValidationError(
                f"missing or unreadable entry cannot appear in selected_entries: {selected.entry_id}"
            )
        if artifact.source_content is None:
            raise ContextValidationError(
                f"entry without selected source content cannot appear in selected_entries: {selected.entry_id}"
            )
        if artifact.freshness_state in {
            FreshnessState.STALE,
            FreshnessState.UNKNOWN,
            FreshnessState.MISSING,
        }:
            raise ContextValidationError(
                f"non-fresh entry cannot appear in selected_entries: {selected.entry_id}"
            )
        if (
            artifact.classification == Classification.DERIVED
            and artifact.freshness_state != FreshnessState.FRESH
        ):
            raise ContextValidationError(
                f"non-fresh derived entry cannot appear in selected_entries: {selected.entry_id}"
            )
        measured_selected = _measure_text(selected.selected_content)
        if measured_selected != context_entry.size_estimate.selected:
            raise ContextValidationError(
                f"selection result {selected.entry_id} selected-content measurement mismatch"
            )

    _validate_repository_observations(result.repository_observations)
    _validate_budget_consistency(result.context_entries, result.budget)


def _file_inspection_requires_read(artifact: InspectedContextArtifact) -> bool:
    """Return whether canonical selection policy requires this file to be read."""

    return (
        artifact.path is not None
        and artifact.existence == ExistenceState.PRESENT
        and artifact.inclusion_mode != InclusionMode.EXCLUDED
        and (
            artifact.classification != Classification.DERIVED
            or artifact.freshness_state == FreshnessState.FRESH
        )
    )


def _validate_budget_consistency(
    entries: tuple[ContextEntry, ...],
    budget: ContextBudget,
) -> None:
    _validate_budget(budget)
    selected = [item for item in entries if item.size_estimate.selected != ZERO_SIZE]
    expected = {
        "selected_entry_count": len(selected),
        "selected_bytes": sum(item.size_estimate.selected.bytes for item in selected),
        "selected_characters": sum(
            item.size_estimate.selected.characters for item in selected
        ),
        "selected_lines": sum(item.size_estimate.selected.lines for item in selected),
        "selected_approximate_tokens": sum(
            item.size_estimate.selected.approximate_tokens for item in selected
        ),
    }
    for field_name, expected_value in expected.items():
        if getattr(budget.result, field_name) != expected_value:
            raise ContextValidationError(
                f"budget result {field_name} does not equal measured selected context entries"
            )
    expected_status = _budget_status(
        selected,
        expected["selected_approximate_tokens"],
        budget.configuration,
    )
    if budget.result.status != expected_status:
        raise ContextValidationError(
            "budget result status does not match measured selected context entries and thresholds"
        )


def validate_context_manifest(document: ContextManifest) -> ContextManifest:
    """Validate a context manifest and return its canonical ordered form."""

    if not isinstance(document, ContextManifest):
        raise ContextValidationError("context manifest must use the ContextManifest model")
    if document.schema_version != SCHEMA_VERSION:
        raise ContextValidationError(f"schema_version must be {SCHEMA_VERSION}")
    _validate_task_slug(document.task_slug)
    expected_artifact_path = context_manifest_path(document.task_slug)
    _validate_repo_path(document.artifact_path, "artifact_path")
    if document.artifact_path != expected_artifact_path:
        raise ContextValidationError(f"artifact_path must be {expected_artifact_path}")
    if document.artifact_role != ARTIFACT_ROLE:
        raise ContextValidationError(f"artifact_role must be {ARTIFACT_ROLE}")
    if document.authority != AUTHORITY:
        raise ContextValidationError(f"authority must be {AUTHORITY}")
    if document.edit_model != EDIT_MODEL:
        raise ContextValidationError(f"edit_model must be {EDIT_MODEL}")

    registry = build_context_registry(document.task_slug)
    registry_by_id = {entry.entry_id: entry for entry in registry}
    registry_order = {entry.path: entry.order for entry in registry if entry.path is not None}

    source_artifacts = tuple(
        sorted(
            (_validate_source_artifact(item, registry_by_id) for item in document.source_artifacts),
            key=lambda item: (registry_order.get(item.path, 1_000_000), item.path),
        )
    )
    _validate_unique_paths((item.path for item in source_artifacts), "source_artifacts")
    expected_source_paths = tuple(
        entry.path for entry in registry if entry.classification == Classification.SOURCE
    )
    actual_source_paths = tuple(item.path for item in source_artifacts)
    if actual_source_paths != expected_source_paths:
        raise ContextValidationError(
            "source_artifacts must contain exactly the seven registered task sources"
        )

    if len(document.generated_artifacts) != 1:
        raise ContextValidationError(
            "generated_artifacts must contain exactly the generated agent workset"
        )
    generated_artifacts = tuple(
        sorted(
            (_validate_generated_artifact(item, document) for item in document.generated_artifacts),
            key=lambda item: item.path,
        )
    )
    _validate_unique_paths((item.path for item in generated_artifacts), "generated_artifacts")
    generated_paths = {item.path for item in generated_artifacts}

    _validate_unique_entry_ids(document.context_entries)
    context_entries = tuple(
        sorted(
            (
                _validate_context_entry(
                    item,
                    registry_by_id=registry_by_id,
                    artifact_path=document.artifact_path,
                    generated_paths=generated_paths,
                )
                for item in document.context_entries
            ),
            key=lambda item: (item.order, item.entry_id),
        )
    )
    _validate_unique_entry_ids(context_entries)
    _validate_unique_paths(
        (item.path for item in context_entries if item.path is not None),
        "context_entries",
    )
    expected_context_ids = tuple(entry.entry_id for entry in registry)
    actual_context_ids = tuple(entry.entry_id for entry in context_entries)
    if actual_context_ids != expected_context_ids:
        raise ContextValidationError(
            "context_entries must contain exactly one entry for every default registry entry"
        )

    workset_context_entry = next(
        item for item in context_entries if item.entry_id == "agent_workset_output"
    )
    if workset_context_entry.existence != ExistenceState.PRESENT:
        raise ContextValidationError(
            "generated workset/context-entry existence contradiction: "
            "agent_workset_output must be present"
        )

    context_by_path = {item.path: item for item in context_entries if item.path is not None}
    for source in source_artifacts:
        context_entry = context_by_path[source.path]
        if source.authority_level != context_entry.authority_level:
            raise ContextValidationError(
                f"source artifact authority conflicts with context entry: {source.path}"
            )
        if source.existence != context_entry.existence:
            raise ContextValidationError(
                f"source artifact existence conflicts with context entry: {source.path}"
            )

    findings = tuple(
        sorted(
            (_validate_finding(item) for item in document.findings),
            key=_finding_sort_key,
        )
    )
    budget = _validate_budget(document.budget)
    _validate_budget_consistency(context_entries, budget)
    return ContextManifest(
        schema_version=document.schema_version,
        artifact_path=document.artifact_path,
        artifact_role=document.artifact_role,
        authority=document.authority,
        edit_model=document.edit_model,
        task_slug=document.task_slug,
        source_artifacts=source_artifacts,
        generated_artifacts=generated_artifacts,
        context_entries=context_entries,
        findings=findings,
        budget=budget,
    )


def render_context_manifest_yaml(document: ContextManifest) -> str:
    """Render a validated context manifest with stable safe-YAML ordering."""

    canonical = validate_context_manifest(document)
    rendered = yaml.safe_dump(
        _manifest_to_data(canonical),
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    )
    return rendered.rstrip("\n") + "\n"


def parse_context_manifest_yaml(text: str) -> ContextManifest:
    """Parse safe YAML into a validated context-manifest model."""

    if not isinstance(text, str):
        raise ContextValidationError("context manifest YAML must be text")
    try:
        data = yaml.load(text, Loader=_StrictSafeLoader)
    except _ContextYamlStructureError as exc:
        raise ContextValidationError(f"context manifest YAML is invalid: {exc}") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = ""
        if mark is not None:
            location = f" at line {mark.line + 1}, column {mark.column + 1}"
        raise ContextValidationError(f"context manifest YAML is invalid{location}") from None
    if not isinstance(data, Mapping):
        raise ContextValidationError("context manifest must be a mapping")
    _require_keys(
        data,
        {
            "schema_version",
            "artifact_path",
            "artifact_role",
            "authority",
            "edit_model",
            "task_slug",
            "source_artifacts",
            "generated_artifacts",
            "context_entries",
            "findings",
            "budget",
        },
        "context manifest",
    )
    document = ContextManifest(
        schema_version=_integer(data["schema_version"], "schema_version"),
        artifact_path=_string(data["artifact_path"], "artifact_path"),
        artifact_role=_string(data["artifact_role"], "artifact_role"),
        authority=_string(data["authority"], "authority"),
        edit_model=_string(data["edit_model"], "edit_model"),
        task_slug=_string(data["task_slug"], "task_slug"),
        source_artifacts=tuple(
            _parse_source_artifact(item, index)
            for index, item in enumerate(_list(data["source_artifacts"], "source_artifacts"))
        ),
        generated_artifacts=tuple(
            _parse_generated_artifact(item, index)
            for index, item in enumerate(_list(data["generated_artifacts"], "generated_artifacts"))
        ),
        context_entries=tuple(
            _parse_context_entry(item, index)
            for index, item in enumerate(_list(data["context_entries"], "context_entries"))
        ),
        findings=tuple(
            _parse_finding(item, index)
            for index, item in enumerate(_list(data["findings"], "findings"))
        ),
        budget=_parse_budget(data["budget"]),
    )
    return validate_context_manifest(document)


def _validate_task_slug(task_slug: str) -> None:
    if not isinstance(task_slug, str) or TASK_SLUG_RE.fullmatch(task_slug) is None:
        raise ContextValidationError("task_slug must be a safe task slug")


def _validate_repo_path(path: str, label: str) -> str:
    if not isinstance(path, str) or not path:
        raise ContextValidationError(f"{label} must be a non-empty repository-relative POSIX path")
    if "\x00" in path:
        raise ContextValidationError(f"{label} contains an unsafe NUL character")
    if "\\" in path:
        raise ContextValidationError(f"{label} must use POSIX separators, not backslashes: {path}")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or path.startswith("//"):
        raise ContextValidationError(f"{label} must be repository-relative: {path}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ContextValidationError(f"{label} contains an unsafe path segment: {path}")
    if posix.as_posix() != path:
        raise ContextValidationError(f"{label} must be a normalized POSIX path: {path}")
    return path


def _validate_registry_entry(entry: ContextRegistryEntry) -> None:
    if not isinstance(entry, ContextRegistryEntry):
        raise ContextValidationError("registry entries must use ContextRegistryEntry")
    _validate_entry_id(entry.entry_id, "registry entry_id")
    _require_enum(entry.classification, Classification, f"registry {entry.entry_id}.classification")
    _require_enum(entry.authority_level, AuthorityLevel, f"registry {entry.entry_id}.authority_level")
    _require_enum(entry.inclusion_mode, InclusionMode, f"registry {entry.entry_id}.inclusion_mode")
    compatible_authorities = {
        Classification.SOURCE: {AuthorityLevel.TASK_SOURCE},
        Classification.CONFIGURATION: {AuthorityLevel.HARNESS_CONFIGURATION},
        Classification.REPOSITORY_SIGNAL: {AuthorityLevel.REPOSITORY_OBSERVATION},
        Classification.DERIVED: {
            AuthorityLevel.DERIVED_PROJECTION,
            AuthorityLevel.ADVISORY_REPORT,
            AuthorityLevel.HANDOFF_OUTPUT,
            AuthorityLevel.POST_IMPLEMENTATION_REPORT,
        },
    }
    if entry.authority_level not in compatible_authorities[entry.classification]:
        raise ContextValidationError(
            f"registry {entry.entry_id} authority_level {entry.authority_level.value} "
            f"is incompatible with classification {entry.classification.value}"
        )
    _non_negative_integer(entry.order, f"registry {entry.entry_id}.order")
    if entry.synthetic:
        if entry.path is not None:
            raise ContextValidationError(f"synthetic registry entry {entry.entry_id} must have path null")
        if entry.classification != Classification.REPOSITORY_SIGNAL:
            raise ContextValidationError(
                f"synthetic registry entry {entry.entry_id} must use repository_signal classification"
            )
    else:
        if entry.path is None:
            raise ContextValidationError(f"non-synthetic registry entry {entry.entry_id} requires a path")
        _validate_repo_path(entry.path, f"registry {entry.entry_id}.path")
    _validate_exclusion(entry.inclusion_mode, entry.exclusion_reason, f"registry {entry.entry_id}")
    if entry.path is not None and _is_task_generated_path(entry.path) and entry.inclusion_mode != InclusionMode.EXCLUDED:
        raise ContextValidationError(f"generated task output cannot be selected as an input: {entry.path}")
    if entry.authority_level in {AuthorityLevel.HANDOFF_OUTPUT, AuthorityLevel.POST_IMPLEMENTATION_REPORT} and entry.inclusion_mode != InclusionMode.EXCLUDED:
        raise ContextValidationError(
            f"{entry.authority_level.value} registry entry must be excluded: {entry.entry_id}"
        )
    if not isinstance(entry.dependencies, tuple) or any(not isinstance(value, str) or not value for value in entry.dependencies):
        raise ContextValidationError(f"registry {entry.entry_id}.dependencies must be non-empty entry_id strings")


def _validate_acyclic_registry(by_id: Mapping[str, ContextRegistryEntry]) -> None:
    state: dict[str, int] = {}

    def visit(entry_id: str, trail: tuple[str, ...]) -> None:
        marker = state.get(entry_id, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle = " -> ".join((*trail, entry_id))
            raise ContextValidationError(f"context registry lineage cycle detected: {cycle}")
        state[entry_id] = 1
        for dependency in by_id[entry_id].dependencies:
            visit(dependency, (*trail, entry_id))
        state[entry_id] = 2

    for entry_id in sorted(by_id):
        visit(entry_id, ())


def _validate_source_artifact(
    artifact: SourceArtifact,
    registry_by_id: Mapping[str, ContextRegistryEntry],
) -> SourceArtifact:
    if not isinstance(artifact, SourceArtifact):
        raise ContextValidationError("source_artifacts items must use SourceArtifact")
    _validate_repo_path(artifact.path, "source artifact path")
    _require_enum(artifact.authority_level, AuthorityLevel, "source artifact authority_level")
    _require_enum(artifact.existence, ExistenceState, "source artifact existence")
    if artifact.authority_level != AuthorityLevel.TASK_SOURCE:
        raise ContextValidationError("source artifact authority_level must be task_source")
    registered = [entry for entry in registry_by_id.values() if entry.path == artifact.path]
    if not registered or registered[0].classification != Classification.SOURCE:
        raise ContextValidationError(f"source artifact path is not registered as a task source: {artifact.path}")
    if artifact.existence == ExistenceState.PRESENT:
        _validate_sha256(artifact.sha256, "source artifact sha256")
    elif artifact.existence in {ExistenceState.MISSING, ExistenceState.UNREADABLE}:
        if artifact.sha256 is not None:
            raise ContextValidationError(
                f"source artifact sha256 must be null when existence is {artifact.existence.value}"
            )
    else:
        raise ContextValidationError("source artifact existence must be present, missing, or unreadable")
    return artifact


def _validate_generated_artifact(artifact: GeneratedArtifact, document: ContextManifest) -> GeneratedArtifact:
    if not isinstance(artifact, GeneratedArtifact):
        raise ContextValidationError("generated_artifacts items must use GeneratedArtifact")
    _validate_repo_path(artifact.path, "generated artifact path")
    if artifact.path == document.artifact_path:
        raise ContextValidationError("context manifest must not list itself in generated_artifacts")
    expected_path = agent_workset_path(document.task_slug)
    if artifact.path != expected_path:
        raise ContextValidationError(f"generated artifact path must be {expected_path}")
    if artifact.artifact_role != WORKSET_ARTIFACT_ROLE:
        raise ContextValidationError(f"generated artifact_role must be {WORKSET_ARTIFACT_ROLE}")
    _require_enum(artifact.authority_level, AuthorityLevel, "generated artifact authority_level")
    if artifact.authority_level != AuthorityLevel.HANDOFF_OUTPUT:
        raise ContextValidationError("generated artifact authority_level must be handoff_output")
    _validate_sha256(artifact.sha256, "generated artifact sha256")
    return artifact


def _validate_context_entry(
    entry: ContextEntry,
    *,
    registry_by_id: Mapping[str, ContextRegistryEntry],
    artifact_path: str,
    generated_paths: set[str],
) -> ContextEntry:
    if not isinstance(entry, ContextEntry):
        raise ContextValidationError("context_entries items must use ContextEntry")
    _validate_entry_id(entry.entry_id, "context entry_id")
    _require_enum(entry.classification, Classification, f"{entry.entry_id}.classification")
    _require_enum(entry.authority_level, AuthorityLevel, f"{entry.entry_id}.authority_level")
    _require_enum(entry.inclusion_mode, InclusionMode, f"{entry.entry_id}.inclusion_mode")
    _require_enum(entry.existence, ExistenceState, f"{entry.entry_id}.existence")
    _require_enum(entry.freshness_state, FreshnessState, f"{entry.entry_id}.freshness_state")
    _non_negative_integer(entry.order, f"{entry.entry_id}.order")
    _validate_exclusion(entry.inclusion_mode, entry.exclusion_reason, f"context entry {entry.entry_id}")

    registered = registry_by_id.get(entry.entry_id)
    if registered is None:
        raise ContextValidationError(f"context entry is not in the explicit registry: {entry.entry_id}")
    if registered.synthetic:
        if entry.path is not None:
            raise ContextValidationError(f"synthetic context entry {entry.entry_id} must have path null")
    else:
        if entry.path is None:
            raise ContextValidationError(f"non-synthetic context entry {entry.entry_id} requires a path")
        _validate_repo_path(entry.path, f"{entry.entry_id}.path")
    if entry.path != registered.path:
        raise ContextValidationError(f"context entry {entry.entry_id} path conflicts with the registry")
    if entry.classification != registered.classification:
        raise ContextValidationError(f"context entry {entry.entry_id} classification conflicts with the registry")
    if entry.authority_level != registered.authority_level:
        raise ContextValidationError(f"context entry {entry.entry_id} authority_level conflicts with the registry")
    if entry.inclusion_mode != registered.inclusion_mode:
        raise ContextValidationError(f"context entry {entry.entry_id} inclusion_mode conflicts with the registry")
    if entry.order != registered.order:
        raise ContextValidationError(f"context entry {entry.entry_id} order conflicts with the registry")
    if entry.exclusion_reason != registered.exclusion_reason:
        raise ContextValidationError(f"context entry {entry.entry_id} exclusion_reason conflicts with the registry")

    _validate_size_estimate(entry.size_estimate, entry.entry_id)
    if entry.inclusion_mode == InclusionMode.EXCLUDED:
        if entry.size_estimate.selected != ZERO_SIZE:
            raise ContextValidationError(f"excluded context entry {entry.entry_id} must have zero selected size")
    elif entry.path in generated_paths or entry.path == artifact_path or (
        entry.path is not None and _is_task_generated_path(entry.path)
    ):
        raise ContextValidationError(f"generated output cannot be selected as an input: {entry.path}")
    if entry.authority_level in {AuthorityLevel.HANDOFF_OUTPUT, AuthorityLevel.POST_IMPLEMENTATION_REPORT} and entry.inclusion_mode != InclusionMode.EXCLUDED:
        raise ContextValidationError(f"{entry.authority_level.value} context entry must be excluded: {entry.entry_id}")
    return entry


def _validate_registry_policy_match(
    value: InspectedContextArtifact | SelectedContextEntry | ContextEntry,
    registered: ContextRegistryEntry,
    label: str,
) -> None:
    for field_name in (
        "entry_id",
        "path",
        "classification",
        "authority_level",
        "inclusion_mode",
        "order",
    ):
        if getattr(value, field_name) != getattr(registered, field_name):
            raise ContextValidationError(
                f"selection result {label} {registered.entry_id} {field_name} conflicts with the registry"
            )
    if isinstance(value, ContextEntry) and value.exclusion_reason != registered.exclusion_reason:
        raise ContextValidationError(
            f"selection result context entry {registered.entry_id} exclusion_reason conflicts with the registry"
        )


def _validate_size_estimate(size: SizeEstimate, label: str) -> None:
    if not isinstance(size, SizeEstimate):
        raise ContextValidationError(f"{label}.size_estimate must use SizeEstimate")
    _validate_size_values(size.source, f"{label}.size_estimate.source")
    _validate_size_values(size.selected, f"{label}.size_estimate.selected")


def _validate_size_values(size: SizeValues, label: str) -> None:
    if not isinstance(size, SizeValues):
        raise ContextValidationError(f"{label} must use SizeValues")
    for field_name in ("bytes", "characters", "lines", "approximate_tokens"):
        _non_negative_integer(getattr(size, field_name), f"{label}.{field_name}")
    expected_tokens = _approximate_tokens(size.characters)
    if size.approximate_tokens != expected_tokens:
        raise ContextValidationError(
            f"{label}.approximate_tokens must equal ceil({label}.characters / 4); "
            f"expected {expected_tokens}"
        )


def _validate_finding(finding: ContextFinding) -> ContextFinding:
    if not isinstance(finding, ContextFinding):
        raise ContextValidationError("findings items must use ContextFinding")
    if not isinstance(finding.code, str) or FINDING_CODE_RE.fullmatch(finding.code) is None:
        raise ContextValidationError("finding code must be a non-empty snake_case identifier")
    _require_enum(finding.level, FindingLevel, f"finding {finding.code}.level")
    if finding.path is not None:
        _validate_repo_path(finding.path, f"finding {finding.code}.path")
    if not isinstance(finding.message, str) or not finding.message.strip():
        raise ContextValidationError(f"finding {finding.code}.message must be non-empty")
    return finding


def _validate_budget(budget: ContextBudget) -> ContextBudget:
    if not isinstance(budget, ContextBudget):
        raise ContextValidationError("budget must use ContextBudget")
    configuration = budget.configuration
    result = budget.result
    _validate_budget_configuration(configuration)
    if not isinstance(result, BudgetResult):
        raise ContextValidationError("budget.result must use BudgetResult")
    for field_name in (
        "selected_entry_count",
        "selected_bytes",
        "selected_characters",
        "selected_lines",
        "selected_approximate_tokens",
    ):
        _non_negative_integer(getattr(result, field_name), f"budget.result.{field_name}")
    _require_enum(result.status, BudgetStatus, "budget.result.status")
    return budget


def _validate_budget_configuration(
    configuration: BudgetConfiguration,
) -> BudgetConfiguration:
    if not isinstance(configuration, BudgetConfiguration):
        raise ContextValidationError("budget.configuration must use BudgetConfiguration")
    for field_name in _CONTEXT_BUDGET_FIELDS:
        _non_negative_integer(getattr(configuration, field_name), f"budget.configuration.{field_name}")
    if configuration.per_file_high_risk_approximate_tokens <= configuration.per_file_warning_approximate_tokens:
        raise ContextValidationError("per-file high-risk threshold must exceed the warning threshold")
    if configuration.total_high_risk_approximate_tokens <= configuration.total_warning_approximate_tokens:
        raise ContextValidationError("total high-risk threshold must exceed the warning threshold")
    return configuration


def _validate_exclusion(mode: InclusionMode, reason: str | None, label: str) -> None:
    if mode == InclusionMode.EXCLUDED:
        if not isinstance(reason, str) or ENTRY_ID_RE.fullmatch(reason) is None:
            raise ContextValidationError(f"{label} requires a stable snake_case exclusion_reason")
    elif reason is not None:
        raise ContextValidationError(f"{label} must have exclusion_reason null when included")


def _validate_entry_id(entry_id: str, label: str) -> None:
    if not isinstance(entry_id, str) or ENTRY_ID_RE.fullmatch(entry_id) is None:
        raise ContextValidationError(f"{label} must be a non-empty snake_case identifier")


def _validate_sha256(value: str | None, label: str) -> None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ContextValidationError(f"{label} must be exactly 64 lowercase hexadecimal characters")


def _validate_unique_paths(paths: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            raise ContextValidationError(f"duplicate path in {label}: {path}")
        seen.add(path)


def _validate_unique_entry_ids(entries: Iterable[ContextEntry]) -> None:
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, ContextEntry):
            raise ContextValidationError("context_entries items must use ContextEntry")
        if entry.entry_id in seen:
            raise ContextValidationError(f"duplicate context entry_id: {entry.entry_id}")
        seen.add(entry.entry_id)


def _is_task_generated_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 5 and parts[0:2] == (".harness", "tasks") and parts[3] == "generated"


def _finding_sort_key(finding: ContextFinding) -> tuple[int, str, str, str]:
    severity = {
        FindingLevel.BLOCKER: 0,
        FindingLevel.WARNING: 1,
        FindingLevel.INFO: 2,
    }
    return (severity[finding.level], finding.code, finding.path or "", finding.message)


def _manifest_to_data(document: ContextManifest) -> dict[str, Any]:
    return {
        "schema_version": document.schema_version,
        "artifact_path": document.artifact_path,
        "artifact_role": document.artifact_role,
        "authority": document.authority,
        "edit_model": document.edit_model,
        "task_slug": document.task_slug,
        "source_artifacts": [
            {
                "path": item.path,
                "authority_level": item.authority_level.value,
                "existence": item.existence.value,
                "sha256": item.sha256,
            }
            for item in document.source_artifacts
        ],
        "generated_artifacts": [
            {
                "path": item.path,
                "artifact_role": item.artifact_role,
                "authority_level": item.authority_level.value,
                "sha256": item.sha256,
            }
            for item in document.generated_artifacts
        ],
        "context_entries": [
            {
                "entry_id": item.entry_id,
                "path": item.path,
                "classification": item.classification.value,
                "authority_level": item.authority_level.value,
                "inclusion_mode": item.inclusion_mode.value,
                "order": item.order,
                "existence": item.existence.value,
                "freshness_state": item.freshness_state.value,
                "size_estimate": {
                    "source": _size_to_data(item.size_estimate.source),
                    "selected": _size_to_data(item.size_estimate.selected),
                },
                "exclusion_reason": item.exclusion_reason,
            }
            for item in document.context_entries
        ],
        "findings": [
            {
                "code": item.code,
                "level": item.level.value,
                "path": item.path,
                "message": item.message,
            }
            for item in document.findings
        ],
        "budget": {
            "configuration": {
                "per_file_warning_approximate_tokens": document.budget.configuration.per_file_warning_approximate_tokens,
                "per_file_high_risk_approximate_tokens": document.budget.configuration.per_file_high_risk_approximate_tokens,
                "total_warning_approximate_tokens": document.budget.configuration.total_warning_approximate_tokens,
                "total_high_risk_approximate_tokens": document.budget.configuration.total_high_risk_approximate_tokens,
            },
            "result": {
                "selected_entry_count": document.budget.result.selected_entry_count,
                "selected_bytes": document.budget.result.selected_bytes,
                "selected_characters": document.budget.result.selected_characters,
                "selected_lines": document.budget.result.selected_lines,
                "selected_approximate_tokens": document.budget.result.selected_approximate_tokens,
                "status": document.budget.result.status.value,
            },
        },
    }


def _size_to_data(size: SizeValues) -> dict[str, int]:
    return {
        "bytes": size.bytes,
        "characters": size.characters,
        "lines": size.lines,
        "approximate_tokens": size.approximate_tokens,
    }


def _parse_source_artifact(value: Any, index: int) -> SourceArtifact:
    label = f"source_artifacts[{index}]"
    data = _mapping(value, label)
    _require_keys(data, {"path", "authority_level", "existence", "sha256"}, label)
    return SourceArtifact(
        path=_string(data["path"], f"{label}.path"),
        authority_level=_enum(data["authority_level"], AuthorityLevel, f"{label}.authority_level"),
        existence=_enum(data["existence"], ExistenceState, f"{label}.existence"),
        sha256=_nullable_string(data["sha256"], f"{label}.sha256"),
    )


def _parse_generated_artifact(value: Any, index: int) -> GeneratedArtifact:
    label = f"generated_artifacts[{index}]"
    data = _mapping(value, label)
    _require_keys(data, {"path", "artifact_role", "authority_level", "sha256"}, label)
    return GeneratedArtifact(
        path=_string(data["path"], f"{label}.path"),
        artifact_role=_string(data["artifact_role"], f"{label}.artifact_role"),
        authority_level=_enum(data["authority_level"], AuthorityLevel, f"{label}.authority_level"),
        sha256=_string(data["sha256"], f"{label}.sha256"),
    )


def _parse_context_entry(value: Any, index: int) -> ContextEntry:
    label = f"context_entries[{index}]"
    data = _mapping(value, label)
    _require_keys(
        data,
        {
            "entry_id",
            "path",
            "classification",
            "authority_level",
            "inclusion_mode",
            "order",
            "existence",
            "freshness_state",
            "size_estimate",
            "exclusion_reason",
        },
        label,
    )
    size_data = _mapping(data["size_estimate"], f"{label}.size_estimate")
    _require_keys(size_data, {"source", "selected"}, f"{label}.size_estimate")
    return ContextEntry(
        entry_id=_string(data["entry_id"], f"{label}.entry_id"),
        path=_nullable_string(data["path"], f"{label}.path"),
        classification=_enum(data["classification"], Classification, f"{label}.classification"),
        authority_level=_enum(data["authority_level"], AuthorityLevel, f"{label}.authority_level"),
        inclusion_mode=_enum(data["inclusion_mode"], InclusionMode, f"{label}.inclusion_mode"),
        order=_integer(data["order"], f"{label}.order"),
        existence=_enum(data["existence"], ExistenceState, f"{label}.existence"),
        freshness_state=_enum(data["freshness_state"], FreshnessState, f"{label}.freshness_state"),
        size_estimate=SizeEstimate(
            source=_parse_size_values(size_data["source"], f"{label}.size_estimate.source"),
            selected=_parse_size_values(size_data["selected"], f"{label}.size_estimate.selected"),
        ),
        exclusion_reason=_nullable_string(data["exclusion_reason"], f"{label}.exclusion_reason"),
    )


def _parse_size_values(value: Any, label: str) -> SizeValues:
    data = _mapping(value, label)
    _require_keys(data, {"bytes", "characters", "lines", "approximate_tokens"}, label)
    return SizeValues(
        bytes=_integer(data["bytes"], f"{label}.bytes"),
        characters=_integer(data["characters"], f"{label}.characters"),
        lines=_integer(data["lines"], f"{label}.lines"),
        approximate_tokens=_integer(data["approximate_tokens"], f"{label}.approximate_tokens"),
    )


def _parse_finding(value: Any, index: int) -> ContextFinding:
    label = f"findings[{index}]"
    data = _mapping(value, label)
    _require_keys(data, {"code", "level", "path", "message"}, label)
    return ContextFinding(
        code=_string(data["code"], f"{label}.code"),
        level=_enum(data["level"], FindingLevel, f"{label}.level"),
        path=_nullable_string(data["path"], f"{label}.path"),
        message=_string(data["message"], f"{label}.message"),
    )


def _parse_budget(value: Any) -> ContextBudget:
    data = _mapping(value, "budget")
    _require_keys(data, {"configuration", "result"}, "budget")
    configuration = _mapping(data["configuration"], "budget.configuration")
    _require_keys(
        configuration,
        {
            "per_file_warning_approximate_tokens",
            "per_file_high_risk_approximate_tokens",
            "total_warning_approximate_tokens",
            "total_high_risk_approximate_tokens",
        },
        "budget.configuration",
    )
    result = _mapping(data["result"], "budget.result")
    _require_keys(
        result,
        {
            "selected_entry_count",
            "selected_bytes",
            "selected_characters",
            "selected_lines",
            "selected_approximate_tokens",
            "status",
        },
        "budget.result",
    )
    return ContextBudget(
        configuration=BudgetConfiguration(
            per_file_warning_approximate_tokens=_integer(
                configuration["per_file_warning_approximate_tokens"],
                "budget.configuration.per_file_warning_approximate_tokens",
            ),
            per_file_high_risk_approximate_tokens=_integer(
                configuration["per_file_high_risk_approximate_tokens"],
                "budget.configuration.per_file_high_risk_approximate_tokens",
            ),
            total_warning_approximate_tokens=_integer(
                configuration["total_warning_approximate_tokens"],
                "budget.configuration.total_warning_approximate_tokens",
            ),
            total_high_risk_approximate_tokens=_integer(
                configuration["total_high_risk_approximate_tokens"],
                "budget.configuration.total_high_risk_approximate_tokens",
            ),
        ),
        result=BudgetResult(
            selected_entry_count=_integer(result["selected_entry_count"], "budget.result.selected_entry_count"),
            selected_bytes=_integer(result["selected_bytes"], "budget.result.selected_bytes"),
            selected_characters=_integer(result["selected_characters"], "budget.result.selected_characters"),
            selected_lines=_integer(result["selected_lines"], "budget.result.selected_lines"),
            selected_approximate_tokens=_integer(
                result["selected_approximate_tokens"],
                "budget.result.selected_approximate_tokens",
            ),
            status=_enum(result["status"], BudgetStatus, "budget.result.status"),
        ),
    )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextValidationError(f"{label} must be a mapping")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContextValidationError(f"{label} must be a list")
    return value


def _require_keys(data: Mapping[str, Any], required: set[str], label: str) -> None:
    for key in data:
        if not isinstance(key, str):
            raise ContextValidationError(
                f"{label} mapping keys must be strings; found {_safe_yaml_key_description(key)}"
            )
    keys = set(data)
    missing = sorted(required - keys)
    if missing:
        raise ContextValidationError(f"{label} is missing required field: {missing[0]}")
    unknown = sorted(keys - required)
    if unknown:
        raise ContextValidationError(f"{label} contains unknown field: {unknown[0]}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContextValidationError(f"{label} must be a non-empty string")
    return value


def _nullable_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContextValidationError(f"{label} must be an integer")
    return value


def _non_negative_integer(value: Any, label: str) -> int:
    result = _integer(value, label)
    if result < 0:
        raise ContextValidationError(f"{label} must be non-negative")
    return result


def _enum(value: Any, enum_type: type[Enum], label: str) -> Any:
    if not isinstance(value, str):
        raise ContextValidationError(f"{label} must be a string")
    try:
        return enum_type(value)
    except ValueError:
        allowed = ", ".join(item.value for item in enum_type)
        raise ContextValidationError(f"{label} must be one of: {allowed}") from None


def _require_enum(value: Any, enum_type: type[Enum], label: str) -> None:
    if not isinstance(value, enum_type):
        allowed = ", ".join(item.value for item in enum_type)
        raise ContextValidationError(f"{label} must be one of: {allowed}")
