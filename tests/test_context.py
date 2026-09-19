from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ai_sdlc_harness.context import (
    ARTIFACT_ROLE,
    AUTHORITY,
    EDIT_MODEL,
    SCHEMA_VERSION,
    AuthorityLevel,
    BudgetConfiguration,
    BudgetResult,
    BudgetStatus,
    Classification,
    ContextBudget,
    ContextEntry,
    ContextFinding,
    ContextManifest,
    ContextRegistryEntry,
    ContextSelectionResult,
    ContextValidationError,
    ExistenceState,
    FindingLevel,
    FreshnessState,
    GeneratedArtifact,
    InclusionMode,
    RepositoryObservations,
    SizeEstimate,
    SizeValues,
    SourceArtifact,
    _approximate_tokens,
    _logical_line_count,
    agent_workset_path,
    build_context_registry,
    context_manifest_path,
    parse_context_manifest_yaml,
    render_context_manifest_yaml,
    resolve_budget_configuration,
    select_context,
    validate_context_manifest,
    validate_registry,
)
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.files import sha256_bytes
from ai_sdlc_harness.lineage import (
    LineageResolutionResult,
    resolve_derived_freshness,
)
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.requirements import build_requirements_document, render_requirements_yaml
from ai_sdlc_harness.spec import run_spec
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.test_contract import run_test_contract_review


SLUG = "context-foundation"
SOURCE_HASH = "a" * 64
WORKSET_HASH = "b" * 64


def _values(value: int = 0) -> SizeValues:
    return SizeValues(
        bytes=value,
        characters=value,
        lines=value,
        approximate_tokens=_approximate_tokens(value) if value >= 0 else 0,
    )


def _size(source: int = 0, selected: int = 0) -> SizeEstimate:
    return SizeEstimate(source=_values(source), selected=_values(selected))


def _budget() -> ContextBudget:
    return ContextBudget(
        configuration=BudgetConfiguration(
            per_file_warning_approximate_tokens=4000,
            per_file_high_risk_approximate_tokens=8000,
            total_warning_approximate_tokens=12000,
            total_high_risk_approximate_tokens=24000,
        ),
        result=BudgetResult(
            selected_entry_count=0,
            selected_bytes=0,
            selected_characters=0,
            selected_lines=0,
            selected_approximate_tokens=0,
            status=BudgetStatus.WITHIN_BUDGET,
        ),
    )


def _task_entry() -> ContextEntry:
    return ContextEntry(
        entry_id="task",
        path=f".harness/tasks/{SLUG}/task.md",
        classification=Classification.SOURCE,
        authority_level=AuthorityLevel.TASK_SOURCE,
        inclusion_mode=InclusionMode.READ_FIRST,
        order=10,
        existence=ExistenceState.PRESENT,
        freshness_state=FreshnessState.NOT_APPLICABLE,
        size_estimate=_size(source=8, selected=4),
        exclusion_reason=None,
    )


def _repository_entry() -> ContextEntry:
    return ContextEntry(
        entry_id="repository_signals",
        path=None,
        classification=Classification.REPOSITORY_SIGNAL,
        authority_level=AuthorityLevel.REPOSITORY_OBSERVATION,
        inclusion_mode=InclusionMode.READ_IF_NEEDED,
        order=130,
        existence=ExistenceState.NOT_APPLICABLE,
        freshness_state=FreshnessState.NOT_APPLICABLE,
        size_estimate=_size(selected=2),
        exclusion_reason=None,
    )


def _workset_entry() -> ContextEntry:
    return ContextEntry(
        entry_id="agent_workset_output",
        path=agent_workset_path(SLUG),
        classification=Classification.DERIVED,
        authority_level=AuthorityLevel.HANDOFF_OUTPUT,
        inclusion_mode=InclusionMode.EXCLUDED,
        order=200,
        existence=ExistenceState.PRESENT,
        freshness_state=FreshnessState.FRESH,
        size_estimate=_size(source=10, selected=0),
        exclusion_reason="generated_output_not_input",
    )


def _complete_document() -> ContextManifest:
    registry = build_context_registry(SLUG)
    context_entries: list[ContextEntry] = []
    for registered in registry:
        if registered.classification == Classification.SOURCE:
            existence = ExistenceState.PRESENT
            freshness = FreshnessState.NOT_APPLICABLE
        elif registered.entry_id == "repository_signals":
            existence = ExistenceState.NOT_APPLICABLE
            freshness = FreshnessState.NOT_APPLICABLE
        elif registered.entry_id == "agent_workset_output":
            existence = ExistenceState.PRESENT
            freshness = FreshnessState.NOT_APPLICABLE
        else:
            existence = ExistenceState.MISSING
            freshness = (
                FreshnessState.MISSING
                if registered.classification == Classification.DERIVED
                else FreshnessState.NOT_APPLICABLE
            )
        context_entries.append(
            ContextEntry(
                entry_id=registered.entry_id,
                path=registered.path,
                classification=registered.classification,
                authority_level=registered.authority_level,
                inclusion_mode=registered.inclusion_mode,
                order=registered.order,
                existence=existence,
                freshness_state=freshness,
                size_estimate=_size(),
                exclusion_reason=registered.exclusion_reason,
            )
        )

    source_artifacts = tuple(
        SourceArtifact(
            path=registered.path,
            authority_level=AuthorityLevel.TASK_SOURCE,
            existence=ExistenceState.PRESENT,
            sha256=SOURCE_HASH,
        )
        for registered in registry
        if registered.classification == Classification.SOURCE
    )
    return ContextManifest(
        schema_version=SCHEMA_VERSION,
        artifact_path=context_manifest_path(SLUG),
        artifact_role=ARTIFACT_ROLE,
        authority=AUTHORITY,
        edit_model=EDIT_MODEL,
        task_slug=SLUG,
        source_artifacts=source_artifacts,
        generated_artifacts=(
            GeneratedArtifact(
                path=agent_workset_path(SLUG),
                artifact_role="agent_handoff",
                authority_level=AuthorityLevel.HANDOFF_OUTPUT,
                sha256=WORKSET_HASH,
            ),
        ),
        context_entries=tuple(context_entries),
        findings=(),
        budget=_budget(),
    )


def _realistic_document() -> ContextManifest:
    document = _complete_document()
    replacements = {
        "task": _task_entry(),
        "repository_signals": _repository_entry(),
        "agent_workset_output": _workset_entry(),
    }
    return replace(
        document,
        context_entries=tuple(
            replacements.get(entry.entry_id, entry) for entry in document.context_entries
        ),
        findings=(
            ContextFinding(
                code="within_budget",
                level=FindingLevel.INFO,
                path=None,
                message="Selected context is within the advisory budget.",
            ),
        ),
        budget=replace(
            _budget(),
            result=BudgetResult(
                selected_entry_count=2,
                selected_bytes=6,
                selected_characters=6,
                selected_lines=6,
                selected_approximate_tokens=2,
                status=BudgetStatus.WITHIN_BUDGET,
            ),
        ),
    )


def _registry_entry(
    entry_id: str,
    path: str | None,
    *,
    classification: Classification = Classification.SOURCE,
    authority: AuthorityLevel = AuthorityLevel.TASK_SOURCE,
    inclusion: InclusionMode = InclusionMode.READ_FIRST,
    order: int = 10,
    exclusion_reason: str | None = None,
    synthetic: bool = False,
    dependencies: tuple[str, ...] = (),
) -> ContextRegistryEntry:
    return ContextRegistryEntry(
        entry_id=entry_id,
        path=path,
        classification=classification,
        authority_level=authority,
        inclusion_mode=inclusion,
        order=order,
        exclusion_reason=exclusion_reason,
        synthetic=synthetic,
        dependencies=dependencies,
    )


def _rendered_data(document: ContextManifest | None = None) -> dict:
    return yaml.safe_load(render_context_manifest_yaml(document or _realistic_document()))


def _parse_data(data: dict) -> ContextManifest:
    return parse_context_manifest_yaml(yaml.safe_dump(data, sort_keys=False))


def _write_repository_file(root: Path, relative_path: str, content: str | bytes) -> Path:
    target = root.joinpath(*relative_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8", newline="")
    return target


def _selection_repository(tmp_path: Path, *, include_derived: bool = False) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    for entry in build_context_registry(SLUG):
        if entry.classification == Classification.SOURCE:
            _write_repository_file(
                root,
                entry.path,
                f"# {entry.entry_id.replace('_', ' ').title()}\n\n{entry.entry_id} context\n",
            )
    _write_repository_file(
        root,
        ".harness/packs/selected.yaml",
        "selected_packs:\n  - id: security-baseline\n    version: '1'\n    enabled: true\n",
    )
    if include_derived:
        _write_repository_file(
            root,
            f".harness/tasks/{SLUG}/spec.md",
            "# Specification\n\n- info: specification ready\n",
        )
        requirements = build_requirements_document(SLUG, "- Keep selection deterministic")
        _write_repository_file(
            root,
            f".harness/tasks/{SLUG}/requirements.yaml",
            render_requirements_yaml(requirements),
        )
        _write_repository_file(
            root,
            f".harness/tasks/{SLUG}/preflight.md",
            "# Preflight Report\n\n- warning: review boundary\n",
        )
        _write_repository_file(
            root,
            f".harness/tasks/{SLUG}/test-contract-review.md",
            "# Test-Contract Readiness Review\n\n- info: tests identified\n",
        )
    return root


def _fresh_derived() -> dict[str, FreshnessState]:
    return {
        "spec": FreshnessState.FRESH,
        "requirements_projection": FreshnessState.FRESH,
        "preflight": FreshnessState.FRESH,
        "test_contract_review": FreshnessState.FRESH,
    }


def _observations() -> RepositoryObservations:
    return RepositoryObservations(
        available=True,
        git_repo=True,
        detected_languages=("python",),
        detected_package_managers=("pip",),
        detected_test_frameworks=("pytest",),
        detected_ci=("github-actions",),
        existing_agent_files=("AGENTS.md",),
    )


def _selected(result: ContextSelectionResult, entry_id: str):
    return next(entry for entry in result.selected_entries if entry.entry_id == entry_id)


def _finding_codes(result: ContextSelectionResult) -> list[str]:
    return [finding.code for finding in result.findings]


def _context_entry(result: ContextSelectionResult, entry_id: str) -> ContextEntry:
    return next(entry for entry in result.context_entries if entry.entry_id == entry_id)


def _replace_document_entry(
    document: ContextManifest,
    entry_id: str,
    **changes,
) -> ContextManifest:
    return replace(
        document,
        context_entries=tuple(
            replace(entry, **changes) if entry.entry_id == entry_id else entry
            for entry in document.context_entries
        ),
    )


def _replace_result_item(items, entry_id: str, **changes):
    return tuple(
        replace(item, **changes) if item.entry_id == entry_id else item
        for item in items
    )


def _budget_configuration(
    *,
    per_file_warning: int = 4000,
    per_file_high_risk: int = 8000,
    total_warning: int = 12000,
    total_high_risk: int = 24000,
) -> BudgetConfiguration:
    return BudgetConfiguration(
        per_file_warning_approximate_tokens=per_file_warning,
        per_file_high_risk_approximate_tokens=per_file_high_risk,
        total_warning_approximate_tokens=total_warning,
        total_high_risk_approximate_tokens=total_high_risk,
    )


def _budget_configuration_data(**changes: object) -> dict[str, object]:
    configuration = _budget().configuration
    data = {
        "per_file_warning_approximate_tokens": (
            configuration.per_file_warning_approximate_tokens
        ),
        "per_file_high_risk_approximate_tokens": (
            configuration.per_file_high_risk_approximate_tokens
        ),
        "total_warning_approximate_tokens": configuration.total_warning_approximate_tokens,
        "total_high_risk_approximate_tokens": configuration.total_high_risk_approximate_tokens,
    }
    data.update(changes)
    return data


def _single_source_selection(
    tmp_path: Path,
    content: str | bytes,
    *,
    configuration: BudgetConfiguration | None = None,
) -> ContextSelectionResult:
    root = tmp_path / "repository"
    root.mkdir()
    path = ".harness/custom/input.md"
    _write_repository_file(root, path, content)
    return select_context(
        root,
        SLUG,
        derived_freshness={},
        registry=(_registry_entry("input", path),),
        budget_configuration=configuration,
    )


def test_complete_valid_document():
    assert validate_context_manifest(_complete_document()) == _complete_document()


def test_empty_partial_document_is_rejected():
    document = replace(
        _complete_document(),
        source_artifacts=(),
        generated_artifacts=(),
        context_entries=(),
    )

    with pytest.raises(ContextValidationError):
        validate_context_manifest(document)


def test_missing_context_entry_is_rejected():
    document = _complete_document()

    with pytest.raises(ContextValidationError, match="exactly one entry for every default registry entry"):
        validate_context_manifest(replace(document, context_entries=document.context_entries[:-1]))


def test_extra_context_entry_is_rejected():
    document = _complete_document()
    extra = replace(
        _task_entry(),
        entry_id="extra",
        path=f".harness/tasks/{SLUG}/extra.md",
    )

    with pytest.raises(ContextValidationError, match="not in the explicit registry"):
        validate_context_manifest(replace(document, context_entries=(*document.context_entries, extra)))


@pytest.mark.parametrize(
    ("entry_id", "changes", "message"),
    [
        ("task", {"inclusion_mode": InclusionMode.READ_IF_NEEDED}, "inclusion_mode conflicts"),
        ("task", {"classification": Classification.DERIVED}, "classification conflicts"),
        (
            "task",
            {"authority_level": AuthorityLevel.DERIVED_PROJECTION},
            "authority_level conflicts",
        ),
        ("task", {"order": 999}, "order conflicts"),
        (
            "agent_workset_output",
            {"exclusion_reason": "different_exclusion"},
            "exclusion_reason conflicts",
        ),
    ],
)
def test_context_entry_policy_must_match_registry(entry_id, changes, message):
    document = _replace_document_entry(_complete_document(), entry_id, **changes)

    with pytest.raises(ContextValidationError, match=message):
        validate_context_manifest(document)


def test_missing_source_artifact_is_rejected():
    document = _complete_document()

    with pytest.raises(ContextValidationError, match="exactly the seven registered task sources"):
        validate_context_manifest(replace(document, source_artifacts=document.source_artifacts[:-1]))


def test_extra_source_artifact_is_rejected():
    document = _complete_document()
    extra = replace(
        document.source_artifacts[0],
        path=".harness/packs/selected.yaml",
    )

    with pytest.raises(ContextValidationError, match="not registered as a task source"):
        validate_context_manifest(replace(document, source_artifacts=(*document.source_artifacts, extra)))


def test_source_and_context_existence_must_match():
    document = _complete_document()
    changed_source = replace(
        document.source_artifacts[0],
        existence=ExistenceState.MISSING,
        sha256=None,
    )

    with pytest.raises(ContextValidationError, match="existence conflicts with context entry"):
        validate_context_manifest(
            replace(
                document,
                source_artifacts=(changed_source, *document.source_artifacts[1:]),
            )
        )


def test_missing_generated_workset_artifact_is_rejected():
    with pytest.raises(ContextValidationError, match="exactly the generated agent workset"):
        validate_context_manifest(replace(_complete_document(), generated_artifacts=()))


def test_generated_workset_context_entry_is_present_and_unselected():
    validated = validate_context_manifest(_complete_document())
    workset = next(
        entry for entry in validated.context_entries if entry.entry_id == "agent_workset_output"
    )

    assert workset.existence == ExistenceState.PRESENT
    assert workset.inclusion_mode == InclusionMode.EXCLUDED
    assert workset.size_estimate.selected == _values()


@pytest.mark.parametrize(
    "existence",
    [ExistenceState.MISSING, ExistenceState.UNREADABLE],
)
def test_generated_workset_context_entry_must_be_present(existence):
    document = _replace_document_entry(
        _complete_document(),
        "agent_workset_output",
        existence=existence,
    )

    with pytest.raises(
        ContextValidationError,
        match="generated workset/context-entry existence contradiction",
    ):
        validate_context_manifest(document)


def test_parsed_yaml_rejects_generated_workset_context_existence_contradiction():
    data = _rendered_data(_complete_document())
    workset = next(
        entry
        for entry in data["context_entries"]
        if entry["entry_id"] == "agent_workset_output"
    )
    workset["existence"] = ExistenceState.MISSING.value

    with pytest.raises(
        ContextValidationError,
        match="generated workset/context-entry existence contradiction",
    ):
        _parse_data(data)


def test_extra_generated_artifact_is_rejected():
    document = _complete_document()

    with pytest.raises(ContextValidationError, match="exactly the generated agent workset"):
        validate_context_manifest(
            replace(
                document,
                generated_artifacts=(*document.generated_artifacts, document.generated_artifacts[0]),
            )
        )


def test_valid_realistic_document_is_canonically_ordered():
    validated = validate_context_manifest(_realistic_document())

    assert [entry.entry_id for entry in validated.context_entries] == [
        entry.entry_id for entry in build_context_registry(SLUG)
    ]
    assert validated.generated_artifacts[0].sha256 == WORKSET_HASH


def test_explicit_registry_contains_only_approved_entries_in_stable_order():
    registry = build_context_registry(SLUG)

    assert registry[0].entry_id == "task"
    assert registry[-1].entry_id == "global_agent_instructions"
    assert [entry.order for entry in registry] == sorted(entry.order for entry in registry)
    assert {entry.entry_id for entry in registry} == {
        "task",
        "acceptance",
        "architecture_notes",
        "coupling_notes",
        "test_contract",
        "verification",
        "evidence",
        "spec",
        "requirements_projection",
        "preflight",
        "test_contract_review",
        "selected_packs",
        "repository_signals",
        "agent_workset_output",
        "context_manifest_output",
        "evidence_report",
        "validation_report",
        "global_agent_instructions",
    }


def test_rendering_is_deterministic_and_has_final_newline():
    document = _realistic_document()

    first = render_context_manifest_yaml(document)
    second = render_context_manifest_yaml(document)

    assert first == second
    assert first.endswith("\n")


def test_rendering_has_no_timestamp_python_tags_or_self_hash():
    rendered = render_context_manifest_yaml(_realistic_document())

    assert "timestamp" not in rendered
    assert "generated_at" not in rendered
    assert "!!python" not in rendered
    assert context_manifest_path(SLUG) not in str(_rendered_data()["generated_artifacts"])


def test_parse_render_round_trip_returns_canonical_model():
    document = validate_context_manifest(_realistic_document())

    parsed = parse_context_manifest_yaml(render_context_manifest_yaml(document))

    assert parsed == document


def test_duplicate_top_level_yaml_key_is_rejected():
    rendered = render_context_manifest_yaml(_complete_document())
    duplicate = rendered.replace("schema_version: 1\n", "schema_version: 1\nschema_version: 1\n", 1)

    with pytest.raises(ContextValidationError, match=r"duplicate YAML key 'schema_version'"):
        parse_context_manifest_yaml(duplicate)


def test_duplicate_nested_yaml_key_is_rejected():
    rendered = render_context_manifest_yaml(_complete_document())
    duplicate = rendered.replace(
        "    per_file_warning_approximate_tokens: 4000\n",
        "    per_file_warning_approximate_tokens: 4000\n"
        "    per_file_warning_approximate_tokens: 4000\n",
        1,
    )

    with pytest.raises(
        ContextValidationError,
        match=r"duplicate YAML key 'per_file_warning_approximate_tokens'",
    ):
        parse_context_manifest_yaml(duplicate)


def test_duplicate_yaml_key_inside_list_item_is_rejected():
    rendered = render_context_manifest_yaml(_realistic_document())
    duplicate = rendered.replace("- entry_id: task\n", "- entry_id: task\n  entry_id: task\n", 1)

    with pytest.raises(ContextValidationError, match=r"duplicate YAML key 'entry_id'"):
        parse_context_manifest_yaml(duplicate)


def test_duplicate_yaml_keys_with_identical_values_are_rejected():
    rendered = render_context_manifest_yaml(_complete_document())
    duplicate = rendered.replace(
        "artifact_role: context_selection_manifest\n",
        "artifact_role: context_selection_manifest\nartifact_role: context_selection_manifest\n",
        1,
    )

    with pytest.raises(ContextValidationError, match=r"duplicate YAML key 'artifact_role'"):
        parse_context_manifest_yaml(duplicate)


def test_non_string_top_level_mapping_key_is_rejected_cleanly():
    rendered = render_context_manifest_yaml(_complete_document())
    invalid = f"1: invalid\n{rendered}"

    with pytest.raises(
        ContextValidationError,
        match=r"context manifest mapping keys must be strings; found 1",
    ):
        parse_context_manifest_yaml(invalid)


def test_non_string_nested_mapping_key_is_rejected_cleanly():
    rendered = render_context_manifest_yaml(_complete_document())
    invalid = rendered.replace(
        "    per_file_warning_approximate_tokens: 4000\n",
        "    true: invalid\n    per_file_warning_approximate_tokens: 4000\n",
        1,
    )

    with pytest.raises(
        ContextValidationError,
        match=r"budget\.configuration mapping keys must be strings; found True",
    ):
        parse_context_manifest_yaml(invalid)


def test_unhashable_non_string_yaml_key_is_rejected_cleanly():
    rendered = render_context_manifest_yaml(_complete_document())
    invalid = f"? [not, a, string]\n: invalid\n{rendered}"

    with pytest.raises(ContextValidationError, match=r"YAML mapping at .* non-string, unhashable key"):
        parse_context_manifest_yaml(invalid)


def test_parser_rejects_non_mapping_top_level():
    with pytest.raises(ContextValidationError, match="must be a mapping"):
        parse_context_manifest_yaml("- not\n- a\n- mapping\n")


def test_unknown_schema_version_is_rejected():
    data = _rendered_data()
    data["schema_version"] = 2

    with pytest.raises(ContextValidationError, match="schema_version must be 1"):
        _parse_data(data)


def test_missing_required_key_is_rejected():
    data = _rendered_data()
    del data["budget"]

    with pytest.raises(ContextValidationError, match="missing required field: budget"):
        _parse_data(data)


def test_unknown_top_level_schema_field_is_rejected():
    data = _rendered_data()
    data["unexpected"] = "value"

    with pytest.raises(ContextValidationError, match="context manifest contains unknown field: unexpected"):
        _parse_data(data)


def test_unknown_nested_schema_field_is_rejected():
    data = _rendered_data()
    data["budget"]["configuration"]["unexpected"] = 1

    with pytest.raises(
        ContextValidationError,
        match="budget.configuration contains unknown field: unexpected",
    ):
        _parse_data(data)


def test_invalid_enum_is_rejected():
    data = _rendered_data()
    data["context_entries"][0]["classification"] = "semantic_summary"

    with pytest.raises(ContextValidationError, match="classification must be one of"):
        _parse_data(data)


def test_duplicate_context_entry_id_is_rejected():
    document = _realistic_document()
    duplicate = replace(_repository_entry(), path=f".harness/tasks/{SLUG}/task.md")
    duplicate = replace(duplicate, entry_id="task")

    with pytest.raises(ContextValidationError, match="duplicate context entry_id"):
        validate_context_manifest(replace(document, context_entries=(*document.context_entries, duplicate)))


def test_duplicate_registry_path_is_rejected():
    path = ".harness/tasks/example/task.md"
    entries = (
        _registry_entry("one", path),
        _registry_entry("two", path, order=20),
    )

    with pytest.raises(ContextValidationError, match="duplicate context registry path"):
        validate_registry(entries)


def test_conflicting_registry_classification_is_rejected():
    path = ".harness/tasks/example/task.md"
    entries = (
        _registry_entry("one", path),
        _registry_entry(
            "two",
            path,
            classification=Classification.DERIVED,
            authority=AuthorityLevel.DERIVED_PROJECTION,
            order=20,
        ),
    )

    with pytest.raises(ContextValidationError, match="conflicting registry classifications"):
        validate_registry(entries)


@pytest.mark.parametrize(
    ("classification", "authority"),
    [
        (Classification.SOURCE, AuthorityLevel.HARNESS_CONFIGURATION),
        (Classification.CONFIGURATION, AuthorityLevel.TASK_SOURCE),
        (Classification.REPOSITORY_SIGNAL, AuthorityLevel.ADVISORY_REPORT),
        (Classification.DERIVED, AuthorityLevel.TASK_SOURCE),
    ],
)
def test_registry_rejects_incompatible_classification_and_authority(
    classification,
    authority,
):
    entry = _registry_entry(
        "incompatible",
        ".harness/custom/incompatible.md",
        classification=classification,
        authority=authority,
        inclusion=InclusionMode.READ_IF_NEEDED,
    )

    with pytest.raises(ContextValidationError, match="is incompatible with classification"):
        validate_registry((entry,))


@pytest.mark.parametrize(
    "path",
    [
        "/absolute/task.md",
        "C:/Temp/task.md",
        "C:\\Temp\\task.md",
        "\\\\server\\share\\task.md",
        ".harness\\tasks\\task.md",
        ".harness/tasks//task.md",
        ".harness/tasks/./task.md",
        ".harness/tasks/../task.md",
        ".harness/tasks/task.md/",
        "",
    ],
)
def test_unsafe_registry_paths_are_rejected(path):
    with pytest.raises(ContextValidationError):
        validate_registry((_registry_entry("unsafe", path),))


def test_null_path_for_non_synthetic_entry_is_rejected():
    with pytest.raises(ContextValidationError, match="non-synthetic registry entry input requires a path"):
        validate_registry((_registry_entry("input", None),))


def test_path_for_synthetic_entry_is_rejected():
    entry = _registry_entry(
        "signals",
        ".harness/signals.yaml",
        classification=Classification.REPOSITORY_SIGNAL,
        authority=AuthorityLevel.REPOSITORY_OBSERVATION,
        inclusion=InclusionMode.READ_IF_NEEDED,
        synthetic=True,
    )

    with pytest.raises(ContextValidationError, match="synthetic registry entry signals must have path null"):
        validate_registry((entry,))


def test_task_generated_path_cannot_be_registered_as_selected_input():
    entry = _registry_entry(
        "generated_input",
        ".harness/tasks/example/generated/custom.md",
        classification=Classification.DERIVED,
        authority=AuthorityLevel.DERIVED_PROJECTION,
        inclusion=InclusionMode.READ_IF_NEEDED,
    )

    with pytest.raises(ContextValidationError, match="generated task output cannot be selected"):
        validate_registry((entry,))


def test_negative_size_is_rejected():
    entry = replace(_task_entry(), size_estimate=_size(source=-1, selected=0))

    with pytest.raises(ContextValidationError, match="must be non-negative"):
        validate_context_manifest(replace(_complete_document(), context_entries=(entry,)))


def test_excluded_entry_with_nonzero_selected_size_is_rejected():
    entry = replace(_workset_entry(), size_estimate=_size(source=1, selected=1))

    with pytest.raises(ContextValidationError, match="must have zero selected size"):
        validate_context_manifest(replace(_complete_document(), context_entries=(entry,)))


def test_excluded_entry_without_exclusion_reason_is_rejected():
    entry = replace(_workset_entry(), exclusion_reason=None)

    with pytest.raises(ContextValidationError, match="requires a stable snake_case exclusion_reason"):
        validate_context_manifest(replace(_complete_document(), context_entries=(entry,)))


def test_included_entry_with_exclusion_reason_is_rejected():
    entry = replace(_task_entry(), exclusion_reason="not_needed")

    with pytest.raises(ContextValidationError, match="must have exclusion_reason null"):
        validate_context_manifest(replace(_complete_document(), context_entries=(entry,)))


def test_generated_output_selected_as_input_is_rejected():
    entry = replace(
        _workset_entry(),
        inclusion_mode=InclusionMode.READ_IF_NEEDED,
        exclusion_reason=None,
    )

    with pytest.raises(ContextValidationError, match="inclusion_mode conflicts with the registry"):
        validate_context_manifest(replace(_complete_document(), context_entries=(entry,)))


def test_context_manifest_cannot_list_itself_as_generated_artifact():
    artifact = GeneratedArtifact(
        path=context_manifest_path(SLUG),
        artifact_role="agent_handoff",
        authority_level=AuthorityLevel.HANDOFF_OUTPUT,
        sha256=WORKSET_HASH,
    )

    with pytest.raises(ContextValidationError, match="must not list itself"):
        validate_context_manifest(replace(_complete_document(), generated_artifacts=(artifact,)))


def test_registry_self_dependency_is_rejected():
    entry = _registry_entry("self", ".harness/tasks/example/task.md", dependencies=("self",))

    with pytest.raises(ContextValidationError, match="self-dependency"):
        validate_registry((entry,))


def test_registry_dependency_on_unknown_entry_is_rejected():
    entry = _registry_entry(
        "dependent",
        ".harness/tasks/example/task.md",
        dependencies=("missing",),
    )

    with pytest.raises(ContextValidationError, match="depends on unknown entry missing"):
        validate_registry((entry,))


def test_registry_lineage_cycle_is_rejected():
    entries = (
        _registry_entry("one", ".harness/tasks/example/one.md", dependencies=("two",)),
        _registry_entry("two", ".harness/tasks/example/two.md", order=20, dependencies=("one",)),
    )

    with pytest.raises(ContextValidationError, match="lineage cycle"):
        validate_registry(entries)


@pytest.mark.parametrize("invalid_hash", ["a" * 63, "A" * 64, "g" * 64, ""])
def test_invalid_source_sha256_is_rejected(invalid_hash):
    artifact = SourceArtifact(
        path=f".harness/tasks/{SLUG}/task.md",
        authority_level=AuthorityLevel.TASK_SOURCE,
        existence=ExistenceState.PRESENT,
        sha256=invalid_hash,
    )

    with pytest.raises(ContextValidationError, match="64 lowercase hexadecimal"):
        validate_context_manifest(replace(_complete_document(), source_artifacts=(artifact,)))


def test_missing_source_requires_null_sha256():
    artifact = SourceArtifact(
        path=f".harness/tasks/{SLUG}/task.md",
        authority_level=AuthorityLevel.TASK_SOURCE,
        existence=ExistenceState.MISSING,
        sha256=SOURCE_HASH,
    )

    with pytest.raises(ContextValidationError, match="must be null"):
        validate_context_manifest(replace(_complete_document(), source_artifacts=(artifact,)))


def test_invalid_generated_sha256_is_rejected():
    artifact = GeneratedArtifact(
        path=agent_workset_path(SLUG),
        artifact_role="agent_handoff",
        authority_level=AuthorityLevel.HANDOFF_OUTPUT,
        sha256="not-a-hash",
    )

    with pytest.raises(ContextValidationError, match="64 lowercase hexadecimal"):
        validate_context_manifest(replace(_complete_document(), generated_artifacts=(artifact,)))


def test_findings_render_in_deterministic_severity_and_value_order():
    findings = (
        ContextFinding("z_info", FindingLevel.INFO, None, "Info"),
        ContextFinding("b_warning", FindingLevel.WARNING, None, "Warning B"),
        ContextFinding("a_blocker", FindingLevel.BLOCKER, None, "Blocker"),
        ContextFinding("a_warning", FindingLevel.WARNING, None, "Warning A"),
    )
    data = _rendered_data(replace(_complete_document(), findings=findings))

    assert [item["code"] for item in data["findings"]] == [
        "a_blocker",
        "a_warning",
        "b_warning",
        "z_info",
    ]


def test_entries_render_by_order_then_entry_id():
    entries = tuple(reversed(_realistic_document().context_entries))
    data = _rendered_data(replace(_realistic_document(), context_entries=entries))

    assert [item["entry_id"] for item in data["context_entries"]] == [
        item.entry_id for item in build_context_registry(SLUG)
    ]


def test_source_artifacts_render_by_registry_order():
    document = _complete_document()
    data = _rendered_data(
        replace(document, source_artifacts=tuple(reversed(document.source_artifacts)))
    )

    assert [item["path"].rsplit("/", 1)[-1] for item in data["source_artifacts"]] == [
        "task.md",
        "acceptance.md",
        "architecture-notes.md",
        "coupling-notes.md",
        "test-contract.md",
        "verification.md",
        "evidence.md",
    ]


def test_invalid_finding_code_and_empty_message_are_rejected():
    invalid_code = ContextFinding("Not Stable", FindingLevel.WARNING, None, "Message")
    empty_message = ContextFinding("stable_code", FindingLevel.WARNING, None, "")

    with pytest.raises(ContextValidationError, match="snake_case"):
        validate_context_manifest(replace(_complete_document(), findings=(invalid_code,)))
    with pytest.raises(ContextValidationError, match="message must be non-empty"):
        validate_context_manifest(replace(_complete_document(), findings=(empty_message,)))


def test_negative_budget_value_and_invalid_budget_status_are_rejected():
    negative = replace(
        _budget(),
        result=replace(_budget().result, selected_bytes=-1),
    )

    with pytest.raises(ContextValidationError, match="selected_bytes must be non-negative"):
        validate_context_manifest(replace(_complete_document(), budget=negative))

    data = _rendered_data(_complete_document())
    data["budget"]["result"]["status"] = "enforced"
    with pytest.raises(ContextValidationError, match="status must be one of"):
        _parse_data(data)


def test_boolean_value_is_rejected_for_integer_field():
    data = _rendered_data(_complete_document())
    data["budget"]["result"]["selected_bytes"] = True

    with pytest.raises(ContextValidationError, match="selected_bytes must be an integer"):
        _parse_data(data)


def test_per_file_warning_threshold_must_be_below_high_risk_threshold():
    configuration = replace(
        _budget().configuration,
        per_file_warning_approximate_tokens=8000,
    )

    with pytest.raises(ContextValidationError, match="per-file high-risk threshold must exceed the warning threshold"):
        validate_context_manifest(
            replace(_complete_document(), budget=replace(_budget(), configuration=configuration))
        )


def test_total_warning_threshold_must_be_below_high_risk_threshold():
    configuration = replace(
        _budget().configuration,
        total_warning_approximate_tokens=24000,
    )

    with pytest.raises(ContextValidationError, match="total high-risk threshold must exceed the warning threshold"):
        validate_context_manifest(
            replace(_complete_document(), budget=replace(_budget(), configuration=configuration))
        )


def test_budget_configuration_resolver_preserves_valid_custom_configuration():
    custom = _budget_configuration_data(
        per_file_warning_approximate_tokens=100,
        per_file_high_risk_approximate_tokens=200,
        total_warning_approximate_tokens=300,
        total_high_risk_approximate_tokens=400,
    )

    configuration, findings = resolve_budget_configuration(
        {"harness_version": "1.0.0", "context_budget": custom}
    )

    assert configuration == BudgetConfiguration(**custom)
    assert findings == ()


def test_missing_budget_configuration_uses_defaults_with_one_info_finding():
    configuration, findings = resolve_budget_configuration({"harness_version": "1.0.0"})

    assert configuration == _budget().configuration
    assert len(findings) == 1
    assert findings[0].code == "context_budget_defaults_applied"
    assert findings[0].level == FindingLevel.INFO
    assert findings[0].path == ".harness/config.yaml"
    assert "absent" in findings[0].message
    assert "advisory model-agnostic" in findings[0].message


def _missing_budget_field() -> dict[str, object]:
    data = _budget_configuration_data()
    del data["total_high_risk_approximate_tokens"]
    return data


def _budget_with_unknown_field() -> dict[str, object]:
    data = _budget_configuration_data()
    data["unexpected_threshold"] = 1
    return data


@pytest.mark.parametrize(
    "section",
    [
        pytest.param("not-a-mapping", id="non-mapping"),
        pytest.param(_missing_budget_field(), id="missing-field"),
        pytest.param(_budget_with_unknown_field(), id="unknown-field"),
        pytest.param(
            _budget_configuration_data(per_file_warning_approximate_tokens=True),
            id="boolean",
        ),
        pytest.param(
            _budget_configuration_data(per_file_warning_approximate_tokens="4000"),
            id="non-integer",
        ),
        pytest.param(
            _budget_configuration_data(per_file_warning_approximate_tokens=-1),
            id="negative",
        ),
        pytest.param(
            _budget_configuration_data(
                per_file_warning_approximate_tokens=8000,
                per_file_high_risk_approximate_tokens=8000,
            ),
            id="equal-per-file-thresholds",
        ),
        pytest.param(
            _budget_configuration_data(
                total_warning_approximate_tokens=24000,
                total_high_risk_approximate_tokens=24000,
            ),
            id="equal-total-thresholds",
        ),
        pytest.param(
            _budget_configuration_data(
                per_file_warning_approximate_tokens=9000,
                per_file_high_risk_approximate_tokens=8000,
            ),
            id="reversed-per-file-thresholds",
        ),
        pytest.param(
            _budget_configuration_data(
                total_warning_approximate_tokens=25000,
                total_high_risk_approximate_tokens=24000,
            ),
            id="reversed-total-thresholds",
        ),
    ],
)
def test_malformed_budget_configuration_uses_complete_defaults_with_one_warning(section):
    configuration, findings = resolve_budget_configuration({"context_budget": section})

    assert configuration == _budget().configuration
    assert len(findings) == 1
    assert findings[0].code == "malformed_context_budget_configuration"
    assert findings[0].level == FindingLevel.WARNING
    assert findings[0].path == ".harness/config.yaml"
    assert "complete default set" in findings[0].message
    assert "advisory model-agnostic" in findings[0].message


def test_partially_valid_budget_configuration_does_not_retain_custom_values():
    partial = {
        "per_file_warning_approximate_tokens": 1,
        "per_file_high_risk_approximate_tokens": 2,
        "total_warning_approximate_tokens": 3,
    }

    configuration, _ = resolve_budget_configuration({"context_budget": partial})

    assert configuration == _budget().configuration
    assert configuration.per_file_warning_approximate_tokens == 4000


def test_budget_configuration_resolution_is_deterministic():
    configuration_data = {
        "context_budget": _budget_configuration_data(
            total_high_risk_approximate_tokens="invalid"
        )
    }

    assert resolve_budget_configuration(configuration_data) == resolve_budget_configuration(
        configuration_data
    )


@pytest.mark.parametrize("configuration_data", [None, [], "invalid"])
def test_budget_configuration_resolver_rejects_unsupported_top_level_input(
    configuration_data,
):
    with pytest.raises(ContextValidationError, match="repository configuration must be a mapping"):
        resolve_budget_configuration(configuration_data)


def test_resolver_does_not_weaken_strict_direct_budget_validation(tmp_path):
    invalid = _budget_configuration(
        per_file_warning=8000,
        per_file_high_risk=8000,
    )

    with pytest.raises(
        ContextValidationError,
        match="per-file high-risk threshold must exceed the warning threshold",
    ):
        _single_source_selection(tmp_path, "selected", configuration=invalid)


def test_complete_task_selection_is_deterministic_and_authority_ordered(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)

    result = select_context(
        root,
        SLUG,
        derived_freshness=_fresh_derived(),
        repository_observations=_observations(),
    )

    assert [entry.entry_id for entry in result.selected_entries] == [
        "task",
        "acceptance",
        "architecture_notes",
        "coupling_notes",
        "test_contract",
        "verification",
        "evidence",
        "spec",
        "requirements_projection",
        "preflight",
        "test_contract_review",
        "selected_packs",
        "repository_signals",
    ]
    assert len(result.source_artifacts) == 7
    assert all(artifact.source_sha256 for artifact in result.source_artifacts)
    assert result == select_context(
        root,
        SLUG,
        derived_freshness=_fresh_derived(),
        repository_observations=_observations(),
    )


def test_read_first_entries_precede_lower_order_read_if_needed_entries(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    entries = (
        _registry_entry(
            "later",
            ".harness/custom/later.md",
            inclusion=InclusionMode.READ_IF_NEEDED,
            order=1,
        ),
        _registry_entry("first", ".harness/custom/first.md", order=20),
    )
    _write_repository_file(root, ".harness/custom/later.md", "later")
    _write_repository_file(root, ".harness/custom/first.md", "first")

    result = select_context(root, SLUG, derived_freshness={}, registry=entries)

    assert [entry.entry_id for entry in result.selected_entries] == ["first", "later"]


def test_excluded_outputs_are_inspected_but_never_selected(tmp_path):
    root = _selection_repository(tmp_path)
    for relative in (
        agent_workset_path(SLUG),
        context_manifest_path(SLUG),
        f".harness/tasks/{SLUG}/evidence-report.md",
        f".harness/tasks/{SLUG}/validation-report.md",
        ".harness/generated/agent-instructions.md",
    ):
        _write_repository_file(root, relative, "must not be selected")

    result = select_context(root, SLUG, derived_freshness={})
    selected_ids = {entry.entry_id for entry in result.selected_entries}

    assert selected_ids.isdisjoint(
        {
            "agent_workset_output",
            "context_manifest_output",
            "evidence_report",
            "validation_report",
            "global_agent_instructions",
        }
    )
    assert all(
        artifact.source_content is None
        for artifact in result.artifacts
        if artifact.entry_id in {
            "agent_workset_output",
            "context_manifest_output",
            "evidence_report",
            "validation_report",
            "global_agent_instructions",
        }
    )
    assert all(
        artifact.source_sha256 is None
        for artifact in result.artifacts
        if artifact.inclusion_mode == InclusionMode.EXCLUDED
    )


def test_missing_required_source_produces_blocker(tmp_path):
    root = _selection_repository(tmp_path)
    (root / ".harness/tasks" / SLUG / "task.md").unlink()

    result = select_context(root, SLUG, derived_freshness={})

    finding = next(item for item in result.findings if item.code == "missing_required_source")
    assert finding.level == FindingLevel.BLOCKER
    assert finding.path.endswith("task.md")
    artifact = next(item for item in result.artifacts if item.entry_id == "task")
    assert artifact.freshness_state == FreshnessState.NOT_APPLICABLE
    assert artifact.source_sha256 is None


def test_invalid_utf8_required_source_produces_blocker(tmp_path):
    root = _selection_repository(tmp_path)
    _write_repository_file(root, f".harness/tasks/{SLUG}/task.md", b"\xff\xfe")

    result = select_context(root, SLUG, derived_freshness={})

    finding = next(item for item in result.findings if item.code == "unreadable_required_source")
    assert finding.level == FindingLevel.BLOCKER
    assert "task" not in {entry.entry_id for entry in result.selected_entries}
    artifact = next(item for item in result.artifacts if item.entry_id == "task")
    assert artifact.source_sha256 is None


def test_readable_source_inspection_captures_exact_raw_byte_sha256(tmp_path):
    root = _selection_repository(tmp_path)
    target = root / ".harness" / "tasks" / SLUG / "task.md"
    raw = b"# Task\r\n\r\nexact bytes\r\n"
    target.write_bytes(raw)

    result = select_context(root, SLUG, derived_freshness={})
    artifact = next(item for item in result.source_artifacts if item.entry_id == "task")

    assert artifact.source_sha256 == sha256_bytes(raw)
    assert artifact.source_size.bytes == len(raw)


def test_lf_and_crlf_source_snapshots_have_different_sha256(tmp_path):
    root = _selection_repository(tmp_path)
    target = root / ".harness" / "tasks" / SLUG / "task.md"
    target.write_bytes(b"one\ntwo\n")
    lf = select_context(root, SLUG, derived_freshness={}).source_artifacts[0]
    target.write_bytes(b"one\r\ntwo\r\n")
    crlf = select_context(root, SLUG, derived_freshness={}).source_artifacts[0]

    assert lf.source_sha256 == sha256_bytes(b"one\ntwo\n")
    assert crlf.source_sha256 == sha256_bytes(b"one\r\ntwo\r\n")
    assert lf.source_sha256 != crlf.source_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"source_sha256": None},
        {"source_sha256": "0" * 64},
        {
            "existence": ExistenceState.MISSING,
            "source_content": None,
            "source_size": SizeValues(0, 0, 0, 0),
            "source_sha256": "0" * 64,
        },
    ],
)
def test_selection_rejects_inconsistent_source_snapshot_hash_state(tmp_path, changes):
    root = _selection_repository(tmp_path)
    result = select_context(root, SLUG, derived_freshness={})
    artifacts = list(result.artifacts)
    artifacts[0] = replace(artifacts[0], **changes)

    with pytest.raises(ContextValidationError, match="source_sha256"):
        replace(result, artifacts=tuple(artifacts))


def test_selection_rejects_present_task_source_without_captured_content_or_hash(
    tmp_path,
):
    root = _selection_repository(tmp_path)
    result = select_context(root, SLUG, derived_freshness={})
    artifacts = _replace_result_item(
        result.artifacts,
        "task",
        source_content=None,
        source_size=SizeValues(0, 0, 0, 0),
        source_sha256=None,
    )

    with pytest.raises(ContextValidationError, match="requires source_content"):
        replace(result, artifacts=artifacts)


@pytest.mark.parametrize(
    "existence",
    [ExistenceState.MISSING, ExistenceState.UNREADABLE],
)
def test_selection_rejects_nonpresent_task_source_with_fabricated_snapshot(
    tmp_path,
    existence,
):
    root = _selection_repository(tmp_path)
    result = select_context(root, SLUG, derived_freshness={})
    artifacts = _replace_result_item(
        result.artifacts,
        "task",
        existence=existence,
    )

    with pytest.raises(
        ContextValidationError,
        match="non-read file-backed artifact must have null source_content",
    ):
        replace(result, artifacts=artifacts)


def test_selection_rejects_present_excluded_output_with_fabricated_snapshot(tmp_path):
    root = _selection_repository(tmp_path)
    _write_repository_file(root, agent_workset_path(SLUG), "existing workset")
    result = select_context(root, SLUG, derived_freshness={})
    fabricated = "fabricated"
    artifacts = _replace_result_item(
        result.artifacts,
        "agent_workset_output",
        source_content=fabricated,
        source_size=SizeValues(10, 10, 1, 3),
        source_sha256=sha256_bytes(fabricated.encode("utf-8")),
    )

    with pytest.raises(
        ContextValidationError,
        match="non-read file-backed artifact must have null source_content",
    ):
        replace(result, artifacts=artifacts)


def test_selection_rejects_present_nonfresh_derived_with_fabricated_snapshot(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    freshness = {**_fresh_derived(), "spec": FreshnessState.STALE}
    result = select_context(root, SLUG, derived_freshness=freshness)
    fabricated = "fabricated"
    artifacts = _replace_result_item(
        result.artifacts,
        "spec",
        source_content=fabricated,
        source_size=SizeValues(10, 10, 1, 3),
        source_sha256=sha256_bytes(fabricated.encode("utf-8")),
    )

    with pytest.raises(
        ContextValidationError,
        match="non-read file-backed artifact must have null source_content",
    ):
        replace(result, artifacts=artifacts)


def test_valid_readable_task_and_fresh_derived_snapshots_revalidate(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    result = select_context(
        root,
        SLUG,
        derived_freshness=_fresh_derived(),
        repository_observations=_observations(),
    )
    by_id = {artifact.entry_id: artifact for artifact in result.artifacts}

    assert by_id["task"].source_sha256 == sha256_bytes(
        (root / by_id["task"].path).read_bytes()
    )
    assert by_id["spec"].source_sha256 == sha256_bytes(
        (root / by_id["spec"].path).read_bytes()
    )
    assert replace(result) == result


@pytest.mark.parametrize(
    "freshness",
    [FreshnessState.STALE, FreshnessState.UNKNOWN],
)
def test_valid_present_nonfresh_derived_snapshot_remains_nonread(tmp_path, freshness):
    root = _selection_repository(tmp_path, include_derived=True)
    states = {**_fresh_derived(), "spec": freshness}
    result = select_context(root, SLUG, derived_freshness=states)
    spec = next(artifact for artifact in result.artifacts if artifact.entry_id == "spec")

    assert spec.existence == ExistenceState.PRESENT
    assert spec.source_content is None
    assert spec.source_size == SizeValues(0, 0, 0, 0)
    assert spec.source_sha256 is None
    assert replace(result) == result


def test_valid_synthetic_repository_observation_content_keeps_null_digest(tmp_path):
    root = _selection_repository(tmp_path)
    result = select_context(
        root,
        SLUG,
        derived_freshness={},
        repository_observations=_observations(),
    )
    observations = next(
        artifact
        for artifact in result.artifacts
        if artifact.entry_id == "repository_signals"
    )

    assert observations.path is None
    assert observations.source_content is not None
    assert observations.source_size != SizeValues(0, 0, 0, 0)
    assert observations.source_sha256 is None
    assert replace(result) == result


def test_missing_optional_derived_artifact_produces_warning(tmp_path):
    root = _selection_repository(tmp_path)

    result = select_context(root, SLUG, derived_freshness={})

    missing = [item for item in result.findings if item.code == "missing_optional_artifact"]
    assert missing
    assert all(item.level == FindingLevel.WARNING for item in missing)


def test_fresh_derived_artifact_is_read_and_selected(tmp_path, monkeypatch):
    root = _selection_repository(tmp_path, include_derived=True)
    spec_target = (root / ".harness/tasks" / SLUG / "spec.md").resolve()
    real_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def tracking_read_bytes(path):
        if path == spec_target:
            reads.append(path)
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tracking_read_bytes)

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert "spec" in {entry.entry_id for entry in result.selected_entries}
    assert _selected(result, "spec").freshness_state == FreshnessState.FRESH
    artifact = next(item for item in result.artifacts if item.entry_id == "spec")
    assert artifact.source_content is not None
    assert reads == [spec_target]


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (FreshnessState.STALE, "stale_derived_artifact_excluded"),
        (FreshnessState.UNKNOWN, "unknown_derived_artifact_excluded"),
    ],
)
def test_nonfresh_derived_artifact_is_excluded_before_reading(
    tmp_path,
    monkeypatch,
    state,
    code,
):
    root = _selection_repository(tmp_path, include_derived=True)
    spec_target = (root / ".harness/tasks" / SLUG / "spec.md").resolve()
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path == spec_target:
            pytest.fail(f"non-fresh derived artifact was read: {path}")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    freshness = _fresh_derived()
    freshness["spec"] = state

    result = select_context(root, SLUG, derived_freshness=freshness)

    assert "spec" not in {entry.entry_id for entry in result.selected_entries}
    assert "spec" not in dict(result.selected_content_by_entry)
    artifact = next(item for item in result.artifacts if item.entry_id == "spec")
    assert artifact.existence == ExistenceState.PRESENT
    assert artifact.freshness_state == state
    assert artifact.source_content is None
    finding = next(item for item in result.findings if item.code == code and item.path.endswith("spec.md"))
    state_label = "Stale" if state == FreshnessState.STALE else "Derived artifact has unknown freshness and"
    expected_start = (
        f"{state_label} derived artifact was excluded"
        if state == FreshnessState.STALE
        else state_label + " was excluded"
    )
    assert finding.message == (
        f"{expected_start}: .harness/tasks/{SLUG}/spec.md; "
        f"rerun `ai-sdlc spec --task {SLUG}`."
    )


def test_missing_derived_freshness_decision_defaults_unknown_without_reading(tmp_path, monkeypatch):
    root = _selection_repository(tmp_path, include_derived=True)
    spec_target = (root / ".harness/tasks" / SLUG / "spec.md").resolve()
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path == spec_target:
            pytest.fail(f"derived artifact with omitted freshness was read: {path}")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    first = select_context(root, SLUG, derived_freshness={})
    second = select_context(root, SLUG, derived_freshness={})

    assert first == second
    assert "spec" not in {entry.entry_id for entry in first.selected_entries}
    assert "unknown_derived_artifact_excluded" in _finding_codes(first)
    artifact = next(item for item in first.artifacts if item.entry_id == "spec")
    assert artifact.existence == ExistenceState.PRESENT
    assert artifact.freshness_state == FreshnessState.UNKNOWN
    assert artifact.source_content is None


def test_explicit_missing_freshness_for_present_derived_file_does_not_read(tmp_path, monkeypatch):
    root = _selection_repository(tmp_path, include_derived=True)
    spec_target = (root / ".harness/tasks" / SLUG / "spec.md").resolve()
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path == spec_target:
            pytest.fail(f"derived artifact marked missing was read: {path}")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    freshness = _fresh_derived()
    freshness["spec"] = FreshnessState.MISSING

    result = select_context(root, SLUG, derived_freshness=freshness)

    artifact = next(item for item in result.artifacts if item.entry_id == "spec")
    assert artifact.existence == ExistenceState.PRESENT
    assert artifact.freshness_state == FreshnessState.MISSING
    assert artifact.source_content is None
    assert "spec" not in {entry.entry_id for entry in result.selected_entries}
    assert _context_entry(result, "spec").size_estimate == SizeEstimate(
        source=_values(),
        selected=_values(),
    )


def test_actual_missing_derived_file_overrides_supplied_fresh_state(tmp_path, monkeypatch):
    root = _selection_repository(tmp_path, include_derived=True)
    spec_target = (root / ".harness/tasks" / SLUG / "spec.md").resolve()
    spec_target.unlink()
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path == spec_target:
            pytest.fail(f"physically missing derived artifact was read: {path}")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    artifact = next(item for item in result.artifacts if item.entry_id == "spec")
    assert artifact.existence == ExistenceState.MISSING
    assert artifact.freshness_state == FreshnessState.MISSING
    assert artifact.source_content is None
    assert _context_entry(result, "spec").size_estimate == SizeEstimate(
        source=_values(),
        selected=_values(),
    )
    finding = next(
        item
        for item in result.findings
        if item.code == "missing_optional_artifact" and item.path.endswith("spec.md")
    )
    assert finding.level == FindingLevel.WARNING


def test_selector_accepts_lineage_result_mapping_and_guards_nonfresh_reads(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "lineage-selector-repository"
    root.mkdir()
    assert init_project(root)[0] == 0
    assert start_task(root, "Context Foundation")[0] == 0
    assert run_preflight(root, SLUG)[0] == 0
    assert run_spec(root, SLUG)[0] == 0
    assert run_test_contract_review(root, SLUG)[0] == 0
    resolved = resolve_derived_freshness(root, SLUG)
    assert set(resolved.derived_freshness.values()) == {
        FreshnessState.FRESH
    }

    mixed_mapping = dict(resolved.derived_freshness)
    mixed_mapping.update(
        {
            "spec": FreshnessState.STALE,
            "requirements_projection": FreshnessState.MISSING,
            "test_contract_review": FreshnessState.UNKNOWN,
        }
    )
    compatibility = LineageResolutionResult(
        mixed_mapping,
        resolved.findings,
    )
    task_root = (root / ".harness" / "tasks" / SLUG).resolve()
    guarded = {
        (task_root / "spec.md").resolve(),
        (task_root / "requirements.yaml").resolve(),
        (task_root / "test-contract-review.md").resolve(),
    }
    preflight = (task_root / "preflight.md").resolve()
    preflight_reads: list[Path] = []
    real_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        resolved_path = path.resolve()
        if resolved_path in guarded:
            pytest.fail(f"non-fresh derived artifact was read: {path}")
        if resolved_path == preflight:
            preflight_reads.append(resolved_path)
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    result = select_context(
        root,
        SLUG,
        derived_freshness=compatibility.derived_freshness,
    )

    selected_ids = {entry.entry_id for entry in result.selected_entries}
    assert "preflight" in selected_ids
    assert not {
        "spec",
        "requirements_projection",
        "test_contract_review",
    } & selected_ids
    assert preflight_reads == [preflight]
    derived_findings = {
        finding.code: finding
        for finding in result.findings
        if finding.path in {
            f".harness/tasks/{SLUG}/spec.md",
            f".harness/tasks/{SLUG}/requirements.yaml",
            f".harness/tasks/{SLUG}/test-contract-review.md",
        }
        and finding.code
        in {
            "stale_derived_artifact_excluded",
            "missing_optional_artifact",
            "unknown_derived_artifact_excluded",
        }
    }
    assert derived_findings[
        "stale_derived_artifact_excluded"
    ].message == (
        f"Stale derived artifact was excluded: .harness/tasks/{SLUG}/spec.md; "
        f"rerun `ai-sdlc spec --task {SLUG}`."
    )
    assert derived_findings["missing_optional_artifact"].message == (
        "Derived artifact is marked missing and was excluded: "
        f".harness/tasks/{SLUG}/requirements.yaml; "
        f"rerun `ai-sdlc spec --task {SLUG}`."
    )
    assert derived_findings[
        "unknown_derived_artifact_excluded"
    ].message == (
        "Derived artifact has unknown freshness and was excluded: "
        f".harness/tasks/{SLUG}/test-contract-review.md; "
        f"rerun `ai-sdlc test-contract --task {SLUG}`."
    )


def test_selected_packs_configuration_is_selected_deterministically(tmp_path):
    root = _selection_repository(tmp_path)

    result = select_context(root, SLUG, derived_freshness={})

    packs = _selected(result, "selected_packs")
    assert packs.freshness_state == FreshnessState.NOT_APPLICABLE
    assert "security-baseline" in packs.selected_content


def test_missing_configuration_uses_not_applicable_freshness(tmp_path):
    root = _selection_repository(tmp_path)
    (root / ".harness/packs/selected.yaml").unlink()

    result = select_context(root, SLUG, derived_freshness={})

    artifact = next(item for item in result.artifacts if item.entry_id == "selected_packs")
    assert artifact.existence == ExistenceState.MISSING
    assert artifact.freshness_state == FreshnessState.NOT_APPLICABLE


def test_supplied_repository_observations_are_selected_and_sorted(tmp_path):
    root = _selection_repository(tmp_path)
    observations = replace(_observations(), detected_languages=("rust", "python", "rust"))

    result = select_context(
        root,
        SLUG,
        derived_freshness={},
        repository_observations=observations,
    )

    selected = _selected(result, "repository_signals")
    assert "Detected languages: python, rust" in selected.selected_content
    assert selected.path is None


def test_repository_observation_unavailable_produces_warning(tmp_path):
    root = _selection_repository(tmp_path)

    result = select_context(root, SLUG, derived_freshness={})

    assert "repository_signals" not in {entry.entry_id for entry in result.selected_entries}
    finding = next(item for item in result.findings if item.code == "repository_observation_unavailable")
    assert finding.level == FindingLevel.WARNING


def test_resolved_path_escape_is_rejected(tmp_path, monkeypatch):
    root = _selection_repository(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    target = root / ".harness/tasks" / SLUG / "task.md"
    real_resolve = Path.resolve

    def escaped_resolve(path, *args, **kwargs):
        if path == target:
            return outside
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped_resolve)

    result = select_context(root, SLUG, derived_freshness={})

    finding = next(item for item in result.findings if item.code == "unsafe_resolved_path")
    assert finding.level == FindingLevel.BLOCKER
    assert "task" not in {entry.entry_id for entry in result.selected_entries}


def test_invalid_utf8_optional_artifact_produces_warning(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(root, f".harness/tasks/{SLUG}/preflight.md", b"\xff\xfe")

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    finding = next(item for item in result.findings if item.code == "unreadable_optional_artifact")
    assert finding.level == FindingLevel.WARNING
    assert finding.path.endswith("preflight.md")


def test_selector_does_not_discover_arbitrary_task_files(tmp_path):
    root = _selection_repository(tmp_path)
    arbitrary = f".harness/tasks/{SLUG}/arbitrary-notes.md"
    _write_repository_file(root, arbitrary, "must remain undiscovered")

    result = select_context(root, SLUG, derived_freshness={})

    assert arbitrary not in {item.path for item in result.artifacts}
    assert "must remain undiscovered" not in str(result.selected_content_by_entry)


def test_selected_content_normalizes_newlines_redacts_and_filters_environment_lines(tmp_path):
    root = _selection_repository(tmp_path)
    _write_repository_file(
        root,
        f".harness/tasks/{SLUG}/task.md",
        "# Task\r\nWORKSPACE=C:\\Users\\person\\repo\rsecret sk-abcdefghij\rend\r\n",
    )

    result = select_context(root, SLUG, derived_freshness={})
    selected = _selected(result, "task").selected_content

    assert "\r" not in selected
    assert "C:\\Users" not in selected
    assert "[REDACTED]" in selected
    assert "environment-style line(s) omitted" in selected


def test_source_excerpt_obeys_character_and_line_bounds(tmp_path):
    root = _selection_repository(tmp_path)
    content = "\n".join(f"line {index} " + ("x" * 100) for index in range(80))
    _write_repository_file(root, f".harness/tasks/{SLUG}/task.md", content)

    result = select_context(root, SLUG, derived_freshness={})
    selected = _selected(result, "task").selected_content

    assert len(selected) <= 1200
    assert len(selected.splitlines()) <= 40


def test_derived_report_excerpt_and_findings_are_bounded(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    report = "# Preflight Report\n" + "\n".join(
        f"- warning: finding {index} " + ("x" * 60) for index in range(30)
    )
    _write_repository_file(root, f".harness/tasks/{SLUG}/preflight.md", report)

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())
    selected = _selected(result, "preflight")

    assert len(selected.selected_content) <= 900
    assert len(selected.selected_content.splitlines()) <= 40
    assert len(selected.report_findings) == 20
    assert selected.report_findings[0].startswith("warning: finding 0")
    assert selected.report_findings[-1].startswith("warning: finding 19")


def test_extracted_report_findings_are_redacted(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(
        root,
        f".harness/tasks/{SLUG}/preflight.md",
        "# Preflight Report\n\n- warning: leaked sk-abcdefghij\n",
    )

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    findings = _selected(result, "preflight").report_findings
    assert findings == ("warning: leaked [REDACTED]",)


@pytest.mark.parametrize(
    ("entry_id", "filename", "title"),
    [
        ("spec", "spec.md", "# Specification"),
        ("preflight", "preflight.md", "# Preflight Report"),
        (
            "test_contract_review",
            "test-contract-review.md",
            "# Test-Contract Readiness Review",
        ),
    ],
)
def test_each_canonical_known_report_title_is_accepted(
    tmp_path,
    entry_id,
    filename,
    title,
):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(
        root,
        f".harness/tasks/{SLUG}/{filename}",
        f"{title}\r\n\r\n- info: canonical report\r\n",
    )

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert entry_id in {entry.entry_id for entry in result.selected_entries}


@pytest.mark.parametrize(
    "content",
    [
        "# Arbitrary Notes\n\n# Preflight Report\n",
        "# Specification\n",
        "# Wrong First Heading\n\n# Preflight Report\n",
    ],
)
def test_preflight_rejects_noncanonical_first_report_title(tmp_path, content):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(root, f".harness/tasks/{SLUG}/preflight.md", content)

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert "preflight" not in {entry.entry_id for entry in result.selected_entries}
    finding = next(
        item
        for item in result.findings
        if item.code == "malformed_known_report" and item.path.endswith("preflight.md")
    )
    assert finding.level == FindingLevel.WARNING


@pytest.mark.parametrize("prefix", ["\n\n", "\ufeff"])
def test_preflight_accepts_leading_blanks_or_initial_bom(prefix, tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(
        root,
        f".harness/tasks/{SLUG}/preflight.md",
        f"{prefix}# Preflight Report\n\n- info: accepted\n",
    )

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert "preflight" in {entry.entry_id for entry in result.selected_entries}


def test_malformed_known_report_is_excluded_with_warning(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(root, f".harness/tasks/{SLUG}/preflight.md", "not a known report")

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert "preflight" not in {entry.entry_id for entry in result.selected_entries}
    assert "malformed_known_report" in _finding_codes(result)


def test_malformed_requirements_yaml_is_excluded(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(root, f".harness/tasks/{SLUG}/requirements.yaml", "requirements: [\n")

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert "requirements_projection" not in {entry.entry_id for entry in result.selected_entries}
    assert "malformed_requirements_projection" in _finding_codes(result)


def test_malformed_requirements_schema_is_excluded(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(
        root,
        f".harness/tasks/{SLUG}/requirements.yaml",
        "schema_version: 1\nrequirements: arbitrary\n",
    )

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert "requirements_projection" not in {entry.entry_id for entry in result.selected_entries}
    assert "malformed_requirements_projection" in _finding_codes(result)


def test_structured_requirements_selection_is_bounded(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    requirements = build_requirements_document(
        SLUG,
        "\n".join(f"- requirement {index} " + ("x" * 80) for index in range(50)),
    )
    _write_repository_file(
        root,
        f".harness/tasks/{SLUG}/requirements.yaml",
        render_requirements_yaml(requirements),
    )

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())
    selected = _selected(result, "requirements_projection").selected_content

    assert len(selected) <= 900
    assert len(selected.splitlines()) <= 40
    assert "REQ-001" in selected


def test_registry_mutation_cannot_select_context_manifest_output(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    entry = _registry_entry(
        "context_manifest_output",
        context_manifest_path(SLUG),
        classification=Classification.DERIVED,
        authority=AuthorityLevel.DERIVED_PROJECTION,
        inclusion=InclusionMode.READ_IF_NEEDED,
    )

    with pytest.raises(ContextValidationError, match="generated task output cannot be selected"):
        select_context(root, SLUG, derived_freshness={}, registry=(entry,))


def test_selected_entry_cannot_depend_on_context_manifest_output(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    entries = (
        _registry_entry(
            "input",
            ".harness/custom/input.md",
            dependencies=("context_manifest_output",),
        ),
        _registry_entry(
            "context_manifest_output",
            context_manifest_path(SLUG),
            classification=Classification.DERIVED,
            authority=AuthorityLevel.DERIVED_PROJECTION,
            inclusion=InclusionMode.EXCLUDED,
            order=20,
            exclusion_reason="self_generated_output_not_input",
        ),
    )

    with pytest.raises(ContextValidationError, match="cannot depend on protected output"):
        select_context(root, SLUG, derived_freshness={}, registry=entries)


def test_duplicate_selected_paths_are_rejected_before_inspection(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    path = ".harness/custom/shared.md"
    entries = (
        _registry_entry("one", path),
        _registry_entry("two", path, order=20),
    )

    with pytest.raises(ContextValidationError, match="duplicate context registry path"):
        select_context(root, SLUG, derived_freshness={}, registry=entries)


def test_multibyte_source_uses_exact_raw_bytes_and_decoded_characters(tmp_path):
    result = _single_source_selection(tmp_path, "é\r\n".encode("utf-8"))
    size = _context_entry(result, "input").size_estimate

    assert size.source.bytes == 4
    assert size.source.characters == 3
    assert size.source.lines == 1
    assert size.source.approximate_tokens == 1
    assert size.selected.bytes == 2
    assert size.selected.characters == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("a\n", 1),
        ("a\r\n", 1),
        ("a\nb", 2),
        ("a\r\nb\r", 2),
        ("a\n\nb\n", 3),
    ],
)
def test_logical_line_count_normalizes_endings_and_terminal_newline(text, expected):
    assert _logical_line_count(text) == expected


@pytest.mark.parametrize(
    ("characters", "expected"),
    [(0, 0), (1, 1), (4, 1), (5, 2)],
)
def test_approximate_token_estimate_uses_ceiling(characters, expected):
    assert _approximate_tokens(characters) == expected


def test_unicode_token_estimate_uses_python_character_count():
    assert len("😀😀😀😀😀") == 5
    assert _approximate_tokens(len("😀😀😀😀😀")) == 2


def test_selected_measurement_occurs_after_redaction(tmp_path):
    result = _single_source_selection(tmp_path, "sk-abcdefghij")
    selected = _selected(result, "input").selected_content
    size = _context_entry(result, "input").size_estimate

    assert selected == "[REDACTED]"
    assert size.source.characters == len("sk-abcdefghij")
    assert size.selected.characters == len("[REDACTED]")
    assert size.selected.bytes == len("[REDACTED]".encode("utf-8"))


def test_source_measures_full_content_while_selected_measures_bounded_content(tmp_path):
    content = "x" * 2000
    result = _single_source_selection(tmp_path, content)
    selected = _selected(result, "input").selected_content
    size = _context_entry(result, "input").size_estimate

    assert size.source.bytes == 2000
    assert size.source.characters == 2000
    assert len(selected) == 1200
    assert size.selected.characters == len(selected)
    assert size.selected.bytes == len(selected.encode("utf-8"))
    assert size.selected.approximate_tokens == 300


@pytest.mark.parametrize("state", [FreshnessState.STALE, FreshnessState.UNKNOWN])
def test_nonfresh_derived_context_entry_has_zero_measurements(tmp_path, state):
    root = _selection_repository(tmp_path, include_derived=True)
    freshness = _fresh_derived()
    freshness["spec"] = state

    result = select_context(root, SLUG, derived_freshness=freshness)
    entry = _context_entry(result, "spec")

    assert entry.size_estimate == SizeEstimate(source=_values(), selected=_values())


def test_missing_unreadable_excluded_and_unavailable_entries_have_zero_measurements(tmp_path):
    root = _selection_repository(tmp_path)
    (root / ".harness/tasks" / SLUG / "task.md").unlink()
    _write_repository_file(root, f".harness/tasks/{SLUG}/acceptance.md", b"\xff")
    _write_repository_file(root, agent_workset_path(SLUG), "excluded output")

    result = select_context(root, SLUG, derived_freshness={})

    for entry_id in ("task", "acceptance", "agent_workset_output", "repository_signals"):
        entry = _context_entry(result, entry_id)
        assert entry.size_estimate.source == _values()
        assert entry.size_estimate.selected == _values()


def test_context_result_has_one_measured_entry_per_registry_entry_in_order(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)

    result = select_context(root, SLUG, derived_freshness=_fresh_derived())

    assert len(result.context_entries) == len(result.registry)
    assert [item.entry_id for item in result.context_entries] == [
        item.entry_id for item in result.registry
    ]
    assert [(item.order, item.entry_id) for item in result.context_entries] == sorted(
        (item.order, item.entry_id) for item in result.context_entries
    )


def test_available_observations_use_same_canonical_source_and_selected_measurements(tmp_path):
    root = _selection_repository(tmp_path)

    result = select_context(
        root,
        SLUG,
        derived_freshness={},
        repository_observations=_observations(),
    )
    artifact = next(item for item in result.artifacts if item.entry_id == "repository_signals")
    selected = _selected(result, "repository_signals")
    measured = _context_entry(result, "repository_signals").size_estimate

    assert artifact.source_content == selected.selected_content
    assert measured.source == measured.selected
    assert measured.source == artifact.source_size


def test_default_budget_configuration_is_applied(tmp_path):
    result = _single_source_selection(tmp_path, "small")

    assert result.budget.configuration == _budget_configuration()
    assert result.budget.result.status == BudgetStatus.WITHIN_BUDGET


def test_valid_custom_budget_configuration_is_preserved(tmp_path):
    configuration = _budget_configuration(
        per_file_warning=10,
        per_file_high_risk=20,
        total_warning=30,
        total_high_risk=40,
    )

    result = _single_source_selection(tmp_path, "small", configuration=configuration)

    assert result.budget.configuration == configuration


@pytest.mark.parametrize(
    "configuration",
    [
        _budget_configuration(per_file_warning=2, per_file_high_risk=2),
        _budget_configuration(total_warning=2, total_high_risk=2),
    ],
)
def test_invalid_custom_budget_threshold_ordering_is_rejected(tmp_path, configuration):
    with pytest.raises(ContextValidationError, match="high-risk threshold must exceed"):
        _single_source_selection(tmp_path, "small", configuration=configuration)


def test_exact_per_file_warning_boundary_is_advisory_warning(tmp_path):
    configuration = _budget_configuration(
        per_file_warning=1,
        per_file_high_risk=2,
        total_warning=100,
        total_high_risk=200,
    )

    result = _single_source_selection(tmp_path, "a", configuration=configuration)

    assert result.budget.result.status == BudgetStatus.WARNING
    finding = next(item for item in result.findings if item.code == "context_file_budget_warning")
    assert finding.path == ".harness/custom/input.md"
    assert "input" in finding.message
    assert "estimate of 1" in finding.message
    assert "threshold of 1" in finding.message
    assert "advisory model-agnostic" in finding.message
    assert "context_within_budget" not in _finding_codes(result)


def test_exact_per_file_high_risk_boundary_suppresses_lower_warning(tmp_path):
    configuration = _budget_configuration(
        per_file_warning=1,
        per_file_high_risk=2,
        total_warning=100,
        total_high_risk=200,
    )

    result = _single_source_selection(tmp_path, "abcde", configuration=configuration)
    codes = _finding_codes(result)

    assert result.budget.result.status == BudgetStatus.HIGH_RISK
    assert "context_file_budget_high_risk" in codes
    assert "context_file_budget_warning" not in codes
    assert "context_within_budget" not in codes


def test_exact_total_warning_boundary_is_advisory_warning(tmp_path):
    configuration = _budget_configuration(
        per_file_warning=100,
        per_file_high_risk=200,
        total_warning=1,
        total_high_risk=2,
    )

    result = _single_source_selection(tmp_path, "a", configuration=configuration)

    assert result.budget.result.status == BudgetStatus.WARNING
    assert "context_total_budget_warning" in _finding_codes(result)
    assert "context_within_budget" not in _finding_codes(result)


def test_exact_total_high_risk_boundary_suppresses_lower_warning(tmp_path):
    configuration = _budget_configuration(
        per_file_warning=100,
        per_file_high_risk=200,
        total_warning=1,
        total_high_risk=2,
    )

    result = _single_source_selection(tmp_path, "abcde", configuration=configuration)
    codes = _finding_codes(result)

    assert result.budget.result.status == BudgetStatus.HIGH_RISK
    assert "context_total_budget_high_risk" in codes
    assert "context_total_budget_warning" not in codes
    assert "context_within_budget" not in codes


def test_budget_totals_equal_sum_of_selected_entry_measurements(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    result = select_context(
        root,
        SLUG,
        derived_freshness=_fresh_derived(),
        repository_observations=_observations(),
    )
    selected_sizes = [
        item.size_estimate.selected
        for item in result.context_entries
        if item.size_estimate.selected != _values()
    ]

    assert result.budget.result.selected_entry_count == len(selected_sizes)
    assert result.budget.result.selected_bytes == sum(item.bytes for item in selected_sizes)
    assert result.budget.result.selected_characters == sum(
        item.characters for item in selected_sizes
    )
    assert result.budget.result.selected_lines == sum(item.lines for item in selected_sizes)
    assert result.budget.result.selected_approximate_tokens == sum(
        item.approximate_tokens for item in selected_sizes
    )


def test_excluded_and_unselected_entries_do_not_contribute_to_budget(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    _write_repository_file(root, agent_workset_path(SLUG), "x" * 500)
    freshness = _fresh_derived()
    freshness["spec"] = FreshnessState.STALE

    result = select_context(root, SLUG, derived_freshness=freshness)

    assert _context_entry(result, "agent_workset_output").size_estimate.selected == _values()
    assert _context_entry(result, "spec").size_estimate.selected == _values()
    assert result.budget.result.selected_entry_count == sum(
        item.size_estimate.selected != _values() for item in result.context_entries
    )


def test_within_budget_adds_informational_finding(tmp_path):
    result = _single_source_selection(tmp_path, "small")
    findings = [item for item in result.findings if item.code == "context_within_budget"]

    assert len(findings) == 1
    assert findings[0].level == FindingLevel.INFO


def test_direct_manifest_rejects_inconsistent_source_approximate_tokens():
    document = _realistic_document()
    task = next(item for item in document.context_entries if item.entry_id == "task")
    invalid_source = replace(task.size_estimate.source, approximate_tokens=3)
    invalid_task = replace(
        task,
        size_estimate=replace(task.size_estimate, source=invalid_source),
    )
    entries = tuple(
        invalid_task if item.entry_id == "task" else item
        for item in document.context_entries
    )

    with pytest.raises(
        ContextValidationError,
        match=r"task\.size_estimate\.source\.approximate_tokens must equal ceil",
    ):
        validate_context_manifest(replace(document, context_entries=entries))


def test_direct_manifest_rejects_inconsistent_selected_approximate_tokens():
    document = _realistic_document()
    task = next(item for item in document.context_entries if item.entry_id == "task")
    invalid_selected = replace(task.size_estimate.selected, approximate_tokens=2)
    invalid_task = replace(
        task,
        size_estimate=replace(task.size_estimate, selected=invalid_selected),
    )
    entries = tuple(
        invalid_task if item.entry_id == "task" else item
        for item in document.context_entries
    )

    with pytest.raises(
        ContextValidationError,
        match=r"task\.size_estimate\.selected\.approximate_tokens must equal ceil",
    ):
        validate_context_manifest(replace(document, context_entries=entries))


def test_parsed_yaml_rejects_inconsistent_approximate_tokens():
    data = _rendered_data()
    task = next(item for item in data["context_entries"] if item["entry_id"] == "task")
    task["size_estimate"]["source"]["approximate_tokens"] = 99

    with pytest.raises(
        ContextValidationError,
        match=r"task\.size_estimate\.source\.approximate_tokens must equal ceil",
    ):
        _parse_data(data)


def test_zero_characters_requires_zero_approximate_tokens():
    document = _realistic_document()
    task = next(item for item in document.context_entries if item.entry_id == "task")
    invalid_source = SizeValues(bytes=0, characters=0, lines=0, approximate_tokens=1)
    invalid_task = replace(
        task,
        size_estimate=replace(task.size_estimate, source=invalid_source),
    )
    entries = tuple(
        invalid_task if item.entry_id == "task" else item
        for item in document.context_entries
    )

    with pytest.raises(ContextValidationError, match=r"expected 0"):
        validate_context_manifest(replace(document, context_entries=entries))


@pytest.mark.parametrize(
    ("content", "expected_bytes", "expected_characters", "expected_tokens"),
    [
        ("abcd", 4, 4, 1),
        ("éééé", 8, 4, 1),
    ],
)
def test_valid_ascii_and_unicode_selector_measurements_pass_invariant(
    tmp_path,
    content,
    expected_bytes,
    expected_characters,
    expected_tokens,
):
    result = _single_source_selection(tmp_path, content)
    source = _context_entry(result, "input").size_estimate.source

    assert source.bytes == expected_bytes
    assert source.characters == expected_characters
    assert source.approximate_tokens == expected_tokens


def test_selector_produced_measurements_all_satisfy_token_invariant(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    result = select_context(
        root,
        SLUG,
        derived_freshness=_fresh_derived(),
        repository_observations=_observations(),
    )

    for entry in result.context_entries:
        for size in (entry.size_estimate.source, entry.size_estimate.selected):
            assert size.approximate_tokens == _approximate_tokens(size.characters)


def test_selection_result_rejects_missing_inspection_artifact(tmp_path):
    result = _single_source_selection(tmp_path, "selected")

    with pytest.raises(ContextValidationError, match="one inspection artifact per registry entry"):
        replace(result, artifacts=())


@pytest.mark.parametrize("artifacts", ["duplicate", "reordered"])
def test_selection_result_rejects_duplicate_or_reordered_artifacts(tmp_path, artifacts):
    root = _selection_repository(tmp_path)
    result = select_context(root, SLUG, derived_freshness={})
    changed = (
        (result.artifacts[0], result.artifacts[0], *result.artifacts[2:])
        if artifacts == "duplicate"
        else tuple(reversed(result.artifacts))
    )

    with pytest.raises(ContextValidationError, match="one inspection artifact per registry entry"):
        replace(result, artifacts=changed)


def test_selection_result_rejects_artifact_context_existence_mismatch(tmp_path):
    result = _single_source_selection(tmp_path, "selected")
    artifacts = _replace_result_item(
        result.artifacts,
        "input",
        existence=ExistenceState.MISSING,
        source_content=None,
        source_size=SizeValues(0, 0, 0, 0),
        source_sha256=None,
    )

    with pytest.raises(ContextValidationError, match="artifact/context existence mismatch"):
        replace(result, artifacts=artifacts)


def test_selection_result_rejects_artifact_context_source_size_mismatch(tmp_path):
    result = _single_source_selection(tmp_path, "selected")
    artifacts = _replace_result_item(result.artifacts, "input", source_size=_values(1))

    with pytest.raises(ContextValidationError, match="source-content measurement mismatch"):
        replace(result, artifacts=artifacts)


def test_selection_result_rejects_selected_entry_without_selected_measurement(tmp_path):
    result = _single_source_selection(tmp_path, "selected")
    context_entries = _replace_result_item(
        result.context_entries,
        "input",
        size_estimate=replace(result.context_entries[0].size_estimate, selected=_values()),
    )

    with pytest.raises(ContextValidationError, match="correspond exactly to nonzero selected measurements"):
        replace(result, context_entries=context_entries)


def test_selection_result_rejects_selected_measurement_without_selected_entry(tmp_path):
    result = _single_source_selection(tmp_path, "selected")

    with pytest.raises(ContextValidationError, match="correspond exactly to nonzero selected measurements"):
        replace(result, selected_entries=())


def test_selection_result_rejects_selected_content_measurement_mismatch(tmp_path):
    result = _single_source_selection(tmp_path, "selected")
    selected_entries = _replace_result_item(
        result.selected_entries,
        "input",
        selected_content="different selected content",
    )

    with pytest.raises(ContextValidationError, match="selected-content measurement mismatch"):
        replace(result, selected_entries=selected_entries)


@pytest.mark.parametrize(
    ("existence", "freshness", "message"),
    [
        (ExistenceState.PRESENT, FreshnessState.STALE, "non-fresh entry"),
        (ExistenceState.MISSING, FreshnessState.NOT_APPLICABLE, "missing or unreadable entry"),
        (ExistenceState.UNREADABLE, FreshnessState.NOT_APPLICABLE, "missing or unreadable entry"),
    ],
)
def test_selection_result_rejects_nonselectable_entry_state(
    tmp_path,
    existence,
    freshness,
    message,
):
    result = _single_source_selection(tmp_path, "selected")
    artifacts = _replace_result_item(
        result.artifacts,
        "input",
        existence=existence,
        freshness_state=freshness,
        **(
            {
                "source_content": None,
                "source_size": SizeValues(0, 0, 0, 0),
                "source_sha256": None,
            }
            if existence != ExistenceState.PRESENT
            else {}
        ),
    )
    context_entries = _replace_result_item(
        result.context_entries,
        "input",
        existence=existence,
        freshness_state=freshness,
        **(
            {
                "size_estimate": replace(
                    result.context_entries[0].size_estimate,
                    source=SizeValues(0, 0, 0, 0),
                )
            }
            if existence != ExistenceState.PRESENT
            else {}
        ),
    )
    selected_entries = _replace_result_item(
        result.selected_entries,
        "input",
        freshness_state=freshness,
        **(
            {"source_content": None}
            if existence != ExistenceState.PRESENT
            else {}
        ),
    )

    with pytest.raises(ContextValidationError, match=message):
        replace(
            result,
            artifacts=artifacts,
            context_entries=context_entries,
            selected_entries=selected_entries,
        )


def test_selection_result_rejects_excluded_entry_in_selected_entries(tmp_path):
    root = _selection_repository(tmp_path)
    _write_repository_file(root, agent_workset_path(SLUG), "workset")
    result = select_context(root, SLUG, derived_freshness={})
    workset_artifact = next(
        item for item in result.artifacts if item.entry_id == "agent_workset_output"
    )
    selected = replace(
        result.selected_entries[0],
        entry_id=workset_artifact.entry_id,
        path=workset_artifact.path,
        classification=workset_artifact.classification,
        authority_level=workset_artifact.authority_level,
        inclusion_mode=workset_artifact.inclusion_mode,
        order=workset_artifact.order,
        freshness_state=workset_artifact.freshness_state,
        source_content="workset",
        selected_content="workset",
    )
    selected_size = _values(len("workset"))
    context_entries = _replace_result_item(
        result.context_entries,
        "agent_workset_output",
        size_estimate=SizeEstimate(source=_values(), selected=selected_size),
    )

    with pytest.raises(ContextValidationError, match="excluded context entry"):
        replace(
            result,
            selected_entries=(*result.selected_entries, selected),
            context_entries=context_entries,
        )


def test_current_selector_result_revalidates_without_change(tmp_path):
    result = _single_source_selection(tmp_path, "selected")

    assert replace(result) == result


@pytest.mark.parametrize(
    "field_name",
    ["artifacts", "selected_entries", "context_entries", "findings"],
)
def test_selection_result_rejects_none_collection_items_cleanly(tmp_path, field_name):
    result = _single_source_selection(tmp_path, "selected")

    with pytest.raises(ContextValidationError, match=f"{field_name} items must use"):
        replace(result, **{field_name: (None,)})


@pytest.mark.parametrize(
    "field_name",
    ["registry", "artifacts", "selected_entries", "context_entries", "findings"],
)
def test_selection_result_requires_tuple_collections(tmp_path, field_name):
    result = _single_source_selection(tmp_path, "selected")

    with pytest.raises(ContextValidationError, match=f"{field_name} must be a tuple"):
        replace(result, **{field_name: list(getattr(result, field_name))})


def test_selection_result_rejects_invalid_finding_content(tmp_path):
    result = _single_source_selection(tmp_path, "selected")
    invalid_finding = replace(result.findings[0], code="invalid finding code")

    with pytest.raises(ContextValidationError, match="finding code"):
        replace(result, findings=(invalid_finding,))


def test_selection_result_rejects_unsorted_findings(tmp_path):
    result = _single_source_selection(tmp_path, "selected")
    findings = (
        ContextFinding(
            code="later_info",
            level=FindingLevel.INFO,
            path=None,
            message="Info sorts after blockers.",
        ),
        ContextFinding(
            code="earlier_blocker",
            level=FindingLevel.BLOCKER,
            path=None,
            message="Blockers sort first.",
        ),
    )

    with pytest.raises(ContextValidationError, match="findings must be in deterministic order"):
        replace(result, findings=findings)


def test_programmer_created_inconsistent_budget_totals_are_rejected(tmp_path):
    result = _single_source_selection(tmp_path, "small")
    inconsistent_result = replace(
        result.budget.result,
        selected_bytes=result.budget.result.selected_bytes + 1,
    )

    with pytest.raises(ContextValidationError, match="selected_bytes does not equal"):
        replace(result, budget=replace(result.budget, result=inconsistent_result))


def test_measured_selection_budget_and_findings_are_repeatable(tmp_path):
    root = _selection_repository(tmp_path, include_derived=True)
    configuration = _budget_configuration(
        per_file_warning=20,
        per_file_high_risk=40,
        total_warning=100,
        total_high_risk=200,
    )

    first = select_context(
        root,
        SLUG,
        derived_freshness=_fresh_derived(),
        repository_observations=_observations(),
        budget_configuration=configuration,
    )
    second = select_context(
        root,
        SLUG,
        derived_freshness=_fresh_derived(),
        repository_observations=_observations(),
        budget_configuration=configuration,
    )

    assert first == second
