from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import pytest

from ai_sdlc_harness.constants import TASK_ARTIFACT_FILENAMES
from ai_sdlc_harness.context import (
    Classification,
    ContextFinding,
    FindingLevel,
    FreshnessState,
    build_context_registry,
    select_context,
)
from ai_sdlc_harness.files import PathSafetyError, sha256_bytes
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.lineage import (
    CapturedDependency,
    InspectedDependencyState,
    InspectedObservationState,
    InspectedProvenanceState,
    LineageResolutionResult,
    LineageValidationError,
    build_provenance_record,
    capture_dependencies,
    decide_freshness,
    inspect_provenance_state,
    lineage_definition,
    lineage_definition_for_task,
    lineage_definitions,
    lineage_definitions_for_task,
    provenance_dependencies,
    resolve_derived_freshness,
    snapshot_dependencies,
    snapshot_repository_observations,
    validate_manifest_lineage_provenance,
    validate_manifest_provenance,
    validate_provenance_record,
)
from ai_sdlc_harness.manifest import (
    GeneratedArtifactProvenance,
    ManifestV1,
    ManifestV2,
    ProvenanceDependency,
    ProvenanceObservation,
    build_manifest_model,
    load_manifest_model,
    parse_manifest_json,
    persist_manifest_model,
    remove_provenance_records,
    render_manifest_json,
    sha256_file,
)
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.spec import run_spec
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.test_contract import run_test_contract_review
from ai_sdlc_harness.validate import run_validate
from ai_sdlc_harness.validation import (
    validation_dependency_paths,
    validation_observation_definitions,
    validation_provenance_definition_for_task,
)


SLUG = "lineage-task"
OUTPUT_HASH = "a" * 64


def _dependencies(expected):
    return tuple(
        ProvenanceDependency(path, "present", "b" * 64)
        for path in expected.dependencies
    )


def _observations(expected):
    return tuple(
        ProvenanceObservation(
            observation.relative_path,
            observation.predicate,
            True,
        )
        for observation in expected.repository_observations
    )


def _record(registry_id: str = "preflight") -> tuple[object, GeneratedArtifactProvenance]:
    expected = lineage_definition_for_task(registry_id, SLUG)
    record = build_provenance_record(
        expected,
        output_sha256=OUTPUT_HASH,
        dependencies=_dependencies(expected),
        repository_observations=_observations(expected),
    )
    return expected, record


def _matching_inspected(
    expected,
    record,
    *,
    output_state="present",
    output_sha256=OUTPUT_HASH,
):
    return InspectedProvenanceState(
        output_path=expected.output_path,
        output_state=output_state,
        output_sha256=(
            output_sha256 if output_state == "present" else None
        ),
        dependencies=tuple(
            InspectedDependencyState(
                dependency.dependency_path,
                dependency.dependency_state,
                dependency.dependency_sha256,
            )
            for dependency in record.dependencies
        ),
        repository_observations=tuple(
            InspectedObservationState(
                observation.observation_path,
                observation.predicate,
                "observed",
                observation.result,
            )
            for observation in record.repository_observations
        ),
    )


def _replace_inspected_dependency(inspected, index=0, **changes):
    dependencies = list(inspected.dependencies)
    dependencies[index] = replace(dependencies[index], **changes)
    return replace(inspected, dependencies=tuple(dependencies))


def _replace_recorded_dependency(record, index=0, **changes):
    dependencies = list(record.dependencies)
    dependencies[index] = replace(dependencies[index], **changes)
    return replace(record, dependencies=tuple(dependencies))


def _produce_all_lineage_outputs(root: Path) -> LineageResolutionResult:
    assert init_project(root)[0] == 0
    assert start_task(root, "Lineage Task")[0] == 0
    assert run_preflight(root, SLUG)[0] == 0
    assert run_spec(root, SLUG)[0] == 0
    assert run_test_contract_review(root, SLUG)[0] == 0
    return resolve_derived_freshness(root, SLUG)


def _repository_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None
        )
        for path in root.rglob("*")
    }


def test_closed_definitions_match_context_registry_order_and_metadata():
    definitions = lineage_definitions()
    derived_ids = tuple(
        entry.entry_id
        for entry in build_context_registry(SLUG)
        if entry.classification == Classification.DERIVED
        and entry.order < 200
    )

    assert isinstance(definitions, tuple)
    assert len(definitions) == 4
    assert tuple(definition.registry_id for definition in definitions) == derived_ids
    assert derived_ids == (
        "spec",
        "requirements_projection",
        "preflight",
        "test_contract_review",
    )
    assert tuple(definition.producer_command for definition in definitions) == (
        "spec",
        "spec",
        "preflight",
        "test-contract",
    )
    assert {definition.producer_version for definition in definitions} == {1}


def test_resolved_output_paths_and_rerun_commands_are_exact():
    definitions = lineage_definitions_for_task(SLUG)

    assert tuple(definition.output_path for definition in definitions) == (
        f".harness/tasks/{SLUG}/spec.md",
        f".harness/tasks/{SLUG}/requirements.yaml",
        f".harness/tasks/{SLUG}/preflight.md",
        f".harness/tasks/{SLUG}/test-contract-review.md",
    )
    assert tuple(definition.rerun_command for definition in definitions) == (
        f"ai-sdlc spec --task {SLUG}",
        f"ai-sdlc spec --task {SLUG}",
        f"ai-sdlc preflight --task {SLUG}",
        f"ai-sdlc test-contract --task {SLUG}",
    )
    assert lineage_definitions_for_task(SLUG) == definitions


@pytest.mark.parametrize("task_slug", ["", "../escape", "Bad-Slug", "two/slugs", "bad_slug"])
def test_invalid_task_slug_is_rejected(task_slug):
    with pytest.raises(LineageValidationError, match="safe task slug"):
        lineage_definitions_for_task(task_slug)


def test_unknown_registry_id_is_rejected():
    with pytest.raises(LineageValidationError, match="unknown Lineage registry ID"):
        lineage_definition("unknown")


def test_definitions_and_nested_collections_are_immutable():
    definitions = lineage_definitions()
    definition = definitions[0]

    assert isinstance(definition.dependencies, tuple)
    assert isinstance(definition.repository_observations, tuple)
    with pytest.raises(FrozenInstanceError):
        definition.producer_version = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        definition.dependencies[0].relative_path = "changed.md"  # type: ignore[misc]


def test_preflight_dependency_and_observation_graph_is_exact():
    expected = lineage_definition_for_task("preflight", SLUG)

    assert expected.dependencies == (
        *(
            f".harness/tasks/{SLUG}/{filename}"
            for filename in TASK_ARTIFACT_FILENAMES
        ),
        ".harness/packs/selected.yaml",
    )
    assert tuple(
        (observation.relative_path, observation.predicate)
        for observation in expected.repository_observations
    ) == (
        (".git", "exists"),
        ("pyproject.toml", "is_file"),
        ("package.json", "is_file"),
        ("Cargo.toml", "is_file"),
        ("go.mod", "is_file"),
        ("pytest.ini", "is_file"),
        ("AGENTS.md", "is_file"),
        ("CLAUDE.md", "is_file"),
        ("tests", "is_dir"),
        (".github/workflows", "is_dir"),
    )


def test_spec_outputs_share_dependencies_without_sibling_edges_or_observations():
    spec = lineage_definition_for_task("spec", SLUG)
    requirements = lineage_definition_for_task("requirements_projection", SLUG)
    expected_dependencies = (
        *(
            f".harness/tasks/{SLUG}/{filename}"
            for filename in TASK_ARTIFACT_FILENAMES
        ),
        f".harness/tasks/{SLUG}/preflight.md",
    )

    assert spec.dependencies == expected_dependencies
    assert requirements.dependencies == expected_dependencies
    assert spec.repository_observations == ()
    assert requirements.repository_observations == ()
    assert requirements.output_path not in spec.dependencies
    assert spec.output_path not in requirements.dependencies


def test_test_contract_dependency_and_observation_graph_is_exact():
    expected = lineage_definition_for_task("test_contract_review", SLUG)

    assert expected.dependencies == (
        f".harness/tasks/{SLUG}/acceptance.md",
        f".harness/tasks/{SLUG}/test-contract.md",
        f".harness/tasks/{SLUG}/verification.md",
        f".harness/tasks/{SLUG}/evidence.md",
        f".harness/tasks/{SLUG}/preflight.md",
    )
    assert tuple(
        (observation.relative_path, observation.predicate)
        for observation in expected.repository_observations
    ) == (
        ("pyproject.toml", "is_file"),
        ("package.json", "is_file"),
        ("Cargo.toml", "is_file"),
        ("go.mod", "is_file"),
        ("pytest.ini", "is_file"),
        ("tests", "is_dir"),
        (".github/workflows", "is_dir"),
    )


def test_dependency_graph_excludes_forbidden_outputs_and_is_acyclic():
    definitions = lineage_definitions_for_task(SLUG)
    forbidden = {
        ".harness/manifest.json",
        ".harness/state.json",
        ".harness/config.yaml",
        ".harness/generated/agent-instructions.md",
    }
    forbidden_filenames = {
        "agent-workset.md",
        "context-manifest.yaml",
        "evidence-report.md",
        "validation-report.md",
    }

    all_dependencies = {
        path for definition in definitions for path in definition.dependencies
    }
    assert not (all_dependencies & forbidden)
    assert not any(
        PurePosixPath(path).name in forbidden_filenames
        for path in all_dependencies
    )

    output_owner = {
        definition.output_path: definition.registry_id
        for definition in definitions
    }
    edges = {
        definition.registry_id: {
            output_owner[path]
            for path in definition.dependencies
            if path in output_owner
        }
        for definition in definitions
    }

    def visit(node, active, complete):
        if node in active:
            pytest.fail(f"Lineage cycle detected at {node}")
        if node in complete:
            return
        active.add(node)
        for dependency in edges[node]:
            visit(dependency, active, complete)
        active.remove(node)
        complete.add(node)

    complete = set()
    for registry_id in edges:
        visit(registry_id, set(), complete)


def test_build_provenance_record_is_deterministic_and_canonical():
    expected = lineage_definition_for_task("preflight", SLUG)
    dependencies = tuple(reversed(_dependencies(expected)))
    observations = tuple(reversed(_observations(expected)))

    first = build_provenance_record(
        expected,
        output_sha256=OUTPUT_HASH,
        dependencies=dependencies,
        repository_observations=observations,
    )
    second = build_provenance_record(
        expected,
        output_sha256=OUTPUT_HASH,
        dependencies=dependencies,
        repository_observations=observations,
    )

    assert first == second
    assert first.output_path == expected.output_path
    assert first.producer_command == expected.producer_command
    assert first.producer_version == expected.producer_version
    assert first.task_slug == expected.task_slug
    assert tuple(
        dependency.dependency_path for dependency in first.dependencies
    ) == tuple(sorted(expected.dependencies))
    assert tuple(
        (observation.observation_path, observation.predicate)
        for observation in first.repository_observations
    ) == tuple(
        sorted(
            (
                observation.relative_path,
                observation.predicate,
            )
            for observation in expected.repository_observations
        )
    )
    assert validate_provenance_record(first, expected) is first


def test_build_provenance_record_does_not_allow_producer_metadata_override():
    expected = lineage_definition_for_task("spec", SLUG)

    with pytest.raises(TypeError, match="unexpected keyword"):
        build_provenance_record(
            expected,
            output_sha256=OUTPUT_HASH,
            dependencies=_dependencies(expected),
            repository_observations=(),
            producer_command="other",  # type: ignore[call-arg]
        )


def test_build_provenance_record_rejects_forged_resolved_definition():
    expected = lineage_definition_for_task("spec", SLUG)

    with pytest.raises(LineageValidationError, match="closed Lineage definition"):
        build_provenance_record(
            replace(expected, producer_command="other"),
            output_sha256=OUTPUT_HASH,
            dependencies=_dependencies(expected),
            repository_observations=(),
        )


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, "", None])
def test_build_provenance_record_rejects_invalid_output_hash(digest):
    expected = lineage_definition_for_task("spec", SLUG)

    with pytest.raises(LineageValidationError, match="64 lowercase hexadecimal"):
        build_provenance_record(
            expected,
            output_sha256=digest,  # type: ignore[arg-type]
            dependencies=_dependencies(expected),
            repository_observations=(),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("output_path", ".harness/tasks/lineage-task/other.md", "output path"),
        ("producer_command", "other", "producer command"),
        ("producer_version", 2, "producer version"),
        ("task_slug", "other-task", "task slug"),
    ],
)
def test_semantic_validation_rejects_wrong_producer_metadata(field, value, message):
    expected, record = _record()

    with pytest.raises(LineageValidationError, match=message):
        validate_provenance_record(replace(record, **{field: value}), expected)


def test_semantic_validation_rejects_missing_and_extra_dependencies():
    expected, record = _record()

    with pytest.raises(LineageValidationError, match="missing expected dependency"):
        validate_provenance_record(
            replace(record, dependencies=record.dependencies[1:]),
            expected,
        )

    extra = ProvenanceDependency("unexpected.md", "missing", None)
    with pytest.raises(LineageValidationError, match="unexpected dependency"):
        validate_provenance_record(
            replace(
                record,
                dependencies=tuple(
                    sorted(
                        (*record.dependencies, extra),
                        key=lambda dependency: dependency.dependency_path,
                    )
                ),
            ),
            expected,
        )


def test_semantic_validation_rejects_missing_and_extra_observations():
    expected, record = _record()

    with pytest.raises(
        LineageValidationError,
        match="missing expected repository observation",
    ):
        validate_provenance_record(
            replace(
                record,
                repository_observations=record.repository_observations[1:],
            ),
            expected,
        )

    extra = ProvenanceObservation("unexpected", "exists", False)
    with pytest.raises(
        LineageValidationError,
        match="unexpected repository observation",
    ):
        validate_provenance_record(
            replace(
                record,
                repository_observations=tuple(
                    sorted(
                        (*record.repository_observations, extra),
                        key=lambda observation: (
                            observation.observation_path,
                            observation.predicate,
                        ),
                    )
                ),
            ),
            expected,
        )


def test_semantic_validation_requires_canonical_record_order():
    expected, record = _record()

    with pytest.raises(LineageValidationError, match="canonical path order"):
        validate_provenance_record(
            replace(record, dependencies=tuple(reversed(record.dependencies))),
            expected,
        )
    with pytest.raises(LineageValidationError, match="canonical path/predicate order"):
        validate_provenance_record(
            replace(
                record,
                repository_observations=tuple(
                    reversed(record.repository_observations)
                ),
            ),
            expected,
        )


def test_structurally_accepted_unknown_producer_is_rejected_only_by_lineage(project_tmp):
    expected = lineage_definition_for_task("spec", SLUG)
    output = project_tmp / PurePosixPath(expected.output_path)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"spec output")
    document = build_manifest_model(
        project_tmp,
        extra_managed_paths=(PurePosixPath(expected.output_path),),
    )
    assert isinstance(document, ManifestV2)

    valid = build_provenance_record(
        expected,
        output_sha256=sha256_file(output),
        dependencies=_dependencies(expected),
        repository_observations=(),
    )
    unknown = replace(valid, producer_command="structural-test-producer")
    structural_document = replace(
        document,
        generated_artifact_provenance=(unknown,),
    )

    parsed = parse_manifest_json(render_manifest_json(structural_document))

    assert parsed.generated_artifact_provenance[0].producer_command == (
        "structural-test-producer"
    )
    with pytest.raises(LineageValidationError, match="producer command"):
        validate_provenance_record(
            parsed.generated_artifact_provenance[0],
            expected,
        )


def test_manifest_wide_validator_accepts_exact_validation_provenance(project_tmp):
    expected = validation_provenance_definition_for_task(SLUG)
    output = project_tmp / PurePosixPath(expected.output_path)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"validation output")
    document = build_manifest_model(
        project_tmp,
        extra_managed_paths=(PurePosixPath(expected.output_path),),
    )
    assert isinstance(document, ManifestV2)
    record = build_provenance_record(
        expected,
        output_sha256=sha256_file(output),
        dependencies=_dependencies(expected),
        repository_observations=_observations(expected),
    )
    with_provenance = replace(
        document,
        generated_artifact_provenance=(record,),
    )

    assert validate_manifest_provenance(with_provenance) is with_provenance
    assert validate_manifest_lineage_provenance(with_provenance) is with_provenance
    assert record.producer_command == "validate"
    assert record.producer_version == 1
    assert tuple(item.dependency_path for item in record.dependencies) == tuple(
        sorted(path.as_posix() for path in validation_dependency_paths(SLUG))
    )
    assert tuple(
        (item.observation_path, item.predicate)
        for item in record.repository_observations
    ) == tuple(
        sorted(
            (path.as_posix(), predicate.value)
            for path, predicate in validation_observation_definitions(SLUG)
        )
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("wrong_producer", "producer command"),
        ("wrong_task_slug", "closed producer definition"),
        ("wrong_output", "closed producer definition"),
        ("missing_dependency", "missing expected dependency"),
        ("extra_dependency", "unexpected dependency"),
        ("wrong_dependency_order", "canonical path order"),
        ("missing_observation", "missing expected repository observation"),
        ("extra_observation", "unexpected repository observation"),
        ("wrong_observation_order", "canonical path/predicate order"),
    ],
)
def test_manifest_wide_validator_rejects_invalid_validation_definition(
    project_tmp,
    change,
    message,
):
    expected = validation_provenance_definition_for_task(SLUG)
    output = project_tmp / PurePosixPath(expected.output_path)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"validation output")
    document = build_manifest_model(
        project_tmp,
        extra_managed_paths=(PurePosixPath(expected.output_path),),
    )
    assert isinstance(document, ManifestV2)
    record = build_provenance_record(
        expected,
        output_sha256=sha256_file(output),
        dependencies=_dependencies(expected),
        repository_observations=_observations(expected),
    )
    if change == "wrong_producer":
        changed = replace(record, producer_command="spec")
    elif change == "wrong_task_slug":
        changed = replace(record, task_slug="other-task")
    elif change == "wrong_output":
        changed = replace(
            record,
            output_path=f".harness/tasks/{SLUG}/evidence-report.md",
        )
    elif change == "missing_dependency":
        changed = replace(record, dependencies=record.dependencies[1:])
    elif change == "extra_dependency":
        changed = replace(
            record,
            dependencies=tuple(
                sorted(
                    (*record.dependencies, ProvenanceDependency("extra.md", "missing", None)),
                    key=lambda item: item.dependency_path,
                )
            ),
        )
    elif change == "wrong_dependency_order":
        changed = replace(record, dependencies=tuple(reversed(record.dependencies)))
    elif change == "missing_observation":
        changed = replace(
            record,
            repository_observations=record.repository_observations[1:],
        )
    elif change == "extra_observation":
        changed = replace(
            record,
            repository_observations=tuple(
                sorted(
                    (
                        *record.repository_observations,
                        ProvenanceObservation("extra", "exists", False),
                    ),
                    key=lambda item: (item.observation_path, item.predicate),
                )
            ),
        )
    else:
        changed = replace(
            record,
            repository_observations=tuple(
                reversed(record.repository_observations)
            ),
        )
    with_provenance = replace(
        document,
        generated_artifact_provenance=(changed,),
    )

    with pytest.raises(LineageValidationError, match=message):
        validate_manifest_provenance(with_provenance)


def test_invalid_validation_provenance_does_not_change_selector_resolution(project_tmp):
    produced = _produce_all_lineage_outputs(project_tmp)
    assert set(produced.derived_freshness.values()) == {FreshnessState.FRESH}
    assert run_validate(project_tmp, SLUG)[0] == 0
    manifest = load_manifest_model(project_tmp / ".harness" / "manifest.json")
    assert isinstance(manifest, ManifestV2)
    records = tuple(
        replace(record, producer_command="invalid-validation-producer")
        if record.output_path.endswith("/validation-report.md")
        else record
        for record in manifest.generated_artifact_provenance
    )
    persist_manifest_model(
        project_tmp,
        replace(manifest, generated_artifact_provenance=records),
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert tuple(result.derived_freshness) == (
        "spec",
        "requirements_projection",
        "preflight",
        "test_contract_review",
    )
    assert set(result.derived_freshness.values()) == {FreshnessState.FRESH}
    assert result.blockers == ()
    selection = select_context(
        project_tmp,
        SLUG,
        derived_freshness=result.derived_freshness,
    )
    assert not any(
        item.path and item.path.endswith("/validation-report.md")
        for item in selection.selected_entries
    )


def test_pure_construction_does_not_read_or_write_files(tmp_path):
    expected = lineage_definition_for_task("spec", SLUG)

    record = build_provenance_record(
        expected,
        output_sha256=OUTPUT_HASH,
        dependencies=_dependencies(expected),
        repository_observations=(),
    )

    assert record.output_path == expected.output_path
    assert list(tmp_path.iterdir()) == []


def test_dependency_snapshot_records_exact_states_hashes_and_order(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("preflight", SLUG)
    task_root = project_tmp / ".harness" / "tasks" / SLUG
    task_root.mkdir(parents=True)
    task_bytes = b"line one\r\nline two\r\n"
    (task_root / "task.md").write_bytes(task_bytes)
    (task_root / "acceptance.md").mkdir()
    unreadable = task_root / "architecture-notes.md"
    unreadable.write_bytes(b"unreadable")

    real_read_bytes = Path.read_bytes

    def controlled_read_bytes(path):
        if path == unreadable:
            raise PermissionError("simulated unreadable file")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", controlled_read_bytes)

    snapshot = snapshot_dependencies(project_tmp, expected)
    by_path = {record.dependency_path: record for record in snapshot}

    assert tuple(record.dependency_path for record in snapshot) == tuple(
        sorted(expected.dependencies)
    )
    assert by_path[f".harness/tasks/{SLUG}/task.md"] == ProvenanceDependency(
        f".harness/tasks/{SLUG}/task.md",
        "present",
        sha256_bytes(task_bytes),
    )
    assert by_path[f".harness/tasks/{SLUG}/acceptance.md"] == ProvenanceDependency(
        f".harness/tasks/{SLUG}/acceptance.md",
        "not_regular",
        None,
    )
    assert by_path[
        f".harness/tasks/{SLUG}/architecture-notes.md"
    ] == ProvenanceDependency(
        f".harness/tasks/{SLUG}/architecture-notes.md",
        "unreadable",
        None,
    )
    assert by_path[
        f".harness/tasks/{SLUG}/coupling-notes.md"
    ] == ProvenanceDependency(
        f".harness/tasks/{SLUG}/coupling-notes.md",
        "missing",
        None,
    )


def test_dependency_snapshot_distinguishes_lf_and_crlf(project_tmp):
    expected = lineage_definition_for_task("spec", SLUG)
    task_root = project_tmp / ".harness" / "tasks" / SLUG
    task_root.mkdir(parents=True)
    (task_root / "task.md").write_bytes(b"same\n")
    (task_root / "acceptance.md").write_bytes(b"same\r\n")

    snapshot = snapshot_dependencies(project_tmp, expected)
    by_path = {record.dependency_path: record for record in snapshot}

    assert (
        by_path[f".harness/tasks/{SLUG}/task.md"].dependency_sha256
        != by_path[f".harness/tasks/{SLUG}/acceptance.md"].dependency_sha256
    )


def test_dependency_capture_binds_records_to_exact_raw_bytes(project_tmp):
    expected = lineage_definition_for_task("spec", SLUG)
    task_root = project_tmp / ".harness" / "tasks" / SLUG
    task_root.mkdir(parents=True)
    content = b"captured\r\nbytes\n"
    (task_root / "task.md").write_bytes(content)

    captures = capture_dependencies(project_tmp, expected)
    by_path = {
        item.record.dependency_path: item for item in captures
    }
    captured = by_path[f".harness/tasks/{SLUG}/task.md"]
    missing = by_path[f".harness/tasks/{SLUG}/acceptance.md"]

    assert captured.content == content
    assert captured.record.dependency_sha256 == sha256_bytes(content)
    assert missing.content is None
    assert missing.record.dependency_state == "missing"
    assert provenance_dependencies(captures) == snapshot_dependencies(
        project_tmp,
        expected,
    )
    with pytest.raises(FrozenInstanceError):
        captured.content = b"changed"  # type: ignore[misc]


def test_dependency_projection_rejects_hash_not_bound_to_content():
    capture = CapturedDependency(
        ProvenanceDependency("task.md", "present", "0" * 64),
        b"different",
    )

    with pytest.raises(
        LineageValidationError,
        match="hash does not match exact raw bytes",
    ):
        provenance_dependencies((capture,))


def test_dependency_snapshot_inspects_only_closed_definition_paths(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("test_contract_review", SLUG)
    inspected = []
    real_resolve = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["resolve_under_root"],
    ).resolve_under_root

    def recording_resolve(root, relative_path):
        inspected.append(relative_path)
        return real_resolve(root, relative_path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_under_root",
        recording_resolve,
    )

    snapshot_dependencies(project_tmp, expected)

    assert inspected == sorted(expected.dependencies)


def test_dependency_snapshot_rejects_structural_path_safety_failure(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("spec", SLUG)

    def reject_path(_root, _relative_path):
        raise PathSafetyError("simulated escape")

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_under_root",
        reject_path,
    )

    with pytest.raises(PathSafetyError, match="simulated escape"):
        snapshot_dependencies(project_tmp, expected)


def test_repository_observation_snapshot_is_exact_shallow_and_canonical(
    project_tmp,
):
    expected = lineage_definition_for_task("test_contract_review", SLUG)
    (project_tmp / "pyproject.toml").write_bytes(b"[project]\n")
    (project_tmp / "tests").mkdir()

    snapshot = snapshot_repository_observations(project_tmp, expected)
    identities = tuple(
        (record.observation_path, record.predicate) for record in snapshot
    )
    results = {
        (record.observation_path, record.predicate): record.result
        for record in snapshot
    }

    assert identities == tuple(
        sorted(
            (
                observation.relative_path,
                observation.predicate,
            )
            for observation in expected.repository_observations
        )
    )
    assert results[("pyproject.toml", "is_file")] is True
    assert results[("tests", "is_dir")] is True
    assert results[("package.json", "is_file")] is False
    assert all(
        tuple(record.__dataclass_fields__) == (
            "observation_path",
            "predicate",
            "result",
        )
        for record in snapshot
    )


def test_repository_observation_snapshot_rejects_path_escape(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("preflight", SLUG)

    def reject_path(_root, _relative_path):
        raise PathSafetyError("simulated observation escape")

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_under_root",
        reject_path,
    )

    with pytest.raises(PathSafetyError, match="observation escape"):
        snapshot_repository_observations(project_tmp, expected)


def test_inspected_models_are_frozen_tuple_nested_and_deterministic():
    expected, record = _record()
    first = _matching_inspected(expected, record)
    second = _matching_inspected(expected, record)

    assert first == second
    assert isinstance(first.dependencies, tuple)
    assert isinstance(first.repository_observations, tuple)
    with pytest.raises(FrozenInstanceError):
        first.output_state = "missing"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.dependencies[0].dependency_state = "missing"  # type: ignore[misc]


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, None])
def test_present_inspected_file_state_requires_lowercase_sha256(digest):
    with pytest.raises(LineageValidationError, match="lowercase SHA-256"):
        InspectedDependencyState("task.md", "present", digest)


@pytest.mark.parametrize(
    "state",
    ["missing", "not_regular", "unreadable", "unsafe"],
)
def test_nonpresent_inspected_file_state_rejects_sha256(state):
    with pytest.raises(LineageValidationError, match="must not have"):
        InspectedDependencyState("task.md", state, "a" * 64)


def test_inspected_observation_requires_boolean_only_when_observed():
    with pytest.raises(LineageValidationError, match="Boolean"):
        InspectedObservationState("tests", "is_dir", "observed", 1)
    with pytest.raises(LineageValidationError, match="must not have"):
        InspectedObservationState("tests", "is_dir", "unavailable", False)
    with pytest.raises(LineageValidationError, match="must not have"):
        InspectedObservationState("tests", "is_dir", "unsafe", True)


def test_inspected_provenance_requires_tuples_and_canonical_order():
    expected, record = _record()
    inspected = _matching_inspected(expected, record)

    with pytest.raises(LineageValidationError, match="must be a tuple"):
        InspectedProvenanceState(
            inspected.output_path,
            inspected.output_state,
            inspected.output_sha256,
            list(inspected.dependencies),  # type: ignore[arg-type]
            inspected.repository_observations,
        )
    with pytest.raises(LineageValidationError, match="canonical path order"):
        replace(
            inspected,
            dependencies=tuple(reversed(inspected.dependencies)),
        )
    with pytest.raises(LineageValidationError, match="canonical path/predicate"):
        replace(
            inspected,
            repository_observations=tuple(
                reversed(inspected.repository_observations)
            ),
        )


@pytest.mark.parametrize(
    "root",
    ["not-a-path", Path("missing-lineage-repository")],
)
def test_inspection_requires_existing_path_directory(root):
    with pytest.raises(LineageValidationError, match="repository_root"):
        inspect_provenance_state(
            root,  # type: ignore[arg-type]
            lineage_definition_for_task("spec", SLUG),
        )


def test_inspection_rejects_repository_root_file(tmp_path):
    root_file = tmp_path / "repository-file"
    root_file.write_bytes(b"not a directory")

    with pytest.raises(LineageValidationError, match="existing directory"):
        inspect_provenance_state(
            root_file,
            lineage_definition_for_task("spec", SLUG),
        )


def test_inspection_rejects_forged_resolved_definition(project_tmp):
    expected = lineage_definition_for_task("spec", SLUG)

    with pytest.raises(LineageValidationError, match="closed Lineage"):
        inspect_provenance_state(
            project_tmp,
            replace(expected, output_path="other.md"),
        )


def test_inspection_records_output_present_exact_hash_and_missing(project_tmp):
    expected = lineage_definition_for_task("spec", SLUG)
    target = project_tmp / Path(*PurePosixPath(expected.output_path).parts)
    target.parent.mkdir(parents=True)
    content = b"output\r\nexact bytes\n"
    target.write_bytes(content)

    present = inspect_provenance_state(project_tmp, expected)
    target.unlink()
    missing = inspect_provenance_state(project_tmp, expected)

    assert present.output_state == "present"
    assert present.output_sha256 == sha256_bytes(content)
    assert missing.output_state == "missing"
    assert missing.output_sha256 is None


def test_inspection_records_output_not_regular(project_tmp):
    expected = lineage_definition_for_task("spec", SLUG)
    target = project_tmp / Path(*PurePosixPath(expected.output_path).parts)
    target.mkdir(parents=True)

    inspected = inspect_provenance_state(project_tmp, expected)

    assert inspected.output_state == "not_regular"
    assert inspected.output_sha256 is None


def test_inspection_records_output_read_failure_as_unreadable(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("spec", SLUG)
    target = project_tmp / Path(*PurePosixPath(expected.output_path).parts)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"output")

    def fail_output_hash(path):
        if path == target:
            raise PermissionError("simulated output read failure")
        return sha256_bytes(path.read_bytes())

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.sha256_file",
        fail_output_hash,
    )

    inspected = inspect_provenance_state(project_tmp, expected)

    assert inspected.output_state == "unreadable"
    assert inspected.output_sha256 is None


@pytest.mark.parametrize("message", ["final symlink", "parent symlink"])
def test_inspection_records_managed_output_safety_failure_as_unsafe(
    project_tmp,
    monkeypatch,
    message,
):
    expected = lineage_definition_for_task("spec", SLUG)

    def reject_output(_root, _path):
        raise PathSafetyError(message)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_managed_output_under_root",
        reject_output,
    )

    inspected = inspect_provenance_state(project_tmp, expected)

    assert inspected.output_state == "unsafe"
    assert inspected.output_sha256 is None


def test_inspection_records_dependency_states_hashes_lf_crlf_and_order(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("spec", SLUG)
    task_root = project_tmp / ".harness" / "tasks" / SLUG
    task_root.mkdir(parents=True)
    lf = task_root / "task.md"
    crlf = task_root / "acceptance.md"
    directory = task_root / "architecture-notes.md"
    unreadable = task_root / "coupling-notes.md"
    lf.write_bytes(b"same\n")
    crlf.write_bytes(b"same\r\n")
    directory.mkdir()
    unreadable.write_bytes(b"unreadable")
    real_sha256_file = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["sha256_file"],
    ).sha256_file

    def controlled_hash(path):
        if path == unreadable:
            raise PermissionError("simulated dependency read failure")
        return real_sha256_file(path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.sha256_file",
        controlled_hash,
    )

    inspected = inspect_provenance_state(project_tmp, expected)
    by_path = {
        item.dependency_path: item for item in inspected.dependencies
    }

    assert tuple(item.dependency_path for item in inspected.dependencies) == (
        tuple(sorted(expected.dependencies))
    )
    assert by_path[f".harness/tasks/{SLUG}/task.md"].dependency_state == (
        "present"
    )
    assert by_path[
        f".harness/tasks/{SLUG}/acceptance.md"
    ].dependency_state == "present"
    assert (
        by_path[f".harness/tasks/{SLUG}/task.md"].dependency_sha256
        != by_path[
            f".harness/tasks/{SLUG}/acceptance.md"
        ].dependency_sha256
    )
    assert by_path[
        f".harness/tasks/{SLUG}/architecture-notes.md"
    ].dependency_state == "not_regular"
    assert by_path[
        f".harness/tasks/{SLUG}/coupling-notes.md"
    ].dependency_state == "unreadable"
    assert by_path[
        f".harness/tasks/{SLUG}/test-contract.md"
    ].dependency_state == "missing"


def test_inspection_records_outside_root_dependency_as_unsafe(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("spec", SLUG)
    unsafe_path = sorted(expected.dependencies)[0]
    real_resolve = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["resolve_under_root"],
    ).resolve_under_root

    def controlled_resolve(root, relative_path):
        if relative_path == unsafe_path:
            raise PathSafetyError("simulated outside-root symlink")
        return real_resolve(root, relative_path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_under_root",
        controlled_resolve,
    )

    inspected = inspect_provenance_state(project_tmp, expected)

    assert inspected.dependencies[0] == InspectedDependencyState(
        unsafe_path,
        "unsafe",
        None,
    )


def test_inspection_visits_only_exact_closed_identities(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("preflight", SLUG)
    output_paths = []
    input_paths = []
    real_output_resolve = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["resolve_managed_output_under_root"],
    ).resolve_managed_output_under_root
    real_input_resolve = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["resolve_under_root"],
    ).resolve_under_root

    def record_output(root, relative_path):
        output_paths.append(relative_path)
        return real_output_resolve(root, relative_path)

    def record_input(root, relative_path):
        input_paths.append(relative_path)
        return real_input_resolve(root, relative_path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_managed_output_under_root",
        record_output,
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_under_root",
        record_input,
    )

    inspected = inspect_provenance_state(project_tmp, expected)

    assert output_paths == [expected.output_path]
    assert input_paths == [
        *sorted(expected.dependencies),
        *(
            item.relative_path
            for item in sorted(
                expected.repository_observations,
                key=lambda item: (item.relative_path, item.predicate),
            )
        ),
    ]
    assert tuple(
        item.dependency_path for item in inspected.dependencies
    ) == tuple(sorted(expected.dependencies))
    assert tuple(
        (item.observation_path, item.predicate)
        for item in inspected.repository_observations
    ) == tuple(
        sorted(
            (
                item.relative_path,
                item.predicate,
            )
            for item in expected.repository_observations
        )
    )


def test_inspection_observes_true_and_false_for_all_predicates(project_tmp):
    expected = lineage_definition_for_task("preflight", SLUG)
    absent = inspect_provenance_state(project_tmp, expected)
    absent_results = {
        (item.observation_path, item.predicate): item.result
        for item in absent.repository_observations
    }
    (project_tmp / ".git").mkdir()
    (project_tmp / "pyproject.toml").write_bytes(b"[project]\n")
    (project_tmp / "tests").mkdir()

    inspected = inspect_provenance_state(project_tmp, expected)
    results = {
        (item.observation_path, item.predicate): item.result
        for item in inspected.repository_observations
    }

    assert absent_results[(".git", "exists")] is False
    assert results[(".git", "exists")] is True
    assert results[("pyproject.toml", "is_file")] is True
    assert results[("package.json", "is_file")] is False
    assert results[("tests", "is_dir")] is True
    assert results[(".github/workflows", "is_dir")] is False
    assert {
        item.observation_state
        for item in inspected.repository_observations
    } == {"observed"}


def test_inspection_observation_failure_is_unavailable(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("preflight", SLUG)
    failing = (project_tmp / "package.json").resolve()
    real_is_file = Path.is_file

    def controlled_is_file(path):
        if path == failing:
            raise PermissionError("simulated observation failure")
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", controlled_is_file)

    inspected = inspect_provenance_state(project_tmp, expected)
    observation = next(
        item
        for item in inspected.repository_observations
        if item.observation_path == "package.json"
    )

    assert observation.observation_state == "unavailable"
    assert observation.result is None


def test_inspection_observation_safety_failure_is_unsafe(
    project_tmp,
    monkeypatch,
):
    expected = lineage_definition_for_task("preflight", SLUG)
    unsafe_path = ".git"
    real_resolve = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["resolve_under_root"],
    ).resolve_under_root

    def controlled_resolve(root, relative_path):
        if relative_path == unsafe_path:
            raise PathSafetyError("simulated observation escape")
        return real_resolve(root, relative_path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_under_root",
        controlled_resolve,
    )

    inspected = inspect_provenance_state(project_tmp, expected)
    observation = inspected.repository_observations[0]

    assert observation == InspectedObservationState(
        ".git",
        "exists",
        "unsafe",
        None,
    )


def test_repeated_inspection_is_deterministic_and_performs_no_writes(
    project_tmp,
):
    expected = lineage_definition_for_task("spec", SLUG)
    output = project_tmp / Path(*PurePosixPath(expected.output_path).parts)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"output")
    before = {
        path.relative_to(project_tmp).as_posix(): (
            path.read_bytes() if path.is_file() else None
        )
        for path in project_tmp.rglob("*")
    }

    first = inspect_provenance_state(project_tmp, expected)
    second = inspect_provenance_state(project_tmp, expected)
    after = {
        path.relative_to(project_tmp).as_posix(): (
            path.read_bytes() if path.is_file() else None
        )
        for path in project_tmp.rglob("*")
    }

    assert first == second
    assert after == before


def test_inspection_does_not_read_main_manifest(project_tmp, monkeypatch):
    expected = lineage_definition_for_task("spec", SLUG)
    manifest = project_tmp / ".harness" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_bytes(b"must not be read")
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == manifest:
            pytest.fail("inspection read the main manifest")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    inspect_provenance_state(project_tmp, expected)


@pytest.mark.parametrize(
    ("output_state", "record_present", "expected_state"),
    [
        ("missing", False, FreshnessState.MISSING),
        ("missing", True, FreshnessState.MISSING),
        ("unsafe", True, FreshnessState.UNKNOWN),
        ("unreadable", True, FreshnessState.UNKNOWN),
        ("not_regular", True, FreshnessState.UNKNOWN),
        ("present", False, FreshnessState.UNKNOWN),
    ],
)
def test_freshness_output_and_missing_record_table(
    output_state,
    record_present,
    expected_state,
):
    expected, record = _record("spec")
    inspected = _matching_inspected(
        expected,
        record,
        output_state=output_state,
    )

    assert decide_freshness(
        record if record_present else None,
        inspected,
        expected,
    ) == expected_state


def test_exact_matching_state_is_fresh_and_equivalent_inputs_are_equal():
    expected, record = _record()
    first = _matching_inspected(expected, record)
    second = _matching_inspected(expected, record)

    assert decide_freshness(record, first, expected) == FreshnessState.FRESH
    assert decide_freshness(record, second, expected) == FreshnessState.FRESH


def test_output_hash_change_is_stale():
    expected, record = _record("spec")
    inspected = _matching_inspected(
        expected,
        record,
        output_sha256="c" * 64,
    )

    assert decide_freshness(record, inspected, expected) == FreshnessState.STALE


def test_producer_version_change_is_stale_but_direct_validation_still_rejects():
    expected, record = _record("spec")
    changed = replace(record, producer_version=expected.producer_version + 1)
    inspected = _matching_inspected(expected, record)

    assert decide_freshness(changed, inspected, expected) == FreshnessState.STALE
    with pytest.raises(LineageValidationError, match="producer version"):
        validate_provenance_record(changed, expected)


@pytest.mark.parametrize(
    ("recorded_state", "current_state", "hash_relation", "expected_state"),
    [
        ("present", "present", "same", FreshnessState.FRESH),
        ("present", "present", "different", FreshnessState.STALE),
        ("present", "missing", None, FreshnessState.STALE),
        ("present", "not_regular", None, FreshnessState.STALE),
        ("present", "unreadable", None, FreshnessState.UNKNOWN),
        ("missing", "missing", None, FreshnessState.FRESH),
        ("missing", "present", "different", FreshnessState.STALE),
        ("missing", "not_regular", None, FreshnessState.STALE),
        ("missing", "unreadable", None, FreshnessState.UNKNOWN),
        ("not_regular", "not_regular", None, FreshnessState.FRESH),
        ("not_regular", "present", "different", FreshnessState.STALE),
        ("not_regular", "missing", None, FreshnessState.STALE),
        ("not_regular", "unreadable", None, FreshnessState.UNKNOWN),
        ("unreadable", "unreadable", None, FreshnessState.UNKNOWN),
        ("unreadable", "present", "different", FreshnessState.STALE),
        ("unreadable", "missing", None, FreshnessState.STALE),
        ("unreadable", "not_regular", None, FreshnessState.STALE),
    ],
)
def test_dependency_freshness_decision_table(
    recorded_state,
    current_state,
    hash_relation,
    expected_state,
):
    expected, base_record = _record("spec")
    recorded_hash = "b" * 64 if recorded_state == "present" else None
    record = _replace_recorded_dependency(
        base_record,
        dependency_state=recorded_state,
        dependency_sha256=recorded_hash,
    )
    inspected = _matching_inspected(expected, record)
    current_hash = (
        recorded_hash
        if current_state == "present" and hash_relation == "same"
        else "c" * 64 if current_state == "present" else None
    )
    inspected = _replace_inspected_dependency(
        inspected,
        dependency_state=current_state,
        dependency_sha256=current_hash,
    )

    assert decide_freshness(record, inspected, expected) == expected_state


def test_unsafe_dependency_and_observation_are_unknown():
    expected, record = _record()
    inspected = _matching_inspected(expected, record)
    unsafe_dependency = _replace_inspected_dependency(
        inspected,
        dependency_state="unsafe",
        dependency_sha256=None,
    )
    observations = list(inspected.repository_observations)
    observations[0] = replace(
        observations[0],
        observation_state="unsafe",
        result=None,
    )
    unsafe_observation = replace(
        inspected,
        repository_observations=tuple(observations),
    )

    assert decide_freshness(
        record,
        unsafe_dependency,
        expected,
    ) == FreshnessState.UNKNOWN
    assert decide_freshness(
        record,
        unsafe_observation,
        expected,
    ) == FreshnessState.UNKNOWN


def test_observation_changed_is_stale_and_unavailable_is_unknown():
    expected, record = _record()
    inspected = _matching_inspected(expected, record)
    observations = list(inspected.repository_observations)
    observations[0] = replace(observations[0], result=False)
    changed = replace(
        inspected,
        repository_observations=tuple(observations),
    )
    observations[0] = replace(
        observations[0],
        observation_state="unavailable",
        result=None,
    )
    unavailable = replace(
        inspected,
        repository_observations=tuple(observations),
    )

    assert decide_freshness(record, changed, expected) == FreshnessState.STALE
    assert decide_freshness(
        record,
        unavailable,
        expected,
    ) == FreshnessState.UNKNOWN


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("output_path", ".harness/tasks/lineage-task/other.md", "output path"),
        ("producer_command", "other", "producer command"),
        ("task_slug", "other-task", "task slug"),
    ],
)
def test_decision_rejects_wrong_record_metadata(field, value, message):
    expected, record = _record()
    inspected = _matching_inspected(expected, record)

    with pytest.raises(LineageValidationError, match=message):
        decide_freshness(
            replace(record, **{field: value}),
            inspected,
            expected,
        )


def test_decision_rejects_missing_extra_and_noncanonical_record_identities():
    expected, record = _record()
    inspected = _matching_inspected(expected, record)

    with pytest.raises(LineageValidationError, match="missing expected dependency"):
        decide_freshness(
            replace(record, dependencies=record.dependencies[1:]),
            inspected,
            expected,
        )
    extra_dependency = ProvenanceDependency(
        "unexpected.md",
        "missing",
        None,
    )
    with pytest.raises(LineageValidationError, match="unexpected dependency"):
        decide_freshness(
            replace(
                record,
                dependencies=tuple(
                    sorted(
                        (*record.dependencies, extra_dependency),
                        key=lambda item: item.dependency_path,
                    )
                ),
            ),
            inspected,
            expected,
        )
    with pytest.raises(LineageValidationError, match="canonical path order"):
        decide_freshness(
            replace(record, dependencies=tuple(reversed(record.dependencies))),
            inspected,
            expected,
        )
    with pytest.raises(
        LineageValidationError,
        match="missing expected repository observation",
    ):
        decide_freshness(
            replace(
                record,
                repository_observations=record.repository_observations[1:],
            ),
            inspected,
            expected,
        )
    extra_observation = ProvenanceObservation(
        "unexpected",
        "exists",
        False,
    )
    with pytest.raises(
        LineageValidationError,
        match="unexpected repository observation",
    ):
        decide_freshness(
            replace(
                record,
                repository_observations=tuple(
                    sorted(
                        (*record.repository_observations, extra_observation),
                        key=lambda item: (
                            item.observation_path,
                            item.predicate,
                        ),
                    )
                ),
            ),
            inspected,
            expected,
        )
    with pytest.raises(
        LineageValidationError,
        match="canonical path/predicate order",
    ):
        decide_freshness(
            replace(
                record,
                repository_observations=tuple(
                    reversed(record.repository_observations)
                ),
            ),
            inspected,
            expected,
        )


def test_decision_rejects_wrong_inspected_identities():
    expected, record = _record()
    inspected = _matching_inspected(expected, record)

    with pytest.raises(LineageValidationError, match="inspected output path"):
        decide_freshness(
            record,
            replace(inspected, output_path="other.md"),
            expected,
        )
    dependencies = list(inspected.dependencies)
    dependencies[0] = replace(
        dependencies[0],
        dependency_path=".000-other.md",
    )
    wrong_dependency = replace(
        inspected,
        dependencies=tuple(dependencies),
    )
    with pytest.raises(LineageValidationError, match="dependency identities"):
        decide_freshness(record, wrong_dependency, expected)
    observations = list(inspected.repository_observations)
    observations[0] = replace(
        observations[0],
        observation_path=".000-other",
    )
    wrong_observation = replace(
        inspected,
        repository_observations=tuple(observations),
    )
    with pytest.raises(
        LineageValidationError,
        match="repository observation identities",
    ):
        decide_freshness(record, wrong_observation, expected)


def test_decision_performs_no_filesystem_access(monkeypatch):
    expected, record = _record()
    inspected = _matching_inspected(expected, record)

    def unexpected_access(*_args, **_kwargs):
        pytest.fail("pure freshness decision accessed the filesystem")

    monkeypatch.setattr(Path, "exists", unexpected_access)
    monkeypatch.setattr(Path, "is_file", unexpected_access)
    monkeypatch.setattr(Path, "is_dir", unexpected_access)
    monkeypatch.setattr(Path, "read_bytes", unexpected_access)
    monkeypatch.setattr(Path, "resolve", unexpected_access)

    assert decide_freshness(record, inspected, expected) == FreshnessState.FRESH


def test_freshness_precedence_unsafe_over_stale_and_mismatch_over_indeterminate():
    expected, record = _record("spec")
    version_changed = replace(record, producer_version=2)
    inspected = _matching_inspected(
        expected,
        record,
        output_sha256="c" * 64,
    )
    unsafe = _replace_inspected_dependency(
        inspected,
        dependency_state="unsafe",
        dependency_sha256=None,
    )
    indeterminate = _replace_inspected_dependency(
        inspected,
        index=1,
        dependency_state="unreadable",
        dependency_sha256=None,
    )

    assert decide_freshness(
        version_changed,
        unsafe,
        expected,
    ) == FreshnessState.UNKNOWN
    assert decide_freshness(
        version_changed,
        _matching_inspected(expected, record),
        expected,
    ) == FreshnessState.STALE
    assert decide_freshness(
        record,
        indeterminate,
        expected,
    ) == FreshnessState.STALE


def test_lineage_resolution_result_is_frozen_defensive_and_canonical():
    original = {
        "test_contract_review": FreshnessState.UNKNOWN,
        "preflight": FreshnessState.MISSING,
        "requirements_projection": FreshnessState.STALE,
        "spec": FreshnessState.FRESH,
    }
    duplicate = ContextFinding(
        "same_warning",
        FindingLevel.WARNING,
        "z.md",
        "Same warning",
    )
    blocker = ContextFinding(
        "a_blocker",
        FindingLevel.BLOCKER,
        None,
        "Blocker",
    )
    info = ContextFinding(
        "a_info",
        FindingLevel.INFO,
        "a.md",
        "Info",
    )

    result = LineageResolutionResult(
        original,
        (info, duplicate, blocker, duplicate),
    )
    equivalent = LineageResolutionResult(
        dict(original),
        (duplicate, blocker, info),
    )
    original["spec"] = FreshnessState.UNKNOWN

    assert isinstance(result.derived_freshness, MappingProxyType)
    assert tuple(result.derived_freshness) == (
        "spec",
        "requirements_projection",
        "preflight",
        "test_contract_review",
    )
    assert result.derived_freshness["spec"] == FreshnessState.FRESH
    assert result.findings == (blocker, duplicate, info)
    assert result.blockers == (blocker,)
    assert result == equivalent
    with pytest.raises(TypeError):
        result.derived_freshness["spec"] = FreshnessState.STALE  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.findings = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    "mapping",
    [
        {
            "spec": FreshnessState.FRESH,
            "requirements_projection": FreshnessState.FRESH,
            "preflight": FreshnessState.FRESH,
        },
        {
            "spec": FreshnessState.FRESH,
            "requirements_projection": FreshnessState.FRESH,
            "preflight": FreshnessState.FRESH,
            "test_contract_review": FreshnessState.FRESH,
            "extra": FreshnessState.FRESH,
        },
        {
            "spec": FreshnessState.NOT_APPLICABLE,
            "requirements_projection": FreshnessState.FRESH,
            "preflight": FreshnessState.FRESH,
            "test_contract_review": FreshnessState.FRESH,
        },
        {
            "spec": "fresh",
            "requirements_projection": FreshnessState.FRESH,
            "preflight": FreshnessState.FRESH,
            "test_contract_review": FreshnessState.FRESH,
        },
    ],
)
def test_lineage_resolution_result_rejects_invalid_mapping(mapping):
    with pytest.raises(LineageValidationError, match="derived_freshness"):
        LineageResolutionResult(mapping, ())


def test_lineage_resolution_result_requires_context_finding_tuple():
    mapping = {
        definition.registry_id: FreshnessState.MISSING
        for definition in lineage_definitions()
    }

    with pytest.raises(LineageValidationError, match="findings must be a tuple"):
        LineageResolutionResult(mapping, [])  # type: ignore[arg-type]
    with pytest.raises(LineageValidationError, match="ContextFinding"):
        LineageResolutionResult(mapping, ("warning",))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "root",
    ["not-a-path", Path("missing-lineage-resolution-root")],
)
def test_resolver_rejects_invalid_repository_root(root):
    with pytest.raises(LineageValidationError, match="repository_root"):
        resolve_derived_freshness(root, SLUG)  # type: ignore[arg-type]


def test_resolver_rejects_invalid_task_slug(project_tmp):
    with pytest.raises(LineageValidationError, match="safe task slug"):
        resolve_derived_freshness(project_tmp, "../escape")


def test_resolver_uninitialized_fallback_preserves_physical_absence(
    project_tmp,
):
    present = project_tmp / ".harness" / "tasks" / SLUG / "spec.md"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"present without initialization")

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness == {
        "spec": FreshnessState.UNKNOWN,
        "requirements_projection": FreshnessState.MISSING,
        "preflight": FreshnessState.MISSING,
        "test_contract_review": FreshnessState.MISSING,
    }
    assert result.blockers == (
        ContextFinding(
            "lineage_repository_uninitialized",
            FindingLevel.BLOCKER,
            ".harness",
            "AI SDLC Harness is not initialized; run `ai-sdlc init` from the "
            "project root.",
        ),
    )


def test_resolver_initialized_missing_manifest_uses_global_fallback(
    project_tmp,
):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_path.unlink()
    before = _repository_snapshot(project_tmp)

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert set(result.derived_freshness.values()) == {FreshnessState.MISSING}
    assert result.blockers[0].code == "lineage_manifest_missing"
    assert result.blockers[0].message == (
        "Initialized repository is missing the main Harness manifest: "
        ".harness/manifest.json."
    )
    assert _repository_snapshot(project_tmp) == before


def test_resolver_unreadable_manifest_uses_global_fallback(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0

    def unreadable(_path):
        raise PermissionError("simulated unreadable manifest")

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.load_manifest_model",
        unreadable,
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert set(result.derived_freshness.values()) == {FreshnessState.MISSING}
    assert result.blockers[0] == ContextFinding(
        "lineage_manifest_unreadable",
        FindingLevel.BLOCKER,
        ".harness/manifest.json",
        "The main Harness manifest could not be read; Lineage freshness "
        "cannot be trusted.",
    )


@pytest.mark.parametrize(
    "invalid_text",
    [
        "{",
        '{"manifest_schema_version": 2, "manifest_schema_version": 2}',
        '{"manifest_schema_version": 3}',
    ],
)
def test_resolver_invalid_manifest_uses_global_fallback(
    project_tmp,
    invalid_text,
):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_path.write_text(invalid_text, encoding="utf-8")
    before = manifest_path.read_bytes()

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert set(result.derived_freshness.values()) == {FreshnessState.MISSING}
    assert [finding.code for finding in result.blockers] == [
        "lineage_manifest_invalid"
    ]
    assert manifest_path.read_bytes() == before


def test_resolver_unknown_manifest_field_is_structurally_invalid(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["unexpected"] = True
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert [finding.code for finding in result.blockers] == [
        "lineage_manifest_invalid"
    ]


def test_resolver_legacy_v1_is_read_only_and_warns_once(project_tmp):
    assert init_project(project_tmp)[0] == 0
    output = project_tmp / ".harness" / "tasks" / SLUG / "spec.md"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"legacy present output")
    legacy = build_manifest_model(
        project_tmp,
        extra_managed_paths=(PurePosixPath(output.relative_to(project_tmp)),),
        schema_version=1,
    )
    assert isinstance(legacy, ManifestV1)
    persist_manifest_model(project_tmp, legacy)
    manifest_path = project_tmp / ".harness" / "manifest.json"
    before = manifest_path.read_bytes()

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness["spec"] == FreshnessState.UNKNOWN
    assert tuple(result.derived_freshness.values())[1:] == (
        FreshnessState.MISSING,
        FreshnessState.MISSING,
        FreshnessState.MISSING,
    )
    assert [finding.code for finding in result.findings] == [
        "legacy_lineage_manifest"
    ]
    assert result.blockers == ()
    assert manifest_path.read_bytes() == before


def test_resolver_empty_v2_warns_only_for_present_output(project_tmp):
    assert init_project(project_tmp)[0] == 0
    output = project_tmp / ".harness" / "tasks" / SLUG / "spec.md"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"present without record")

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness == {
        "spec": FreshnessState.UNKNOWN,
        "requirements_projection": FreshnessState.MISSING,
        "preflight": FreshnessState.MISSING,
        "test_contract_review": FreshnessState.MISSING,
    }
    assert result.findings == (
        ContextFinding(
            "missing_lineage_provenance",
            FindingLevel.WARNING,
            f".harness/tasks/{SLUG}/spec.md",
            "Present derived output has no Lineage provenance and remains "
            f"unknown: .harness/tasks/{SLUG}/spec.md; rerun "
            f"`ai-sdlc spec --task {SLUG}`.",
        ),
    )


def test_all_produced_outputs_resolve_fresh_deterministically_and_read_only(
    project_tmp,
):
    first = _produce_all_lineage_outputs(project_tmp)
    before = _repository_snapshot(project_tmp)
    second = resolve_derived_freshness(project_tmp, SLUG)

    assert tuple(first.derived_freshness) == (
        "spec",
        "requirements_projection",
        "preflight",
        "test_contract_review",
    )
    assert set(first.derived_freshness.values()) == {FreshnessState.FRESH}
    assert first.findings == ()
    assert second == first
    assert _repository_snapshot(project_tmp) == before


def test_resolver_does_not_read_config_or_state_content_or_call_selector(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    config = (project_tmp / ".harness" / "config.yaml").resolve()
    state = (project_tmp / ".harness" / "state.json").resolve()
    real_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path.resolve() in {config, state}:
            pytest.fail(f"resolver read control-file content: {path}")
        return real_read_text(path, *args, **kwargs)

    def forbidden_selector(*_args, **_kwargs):
        pytest.fail("resolver called select_context")

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(
        "ai_sdlc_harness.context.select_context",
        forbidden_selector,
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert set(result.derived_freshness.values()) == {FreshnessState.MISSING}


def test_semantically_invalid_v2_blocks_global_trust(project_tmp):
    produced = _produce_all_lineage_outputs(project_tmp)
    assert set(produced.derived_freshness.values()) == {FreshnessState.FRESH}
    manifest = load_manifest_model(
        project_tmp / ".harness" / "manifest.json"
    )
    assert isinstance(manifest, ManifestV2)
    records = list(manifest.generated_artifact_provenance)
    records[0] = replace(
        records[0],
        producer_command="unknown-producer",
    )
    persist_manifest_model(
        project_tmp,
        replace(manifest, generated_artifact_provenance=tuple(records)),
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert set(result.derived_freshness.values()) == {FreshnessState.UNKNOWN}
    assert result.blockers == (
        ContextFinding(
            "lineage_provenance_invalid",
            FindingLevel.BLOCKER,
            ".harness/manifest.json",
            "The main Harness manifest contains provenance that does not "
            "match the closed Lineage contract.",
        ),
    )


def test_manifest_semantic_helper_allows_old_producer_version(project_tmp):
    _produce_all_lineage_outputs(project_tmp)
    manifest = load_manifest_model(
        project_tmp / ".harness" / "manifest.json"
    )
    assert isinstance(manifest, ManifestV2)
    old_record = replace(
        manifest.generated_artifact_provenance[0],
        producer_version=2,
    )
    old_manifest = replace(
        manifest,
        generated_artifact_provenance=(
            old_record,
            *manifest.generated_artifact_provenance[1:],
        ),
    )
    persist_manifest_model(project_tmp, old_manifest)

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert validate_manifest_lineage_provenance(old_manifest) is old_manifest
    changed_id = next(
        definition.registry_id
        for definition in lineage_definitions_for_task(SLUG)
        if definition.output_path == old_record.output_path
    )
    assert result.derived_freshness[changed_id] == FreshnessState.STALE
    assert result.blockers == ()


def test_trusted_resolution_is_independent_per_output(project_tmp):
    _produce_all_lineage_outputs(project_tmp)
    spec = project_tmp / ".harness" / "tasks" / SLUG / "spec.md"
    spec.write_bytes(spec.read_bytes() + b"\nmanual edit")

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness == {
        "spec": FreshnessState.STALE,
        "requirements_projection": FreshnessState.FRESH,
        "preflight": FreshnessState.FRESH,
        "test_contract_review": FreshnessState.FRESH,
    }
    assert result.findings == ()


def test_shared_source_and_direct_preflight_changes_have_bounded_staleness(
    project_tmp,
):
    _produce_all_lineage_outputs(project_tmp)
    task_root = project_tmp / ".harness" / "tasks" / SLUG
    shared_source = task_root / "architecture-notes.md"
    shared_source.write_bytes(shared_source.read_bytes() + b"\nchanged")

    shared_result = resolve_derived_freshness(project_tmp, SLUG)

    assert shared_result.derived_freshness == {
        "spec": FreshnessState.STALE,
        "requirements_projection": FreshnessState.STALE,
        "preflight": FreshnessState.STALE,
        "test_contract_review": FreshnessState.FRESH,
    }

    shared_source.write_bytes(
        shared_source.read_bytes().removesuffix(b"\nchanged")
    )
    selected = project_tmp / ".harness" / "packs" / "selected.yaml"
    selected.write_bytes(selected.read_bytes() + b"\n# changed")

    direct_result = resolve_derived_freshness(project_tmp, SLUG)

    assert direct_result.derived_freshness == {
        "spec": FreshnessState.FRESH,
        "requirements_projection": FreshnessState.FRESH,
        "preflight": FreshnessState.STALE,
        "test_contract_review": FreshnessState.FRESH,
    }


def test_repository_observation_change_stales_only_observers(project_tmp):
    _produce_all_lineage_outputs(project_tmp)
    (project_tmp / "package.json").write_bytes(b"{}")

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness == {
        "spec": FreshnessState.FRESH,
        "requirements_projection": FreshnessState.FRESH,
        "preflight": FreshnessState.STALE,
        "test_contract_review": FreshnessState.STALE,
    }


def test_missing_record_and_missing_output_do_not_block_other_fresh_outputs(
    project_tmp,
):
    _produce_all_lineage_outputs(project_tmp)
    definitions = {
        definition.registry_id: definition
        for definition in lineage_definitions_for_task(SLUG)
    }
    requirements = definitions["requirements_projection"]
    output = project_tmp.joinpath(*PurePosixPath(requirements.output_path).parts)
    output.unlink()
    manifest = load_manifest_model(
        project_tmp / ".harness" / "manifest.json"
    )
    test_contract = definitions["test_contract_review"]
    updated = remove_provenance_records(
        manifest,
        (test_contract.output_path,),
        generated_at="unchanged-for-test",
    )
    persist_manifest_model(project_tmp, updated)

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness == {
        "spec": FreshnessState.FRESH,
        "requirements_projection": FreshnessState.MISSING,
        "preflight": FreshnessState.FRESH,
        "test_contract_review": FreshnessState.UNKNOWN,
    }
    assert [finding.code for finding in result.findings] == [
        "missing_lineage_provenance"
    ]


def test_unreadable_shared_dependency_is_unknown_and_warned_once(
    project_tmp,
    monkeypatch,
):
    _produce_all_lineage_outputs(project_tmp)
    dependency = (
        project_tmp / ".harness" / "tasks" / SLUG / "acceptance.md"
    )
    real_hash = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["sha256_file"],
    ).sha256_file

    def controlled_hash(path):
        if path == dependency:
            raise PermissionError("simulated unreadable dependency")
        return real_hash(path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.sha256_file",
        controlled_hash,
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness == {
        "spec": FreshnessState.UNKNOWN,
        "requirements_projection": FreshnessState.UNKNOWN,
        "preflight": FreshnessState.UNKNOWN,
        "test_contract_review": FreshnessState.UNKNOWN,
    }
    warnings = [
        finding
        for finding in result.findings
        if finding.code == "unreadable_lineage_dependency"
    ]
    assert warnings == [
        ContextFinding(
            "unreadable_lineage_dependency",
            FindingLevel.WARNING,
            f".harness/tasks/{SLUG}/acceptance.md",
            "Lineage dependency cannot be read: "
            f".harness/tasks/{SLUG}/acceptance.md.",
        )
    ]


def test_unavailable_shared_observation_is_unknown_and_warned_once(
    project_tmp,
    monkeypatch,
):
    _produce_all_lineage_outputs(project_tmp)
    failing = (project_tmp / "package.json").resolve()
    real_is_file = Path.is_file

    def controlled_is_file(path):
        if path == failing:
            raise PermissionError("simulated unavailable observation")
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", controlled_is_file)

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness["preflight"] == FreshnessState.UNKNOWN
    assert (
        result.derived_freshness["test_contract_review"]
        == FreshnessState.UNKNOWN
    )
    warnings = [
        finding
        for finding in result.findings
        if finding.code == "unavailable_lineage_observation"
    ]
    assert warnings == [
        ContextFinding(
            "unavailable_lineage_observation",
            FindingLevel.WARNING,
            "package.json",
            "Lineage repository observation is unavailable: package.json.",
        )
    ]


def test_unsafe_shared_dependency_is_unknown_blocker_and_deduplicated(
    project_tmp,
    monkeypatch,
):
    _produce_all_lineage_outputs(project_tmp)
    unsafe_path = f".harness/tasks/{SLUG}/acceptance.md"
    real_resolve = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["resolve_under_root"],
    ).resolve_under_root

    def controlled_resolve(root, relative_path):
        if relative_path == unsafe_path:
            raise PathSafetyError("simulated unsafe dependency")
        return real_resolve(root, relative_path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_under_root",
        controlled_resolve,
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert result.derived_freshness == {
        "spec": FreshnessState.UNKNOWN,
        "requirements_projection": FreshnessState.UNKNOWN,
        "preflight": FreshnessState.UNKNOWN,
        "test_contract_review": FreshnessState.UNKNOWN,
    }
    assert result.blockers == (
        ContextFinding(
            "unsafe_lineage_dependency",
            FindingLevel.BLOCKER,
            unsafe_path,
            f"Lineage dependency path is unsafe: {unsafe_path}.",
        ),
    )


def test_current_state_findings_survive_manifest_fallback(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    output_path = f".harness/tasks/{SLUG}/spec.md"
    real_output_resolve = __import__(
        "ai_sdlc_harness.lineage",
        fromlist=["resolve_managed_output_under_root"],
    ).resolve_managed_output_under_root

    def controlled_output(root, relative_path):
        if relative_path == output_path:
            raise PathSafetyError("simulated unsafe output")
        return real_output_resolve(root, relative_path)

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_managed_output_under_root",
        controlled_output,
    )
    (project_tmp / ".harness" / "manifest.json").write_text(
        "{",
        encoding="utf-8",
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    assert [finding.code for finding in result.blockers] == [
        "lineage_manifest_invalid",
        "unsafe_lineage_output",
    ]
    assert result.derived_freshness["spec"] == FreshnessState.UNKNOWN


@pytest.mark.parametrize(
    (
        "scenario",
        "code",
        "level",
        "path",
        "message",
    ),
    [
        (
            "unreadable_output",
            "unreadable_lineage_output",
            FindingLevel.WARNING,
            f".harness/tasks/{SLUG}/spec.md",
            "Derived output cannot be read for Lineage freshness: "
            f".harness/tasks/{SLUG}/spec.md.",
        ),
        (
            "nonregular_output",
            "nonregular_lineage_output",
            FindingLevel.WARNING,
            f".harness/tasks/{SLUG}/spec.md",
            "Derived output is not a regular file: "
            f".harness/tasks/{SLUG}/spec.md.",
        ),
        (
            "unsafe_observation",
            "unsafe_lineage_observation",
            FindingLevel.BLOCKER,
            ".git",
            "Lineage repository-observation path is unsafe: .git.",
        ),
    ],
)
def test_resolver_emits_exact_remaining_current_state_findings(
    project_tmp,
    monkeypatch,
    scenario,
    code,
    level,
    path,
    message,
):
    assert init_project(project_tmp)[0] == 0
    real_inspect = inspect_provenance_state

    def controlled_inspect(root, expected):
        inspected = real_inspect(root, expected)
        if expected.registry_id == "spec":
            if scenario == "unreadable_output":
                return replace(
                    inspected,
                    output_state="unreadable",
                    output_sha256=None,
                )
            if scenario == "nonregular_output":
                return replace(
                    inspected,
                    output_state="not_regular",
                    output_sha256=None,
                )
        if (
            scenario == "unsafe_observation"
            and expected.registry_id == "preflight"
        ):
            observations = list(inspected.repository_observations)
            observations[0] = replace(
                observations[0],
                observation_state="unsafe",
                result=None,
            )
            return replace(
                inspected,
                repository_observations=tuple(observations),
            )
        return inspected

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.inspect_provenance_state",
        controlled_inspect,
    )

    result = resolve_derived_freshness(project_tmp, SLUG)

    finding = next(item for item in result.findings if item.code == code)
    assert finding == ContextFinding(code, level, path, message)
    if scenario == "unsafe_observation":
        assert result.derived_freshness["preflight"] == FreshnessState.MISSING
        assert result.blockers == (finding,)
    else:
        assert result.derived_freshness["spec"] == FreshnessState.UNKNOWN
        assert result.blockers == ()
