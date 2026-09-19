from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.lineage import (
    build_provenance_record,
    validate_manifest_provenance,
)
from ai_sdlc_harness.manifest import (
    GeneratedArtifactProvenance,
    ManagedFileRecord,
    ManifestV1,
    ManifestV2,
    ManifestValidationError,
    ProvenanceDependency,
    ProvenanceObservation,
    build_managed_file_record,
    is_adapter_artifact_path,
    is_task_artifact_path,
    load_manifest_model,
    parse_manifest_json,
    persist_manifest_model,
    remove_provenance_records,
    render_manifest_json,
    replace_managed_and_provenance_records,
    replace_managed_file_records,
    sha256_file,
    update_managed_file_records,
    write_manifest,
)
from ai_sdlc_harness.validation import validation_provenance_definition_for_task

RELEASED_V1_FIXTURE = Path(__file__).parent / "fixtures" / "main-manifest-v1.json"


def _manifest_path(root):
    return root / ".harness" / "manifest.json"


def _v2_data(root):
    assert init_project(root)[0] == 0
    return json.loads(_manifest_path(root).read_text(encoding="utf-8"))


def _released_v1_text():
    return RELEASED_V1_FIXTURE.read_text(encoding="utf-8")


def _write_released_v1(root):
    data = json.loads(_released_v1_text())
    for record in data["managed_files"]:
        if record["hash_algorithm"] == "sha256":
            record["sha256"] = sha256_file(root / record["path"])
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    _manifest_path(root).write_text(text, encoding="utf-8", newline="\n")
    return text


def _managed_entry(data, path):
    return next(record for record in data["managed_files"] if record["path"] == path)


@pytest.mark.parametrize(
    "path",
    [
        ".harness/tasks/sample-task/generated/agent-workset.md",
        ".harness/tasks/sample-task/generated/context-manifest.yaml",
    ],
)
def test_exact_generate_owned_output_paths_are_manifest_eligible(path):
    assert is_task_artifact_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".agents/skills/ai-sdlc-harness/SKILL.md",
        ".claude/skills/ai-sdlc-harness/SKILL.md",
        ".gemini/skills/ai-sdlc-harness/SKILL.md",
    ],
)
def test_exact_adapter_output_paths_are_manifest_eligible(path):
    assert is_adapter_artifact_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".agents/skills/other/SKILL.md",
        ".agents/skills/ai-sdlc-harness/other.md",
        ".claude/skills/ai-sdlc-harness/nested/SKILL.md",
        ".gemini/SKILL.md",
        ".agents/skills/ai-sdlc-harness",
    ],
)
def test_adapter_output_eligibility_is_not_a_wildcard(path):
    assert not is_adapter_artifact_path(path)


@pytest.mark.parametrize(
    "path",
    [
        ".harness/tasks/sample-task/generated/arbitrary.yaml",
        ".harness/tasks/sample-task/generated/nested/context-manifest.yaml",
        ".harness/tasks/Bad/generated/context-manifest.yaml",
    ],
)
def test_generated_output_eligibility_is_not_a_wildcard(path):
    assert not is_task_artifact_path(path)


def test_validation_report_is_an_exact_manifest_eligible_task_output():
    assert is_task_artifact_path(
        ".harness/tasks/sample-task/validation-report.md"
    )


def _v2_data_with_managed_output(root):
    _v2_data(root)
    output_path = PurePosixPath(".harness/tasks/sample-task/preflight.md")
    target = root / Path(*output_path.parts)
    target.parent.mkdir(parents=True)
    target.write_text("# Preflight\n", encoding="utf-8")
    write_manifest(root, extra_managed_paths=[output_path])
    data = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    return data, output_path.as_posix(), sha256_file(target)


def test_frozen_released_versionless_manifest_parses():
    document = parse_manifest_json(_released_v1_text())

    assert isinstance(document, ManifestV1)
    assert document.schema_version == 1
    assert not hasattr(document, "generated_artifact_provenance")


def test_manifest_models_are_frozen():
    document = parse_manifest_json(_released_v1_text())

    with pytest.raises(FrozenInstanceError):
        document.generated_at = "changed"


def test_v2_model_render_parse_round_trip_is_deterministic(project_tmp):
    data = _v2_data(project_tmp)
    document = parse_manifest_json(json.dumps(data))

    first = render_manifest_json(document)
    second = render_manifest_json(parse_manifest_json(first))

    assert isinstance(document, ManifestV2)
    assert first == second
    assert first.endswith("\n")
    assert not first.endswith("\n\n")
    assert "!!python" not in first
    assert list(json.loads(first)) == [
        "manifest_schema_version",
        "harness_version",
        "template_version",
        "generated_at",
        "managed_files",
        "generated_artifact_provenance",
        "integrity",
    ]


def test_v1_rendering_uses_its_own_versionless_contract():
    document = parse_manifest_json(_released_v1_text())
    rendered = render_manifest_json(document)

    assert "manifest_schema_version" not in rendered
    assert "generated_artifact_provenance" not in rendered
    assert parse_manifest_json(rendered) == document


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('{"harness_version": ', "manifest JSON is invalid"),
        ('{"manifest_schema_version":2,"manifest_schema_version":2}', "duplicate key"),
        (
            '{"manifest_schema_version":2,"integrity":{"note":"one","note":"two"}}',
            "duplicate key",
        ),
    ],
)
def test_malformed_or_duplicate_json_is_rejected(text, message):
    with pytest.raises(ManifestValidationError, match=message):
        parse_manifest_json(text)


@pytest.mark.parametrize("version", [1, 3, 0, -1])
def test_unsupported_explicit_schema_version_is_rejected(project_tmp, version):
    data = _v2_data(project_tmp)
    data["manifest_schema_version"] = version

    with pytest.raises(ManifestValidationError, match="unsupported manifest schema version"):
        parse_manifest_json(json.dumps(data))


@pytest.mark.parametrize("version", [True, False, "2", 2.0, None])
def test_schema_version_requires_integer_and_rejects_boolean(project_tmp, version):
    data = _v2_data(project_tmp)
    data["manifest_schema_version"] = version

    with pytest.raises(ManifestValidationError, match="must be integer 2"):
        parse_manifest_json(json.dumps(data))


def test_v2_unknown_top_level_field_is_rejected(project_tmp):
    data = _v2_data(project_tmp)
    data["unexpected"] = True

    with pytest.raises(ManifestValidationError, match="unknown field"):
        parse_manifest_json(json.dumps(data))


def test_unknown_nested_field_is_rejected(project_tmp):
    data = _v2_data(project_tmp)
    data["managed_files"][0]["unexpected"] = True

    with pytest.raises(ManifestValidationError, match="unknown field"):
        parse_manifest_json(json.dumps(data))


def test_v2_missing_required_field_is_rejected(project_tmp):
    data = _v2_data(project_tmp)
    data.pop("generated_artifact_provenance")

    with pytest.raises(ManifestValidationError, match="missing required field"):
        parse_manifest_json(json.dumps(data))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("harness_version", 1),
        ("template_version", None),
        ("generated_at", False),
        ("managed_files", {}),
        ("generated_artifact_provenance", {}),
        ("integrity", []),
    ],
)
def test_v2_wrong_field_types_are_rejected(project_tmp, field, value):
    data = _v2_data(project_tmp)
    data[field] = value

    with pytest.raises(ManifestValidationError):
        parse_manifest_json(json.dumps(data))


def test_duplicate_managed_paths_are_rejected(project_tmp):
    data = _v2_data(project_tmp)
    data["managed_files"].append(dict(data["managed_files"][0]))

    with pytest.raises(ManifestValidationError, match="duplicate path"):
        parse_manifest_json(json.dumps(data))


@pytest.mark.parametrize(
    "path",
    [
        "/absolute/file",
        "C:/drive/file",
        "C:\\drive\\file",
        ".harness\\config.yaml",
        "../escape",
        ".harness/../escape",
        ".harness//config.yaml",
        ".harness/./config.yaml",
    ],
)
def test_unsafe_or_non_normalized_managed_paths_are_rejected(project_tmp, path):
    data = _v2_data(project_tmp)
    data["managed_files"][0]["path"] = path

    with pytest.raises(ManifestValidationError):
        parse_manifest_json(json.dumps(data))


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, "", 4])
def test_malformed_sha256_is_rejected(project_tmp, digest):
    data = _v2_data(project_tmp)
    _managed_entry(data, ".harness/config.yaml")["sha256"] = digest

    with pytest.raises(ManifestValidationError, match="SHA-256|sha256|recorded hash"):
        parse_manifest_json(json.dumps(data))


@pytest.mark.parametrize(
    ("algorithm", "digest", "protected"),
    [
        ("sha256", "0" * 64, True),
        (None, None, False),
        (None, "0" * 64, True),
    ],
)
def test_invalid_manifest_self_record_is_rejected(project_tmp, algorithm, digest, protected):
    data = _v2_data(project_tmp)
    record = _managed_entry(data, ".harness/manifest.json")
    record["hash_algorithm"] = algorithm
    record["sha256"] = digest
    record["protected"] = protected

    with pytest.raises(ManifestValidationError, match="manifest self record"):
        parse_manifest_json(json.dumps(data))


def test_duplicate_provenance_output_path_is_rejected(project_tmp):
    data, output_path, digest = _v2_data_with_managed_output(project_tmp)
    provenance = {
        "output_path": output_path,
        "producer_command": "structural-test-producer",
        "producer_version": 1,
        "task_slug": "sample-task",
        "output_sha256": digest,
        "dependencies": [],
        "repository_observations": [],
    }
    data["generated_artifact_provenance"] = [provenance, dict(provenance)]

    with pytest.raises(ManifestValidationError, match="duplicate output_path"):
        parse_manifest_json(json.dumps(data))


def test_structural_provenance_with_unrecognized_producer_round_trips(project_tmp):
    data, output_path, digest = _v2_data_with_managed_output(project_tmp)
    data["generated_artifact_provenance"] = [
        {
            "output_path": output_path,
            "producer_command": "structural-test-producer",
            "producer_version": 1,
            "task_slug": "sample-task",
            "output_sha256": digest,
            "dependencies": [
                {
                    "dependency_path": "pyproject.toml",
                    "dependency_state": "present",
                    "dependency_sha256": "0" * 64,
                }
            ],
            "repository_observations": [
                {
                    "observation_path": "tests",
                    "predicate": "is_dir",
                    "result": True,
                }
            ],
        }
    ]

    document = parse_manifest_json(json.dumps(data))

    record = document.generated_artifact_provenance[0]
    assert isinstance(record, GeneratedArtifactProvenance)
    assert record.producer_command == "structural-test-producer"
    # Structural storage acceptance does not claim Lineage producer support.
    assert parse_manifest_json(render_manifest_json(document)) == document


def test_provenance_hash_must_match_protected_managed_record(project_tmp):
    data, output_path, _digest = _v2_data_with_managed_output(project_tmp)
    data["generated_artifact_provenance"] = [
        {
            "output_path": output_path,
            "producer_command": "structural-test-producer",
            "producer_version": 1,
            "task_slug": "sample-task",
            "output_sha256": "0" * 64,
            "dependencies": [],
            "repository_observations": [],
        }
    ]

    with pytest.raises(ManifestValidationError, match="contradicts managed-file hash"):
        parse_manifest_json(json.dumps(data))


def test_explicit_v2_write_preserves_existing_nonempty_provenance(project_tmp):
    data, output_path, digest = _v2_data_with_managed_output(project_tmp)
    provenance = {
        "output_path": output_path,
        "producer_command": "structural-test-producer",
        "producer_version": 7,
        "task_slug": "sample-task",
        "output_sha256": digest,
        "dependencies": [
            {
                "dependency_path": ".harness/tasks/sample-task/task.md",
                "dependency_state": "missing",
                "dependency_sha256": None,
            }
        ],
        "repository_observations": [
            {
                "observation_path": "tests",
                "predicate": "is_dir",
                "result": False,
            }
        ],
    }
    data["generated_artifact_provenance"] = [provenance]
    document = parse_manifest_json(json.dumps(data))
    _manifest_path(project_tmp).write_text(
        render_manifest_json(document),
        encoding="utf-8",
        newline="\n",
    )
    unrelated_before = _managed_entry(data, ".harness/state.json")

    write_manifest(project_tmp, upgrade_to_v2=True)

    after = json.loads(_manifest_path(project_tmp).read_text(encoding="utf-8"))
    assert after["generated_artifact_provenance"] == [provenance]
    assert _managed_entry(after, ".harness/state.json") == unrelated_before


def test_targeted_record_replacement_preserves_unrelated_records(project_tmp):
    data, output_path, output_digest = _v2_data_with_managed_output(project_tmp)
    data["generated_artifact_provenance"] = [
        {
            "output_path": output_path,
            "producer_command": "structural-test-producer",
            "producer_version": 1,
            "task_slug": "sample-task",
            "output_sha256": output_digest,
            "dependencies": [],
            "repository_observations": [],
        }
    ]
    document = parse_manifest_json(json.dumps(data))
    before = {record.path: record for record in document.managed_files}
    provenance_before = document.generated_artifact_provenance
    replacement = ManagedFileRecord(
        path=".harness/config.yaml",
        protected=True,
        hash_algorithm="sha256",
        sha256="0" * 64,
    )

    updated = replace_managed_file_records(
        document,
        [replacement],
        generated_at="2026-07-28T00:00:00+00:00",
    )
    after = {record.path: record for record in updated.managed_files}

    assert after[".harness/config.yaml"] == replacement
    assert updated.generated_artifact_provenance == provenance_before
    for path, record in before.items():
        if path != ".harness/config.yaml":
            assert after[path] == record


def test_targeted_v1_update_does_not_migrate(project_tmp):
    document = parse_manifest_json(_released_v1_text())
    current = next(record for record in document.managed_files if record.path == ".harness/config.yaml")

    updated = replace_managed_file_records(
        document,
        [current],
        generated_at="2026-07-28T00:00:00+00:00",
    )

    assert isinstance(updated, ManifestV1)


def test_non_lineage_write_does_not_accidentally_migrate_v1(project_tmp):
    _v2_data(project_tmp)
    _write_released_v1(project_tmp)

    write_manifest(project_tmp)

    assert isinstance(load_manifest_model(_manifest_path(project_tmp)), ManifestV1)


def test_explicit_targeted_v1_upgrade_creates_empty_v2_provenance(project_tmp):
    document = parse_manifest_json(_released_v1_text())
    current = next(record for record in document.managed_files if record.path == ".harness/config.yaml")

    updated = replace_managed_file_records(
        document,
        [current],
        generated_at="2026-07-28T00:00:00+00:00",
        upgrade_to_v2=True,
    )

    assert isinstance(updated, ManifestV2)
    assert updated.generated_artifact_provenance == ()


def test_released_v1_rejects_fields_outside_frozen_contract():
    data = json.loads(_released_v1_text())
    data["generated_artifact_provenance"] = []

    with pytest.raises(ManifestValidationError, match="unknown field"):
        parse_manifest_json(json.dumps(data))


def test_targeted_file_update_writes_canonical_exact_bytes(project_tmp):
    _v2_data(project_tmp)
    config_path = project_tmp / ".harness" / "config.yaml"
    replacement = ManagedFileRecord(
        path=".harness/config.yaml",
        protected=True,
        hash_algorithm="sha256",
        sha256=sha256_file(config_path),
    )

    update_managed_file_records(
        project_tmp,
        [replacement],
        generated_at="2026-07-28T00:00:00+00:00",
    )

    persisted = _manifest_path(project_tmp).read_bytes()
    model = load_manifest_model(_manifest_path(project_tmp))
    assert persisted == render_manifest_json(model).encode("utf-8")


def test_manifest_replacement_failure_preserves_existing_destination(project_tmp, monkeypatch):
    _v2_data(project_tmp)
    manifest_path = _manifest_path(project_tmp)
    before = manifest_path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("replacement failed")

    monkeypatch.setattr("ai_sdlc_harness.files.os.replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        write_manifest(project_tmp)

    assert manifest_path.read_bytes() == before
    assert not list(manifest_path.parent.glob(".manifest.json.*.tmp"))


def _document_with_two_provenance(root):
    _v2_data(root)
    paths = (
        ".harness/tasks/sample-task/spec.md",
        ".harness/tasks/sample-task/requirements.yaml",
    )
    for path_text in paths:
        target = root / Path(*PurePosixPath(path_text).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path_text.encode("utf-8"))
    managed = tuple(build_managed_file_record(root, path) for path in paths)
    provenance = tuple(
        GeneratedArtifactProvenance(
            output_path=record.path,
            producer_command="spec",
            producer_version=1,
            task_slug="sample-task",
            output_sha256=record.sha256,
            dependencies=(),
            repository_observations=(),
        )
        for record in managed
    )
    document = load_manifest_model(_manifest_path(root))
    updated = replace_managed_and_provenance_records(
        document,
        managed,
        provenance,
        generated_at="2026-07-29T00:00:00+00:00",
    )
    return updated, managed, provenance


def test_remove_one_provenance_preserves_unrelated_and_all_managed(project_tmp):
    document, _managed, provenance = _document_with_two_provenance(project_tmp)

    updated = remove_provenance_records(
        document,
        [provenance[0].output_path],
        generated_at="2026-07-29T00:00:01+00:00",
    )

    assert updated.managed_files == document.managed_files
    assert updated.generated_artifact_provenance == (provenance[1],)


def test_remove_multiple_provenance_records(project_tmp):
    document, _managed, provenance = _document_with_two_provenance(project_tmp)

    updated = remove_provenance_records(
        document,
        [record.output_path for record in provenance],
        generated_at="2026-07-29T00:00:01+00:00",
    )

    assert updated.generated_artifact_provenance == ()
    assert updated.managed_files == document.managed_files


def test_remove_provenance_rejects_duplicate_requested_outputs(project_tmp):
    document, _managed, provenance = _document_with_two_provenance(project_tmp)

    with pytest.raises(ManifestValidationError, match="duplicate output paths"):
        remove_provenance_records(
            document,
            [provenance[0].output_path, provenance[0].output_path],
            generated_at="2026-07-29T00:00:01+00:00",
        )


def test_remove_provenance_v1_upgrade_is_explicit():
    document = parse_manifest_json(_released_v1_text())

    unchanged = remove_provenance_records(
        document,
        [".harness/tasks/sample-task/spec.md"],
        generated_at="2026-07-29T00:00:00+00:00",
    )
    upgraded = remove_provenance_records(
        document,
        [".harness/tasks/sample-task/spec.md"],
        generated_at="2026-07-29T00:00:00+00:00",
        upgrade_to_v2=True,
    )

    assert isinstance(unchanged, ManifestV1)
    assert unchanged == document
    assert isinstance(upgraded, ManifestV2)
    assert upgraded.managed_files == document.managed_files
    assert upgraded.generated_artifact_provenance == ()


def test_combined_replacement_preserves_unrelated_records(project_tmp):
    document, managed, provenance = _document_with_two_provenance(project_tmp)
    config_before = next(
        record
        for record in document.managed_files
        if record.path == ".harness/config.yaml"
    )

    updated = replace_managed_and_provenance_records(
        document,
        [managed[0]],
        [provenance[0]],
        generated_at="2026-07-29T00:00:02+00:00",
    )

    assert updated == document
    assert next(
        record
        for record in updated.managed_files
        if record.path == ".harness/config.yaml"
    ) == config_before
    assert updated.generated_artifact_provenance == (
        document.generated_artifact_provenance
    )


def test_combined_replacement_rejects_hash_contradiction(project_tmp):
    document, managed, provenance = _document_with_two_provenance(project_tmp)
    contradictory = GeneratedArtifactProvenance(
        output_path=provenance[0].output_path,
        producer_command=provenance[0].producer_command,
        producer_version=provenance[0].producer_version,
        task_slug=provenance[0].task_slug,
        output_sha256="0" * 64,
        dependencies=provenance[0].dependencies,
        repository_observations=provenance[0].repository_observations,
    )

    with pytest.raises(
        ManifestValidationError,
        match="contradicts managed-file hash",
    ):
        replace_managed_and_provenance_records(
            document,
            [managed[0]],
            [contradictory],
            generated_at="2026-07-29T00:00:02+00:00",
        )


def test_combined_replacement_rejects_duplicate_outputs(project_tmp):
    document, managed, provenance = _document_with_two_provenance(project_tmp)

    with pytest.raises(ManifestValidationError, match="duplicate replacement"):
        replace_managed_and_provenance_records(
            document,
            [managed[0], managed[0]],
            [provenance[0], provenance[0]],
            generated_at="2026-07-29T00:00:02+00:00",
        )


def test_complete_noop_can_preserve_exact_manifest_bytes(project_tmp):
    document, managed, provenance = _document_with_two_provenance(project_tmp)
    persist_manifest_model(project_tmp, document)
    before = _manifest_path(project_tmp).read_bytes()

    updated = replace_managed_and_provenance_records(
        document,
        managed,
        provenance,
        generated_at="2099-01-01T00:00:00+00:00",
    )
    if updated != document:
        persist_manifest_model(project_tmp, updated)

    assert updated == document
    assert _manifest_path(project_tmp).read_bytes() == before


def test_manifest_v2_round_trips_closed_validation_provenance(project_tmp):
    _v2_data(project_tmp)
    expected = validation_provenance_definition_for_task("sample-task")
    output = project_tmp / Path(*PurePosixPath(expected.output_path).parts)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"validation report\n")
    managed = build_managed_file_record(project_tmp, expected.output_path)
    provenance = build_provenance_record(
        expected,
        output_sha256=managed.sha256,
        dependencies=tuple(
            ProvenanceDependency(path, "missing", None)
            for path in expected.dependencies
        ),
        repository_observations=tuple(
            ProvenanceObservation(
                item.relative_path,
                item.predicate,
                False,
            )
            for item in expected.repository_observations
        ),
    )
    document = load_manifest_model(_manifest_path(project_tmp))
    updated = replace_managed_and_provenance_records(
        document,
        (managed,),
        (provenance,),
        generated_at="2026-09-17T00:00:00+00:00",
    )
    rendered = render_manifest_json(updated)
    parsed = parse_manifest_json(rendered)

    assert isinstance(parsed, ManifestV2)
    assert validate_manifest_provenance(parsed) is parsed
    assert parsed.generated_artifact_provenance == (provenance,)
