from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath

import pytest
import yaml

from ai_sdlc_harness.files import sha256_bytes
from ai_sdlc_harness.validation import (
    CapturedValidationDependency,
    CapturedValidationObservation,
    ValidationDependencyState,
    ValidationCurrentnessFinding,
    ValidationCurrentnessResult,
    ValidationCurrentnessState,
    ValidationInspectionError,
    ValidationInspectionSnapshot,
    ValidationObservationPredicate,
    capture_validation_snapshot,
    inspect_validation_snapshot,
    validation_dependency_paths,
    validation_observation_definitions,
    validation_provenance_definition_for_task,
    validation_provenance_from_snapshot,
)


SLUG = "sample-task"


def _requirements_text(slug: str = SLUG, *, status: str = "draft") -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "artifact_role": "advisory_projection",
            "authority": "non_authoritative",
            "edit_model": "edit_source_task_artifacts_and_rerun_spec",
            "task_slug": slug,
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
                    "statement": "Produce a validation result.",
                    "status": status,
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


def _write_clean_repository(root: Path) -> None:
    contents = {
        "task.md": "# Task\n\nImplement validation inspection.\n",
        "acceptance.md": "# Acceptance\n\n- Inspection is deterministic.\n",
        "architecture-notes.md": "# Architecture\n\nKeep inspection pure.\n",
        "coupling-notes.md": "# Coupling\n\nNo new runtime coupling.\n",
        "test-contract.md": "# Tests\n\n- Exercise captured inputs.\n",
        "verification.md": "# Verification\n\npytest tests\n\nTests passed in 1.0s.\n",
        "evidence.md": "# Evidence\n\nImplementation evidence recorded.\n",
        "preflight.md": "# Preflight\n\nNo findings.\n",
        "spec.md": "# Specification\n\nReady.\n",
        "requirements.yaml": _requirements_text(),
        "test-contract-review.md": "# Test Contract Review\n\nNo findings.\n",
        "agent-workset.md": "# Workset\n\nImplement the task.\n",
        "evidence-report.md": "# Evidence Report\n\nNo findings.\n",
    }
    for relative_path in validation_dependency_paths(SLUG):
        target = root.joinpath(*relative_path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents[relative_path.name], encoding="utf-8", newline="")
    (root / ".harness" / "config.yaml").write_text("schema_version: 1\n")
    (root / ".harness" / "manifest.json").write_text("{}\n")
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / ".github" / "workflows").mkdir(parents=True)


def _clean_snapshot(root: Path) -> ValidationInspectionSnapshot:
    _write_clean_repository(root)
    return capture_validation_snapshot(root, SLUG)


def _missing_dependency(path: PurePosixPath) -> CapturedValidationDependency:
    return CapturedValidationDependency(
        path, ValidationDependencyState.MISSING, None, None, None
    )


def test_snapshot_uses_exact_closed_dependency_and_observation_contract(project_tmp):
    snapshot = _clean_snapshot(project_tmp)

    assert tuple(item.path for item in snapshot.dependencies) == (
        PurePosixPath(".harness/tasks/sample-task/task.md"),
        PurePosixPath(".harness/tasks/sample-task/acceptance.md"),
        PurePosixPath(".harness/tasks/sample-task/architecture-notes.md"),
        PurePosixPath(".harness/tasks/sample-task/coupling-notes.md"),
        PurePosixPath(".harness/tasks/sample-task/test-contract.md"),
        PurePosixPath(".harness/tasks/sample-task/verification.md"),
        PurePosixPath(".harness/tasks/sample-task/evidence.md"),
        PurePosixPath(".harness/tasks/sample-task/preflight.md"),
        PurePosixPath(".harness/tasks/sample-task/spec.md"),
        PurePosixPath(".harness/tasks/sample-task/requirements.yaml"),
        PurePosixPath(".harness/tasks/sample-task/test-contract-review.md"),
        PurePosixPath(
            ".harness/tasks/sample-task/generated/agent-workset.md"
        ),
        PurePosixPath(".harness/tasks/sample-task/evidence-report.md"),
    )
    assert tuple(
        (item.path, item.predicate) for item in snapshot.repository_observations
    ) == (
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
            PurePosixPath(".harness/tasks/sample-task"),
            ValidationObservationPredicate.IS_DIR,
        ),
        (PurePosixPath("tests"), ValidationObservationPredicate.IS_DIR),
        (PurePosixPath("pytest.ini"), ValidationObservationPredicate.IS_FILE),
        (
            PurePosixPath(".github/workflows"),
            ValidationObservationPredicate.IS_DIR,
        ),
    )
    path_texts = {item.path.as_posix() for item in snapshot.dependencies}
    assert not any("validation-report.md" in path for path in path_texts)
    assert ".harness/manifest.json" not in path_texts
    assert not any("context-manifest.yaml" in path for path in path_texts)
    assert len(snapshot.dependencies) == 13
    assert len(snapshot.repository_observations) == 7


def test_capture_preserves_exact_bytes_text_and_digest(project_tmp):
    _write_clean_repository(project_tmp)
    paths = validation_dependency_paths(SLUG)
    task = project_tmp.joinpath(*paths[0].parts)
    acceptance = project_tmp.joinpath(*paths[1].parts)
    architecture = project_tmp.joinpath(*paths[2].parts)
    task.write_bytes(b"line one\nline two\n")
    acceptance.write_bytes(b"line one\r\nline two\r\n")
    architecture.write_bytes("café ☕\n".encode("utf-8"))

    snapshot = capture_validation_snapshot(project_tmp, SLUG)
    captured = {item.path.name: item for item in snapshot.dependencies}

    assert captured["task.md"].raw_bytes == b"line one\nline two\n"
    assert captured["acceptance.md"].raw_bytes == b"line one\r\nline two\r\n"
    assert captured["task.md"].sha256 != captured["acceptance.md"].sha256
    assert captured["architecture-notes.md"].decoded_text == "café ☕\n"
    assert captured["architecture-notes.md"].sha256 == sha256_bytes(
        "café ☕\n".encode("utf-8")
    )


def test_capture_classifies_missing_invalid_utf8_and_non_regular(project_tmp):
    _write_clean_repository(project_tmp)
    paths = validation_dependency_paths(SLUG)
    missing = project_tmp.joinpath(*paths[0].parts)
    invalid = project_tmp.joinpath(*paths[1].parts)
    non_regular = project_tmp.joinpath(*paths[2].parts)
    missing.unlink()
    invalid.write_bytes(b"\xff")
    non_regular.unlink()
    non_regular.mkdir()

    snapshot = capture_validation_snapshot(project_tmp, SLUG)
    by_path = {item.path: item for item in snapshot.dependencies}

    assert by_path[paths[0]].state is ValidationDependencyState.MISSING
    assert by_path[paths[1]].state is ValidationDependencyState.UNREADABLE
    assert by_path[paths[2]].state is ValidationDependencyState.NOT_REGULAR
    for path in paths[:3]:
        assert by_path[path].raw_bytes is None
        assert by_path[path].decoded_text is None
        assert by_path[path].sha256 is None


def test_capture_does_not_trust_final_symlink(project_tmp, monkeypatch):
    _write_clean_repository(project_tmp)
    task_path = project_tmp.joinpath(*validation_dependency_paths(SLUG)[0].parts)
    real_is_symlink = Path.is_symlink

    def report_task_symlink(path):
        return path == task_path or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_task_symlink)

    snapshot = capture_validation_snapshot(project_tmp, SLUG)

    assert snapshot.dependencies[0].state is ValidationDependencyState.UNREADABLE
    assert snapshot.dependencies[0].raw_bytes is None


def test_capture_does_not_trust_symlinked_parent(project_tmp, monkeypatch):
    _write_clean_repository(project_tmp)
    linked_parent = project_tmp / ".harness" / "tasks"
    real_is_symlink = Path.is_symlink

    def report_parent_symlink(path):
        return path == linked_parent or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_parent_symlink)

    snapshot = capture_validation_snapshot(project_tmp, SLUG)

    assert all(
        item.state is ValidationDependencyState.UNREADABLE
        for item in snapshot.dependencies
    )
    assert all(item.raw_bytes is None for item in snapshot.dependencies)


@pytest.mark.parametrize(
    ("raw_bytes", "decoded_text", "digest"),
    [
        (None, None, None),
        (b"text", None, sha256_bytes(b"text")),
        (b"text", "different", sha256_bytes(b"text")),
        (b"text", "text", "0" * 64),
    ],
)
def test_present_dependency_invariants(raw_bytes, decoded_text, digest):
    with pytest.raises(ValidationInspectionError):
        CapturedValidationDependency(
            PurePosixPath("task.md"),
            ValidationDependencyState.PRESENT,
            raw_bytes,
            decoded_text,
            digest,
        )


def test_non_present_dependency_rejects_fabricated_content():
    with pytest.raises(ValidationInspectionError):
        CapturedValidationDependency(
            PurePosixPath("task.md"),
            ValidationDependencyState.MISSING,
            b"fabricated",
            "fabricated",
            sha256_bytes(b"fabricated"),
        )


@pytest.mark.parametrize(
    "path",
    [PurePosixPath("../task.md"), PurePosixPath("/task.md"), PurePosixPath(".")],
)
def test_capture_models_reject_unsafe_paths(path):
    with pytest.raises(ValidationInspectionError):
        _missing_dependency(path)


@pytest.mark.parametrize("slug", ["", "../bad", "bad/slug", "UPPER"])
def test_snapshot_rejects_invalid_task_slug(project_tmp, slug):
    snapshot = _clean_snapshot(project_tmp)

    with pytest.raises(ValidationInspectionError):
        ValidationInspectionSnapshot(
            slug, snapshot.dependencies, snapshot.repository_observations
        )


def test_snapshot_rejects_mutable_or_malformed_dependency_collections(project_tmp):
    snapshot = _clean_snapshot(project_tmp)
    dependencies = snapshot.dependencies
    variants = (
        dependencies[:-1],
        (*dependencies, dependencies[-1]),
        (dependencies[1], dependencies[0], *dependencies[2:]),
        (dependencies[0], dependencies[0], *dependencies[2:]),
    )

    with pytest.raises(ValidationInspectionError):
        ValidationInspectionSnapshot(
            SLUG, list(dependencies), snapshot.repository_observations
        )
    for variant in variants:
        with pytest.raises(ValidationInspectionError):
            ValidationInspectionSnapshot(
                SLUG, variant, snapshot.repository_observations
            )


def test_snapshot_rejects_mutable_or_malformed_observation_collections(project_tmp):
    snapshot = _clean_snapshot(project_tmp)
    observations = snapshot.repository_observations
    variants = (
        observations[:-1],
        (*observations, observations[-1]),
        (observations[1], observations[0], *observations[2:]),
        (observations[0], observations[0], *observations[2:]),
    )

    with pytest.raises(ValidationInspectionError):
        ValidationInspectionSnapshot(SLUG, snapshot.dependencies, list(observations))
    for variant in variants:
        with pytest.raises(ValidationInspectionError):
            ValidationInspectionSnapshot(SLUG, snapshot.dependencies, variant)


def test_snapshot_and_nested_models_are_frozen(project_tmp):
    snapshot = _clean_snapshot(project_tmp)

    with pytest.raises(FrozenInstanceError):
        snapshot.task_slug = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot.dependencies[0].state = ValidationDependencyState.MISSING


def test_inspection_performs_no_filesystem_reads(project_tmp, monkeypatch):
    snapshot = _clean_snapshot(project_tmp)

    def fail(*_args, **_kwargs):
        raise AssertionError("pure inspection touched the filesystem")

    monkeypatch.setattr(Path, "read_bytes", fail)
    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(Path, "exists", fail)
    monkeypatch.setattr(Path, "is_file", fail)
    monkeypatch.setattr(Path, "is_dir", fail)

    result = inspect_validation_snapshot(snapshot)

    assert result.task_slug == SLUG


def test_equal_snapshots_produce_equal_deterministic_clean_results(project_tmp):
    snapshot = _clean_snapshot(project_tmp)

    first = inspect_validation_snapshot(snapshot)
    second = inspect_validation_snapshot(snapshot)

    assert first == second
    assert first.blocker_count == 0
    assert first.warning_count == 0
    assert first.info_count == 7
    assert first.clean is True
    assert first.review_readiness == "Yes"


def test_clean_is_false_for_warning_or_blocker(project_tmp):
    clean_snapshot = _clean_snapshot(project_tmp)
    observations = list(clean_snapshot.repository_observations)
    observations[-1] = replace(observations[-1], result=False)
    warning_snapshot = ValidationInspectionSnapshot(
        SLUG, clean_snapshot.dependencies, tuple(observations)
    )
    dependencies = list(clean_snapshot.dependencies)
    dependencies[0] = _missing_dependency(dependencies[0].path)
    blocker_snapshot = ValidationInspectionSnapshot(
        SLUG, tuple(dependencies), clean_snapshot.repository_observations
    )

    warning = inspect_validation_snapshot(warning_snapshot)
    blocker = inspect_validation_snapshot(blocker_snapshot)

    assert warning.blocker_count == 0
    assert warning.warning_count == 1
    assert warning.clean is False
    assert blocker.blocker_count >= 1
    assert blocker.clean is False


def test_existing_task_artifact_and_aggregate_rules_are_preserved(project_tmp):
    snapshot = _clean_snapshot(project_tmp)
    dependencies = list(snapshot.dependencies)
    dependencies[0] = _missing_dependency(dependencies[0].path)
    dependencies[1] = CapturedValidationDependency(
        dependencies[1].path,
        ValidationDependencyState.UNREADABLE,
        None,
        None,
        None,
    )
    empty = b""
    dependencies[4] = CapturedValidationDependency(
        dependencies[4].path,
        ValidationDependencyState.PRESENT,
        empty,
        "",
        sha256_bytes(empty),
    )
    todo = b"# Verification\n\nTODO\n"
    dependencies[5] = CapturedValidationDependency(
        dependencies[5].path,
        ValidationDependencyState.PRESENT,
        todo,
        todo.decode(),
        sha256_bytes(todo),
    )
    dependencies[6] = _missing_dependency(dependencies[6].path)
    result = inspect_validation_snapshot(
        ValidationInspectionSnapshot(
            SLUG, tuple(dependencies), snapshot.repository_observations
        )
    )
    messages = {finding.message for finding in result.findings}

    assert "task.md is missing." in messages
    assert "acceptance.md is unreadable." in messages
    assert "test-contract.md is empty." in messages
    assert "verification.md is TODO-only." in messages
    assert (
        "no usable acceptance criteria and no usable test-contract content are available."
        in messages
    )
    assert (
        "no usable evidence and no usable verification content are available."
        in messages
    )


def test_semantic_warnings_and_imported_findings_are_preserved_and_deduplicated(
    project_tmp,
):
    _write_clean_repository(project_tmp)
    root = project_tmp / ".harness" / "tasks" / SLUG
    (root / "task.md").write_text("# Task\n\nTODO decide later.\n")
    (root / "verification.md").write_text("# Verification\n\nNo command.\n")
    (root / "evidence.md").write_text("# Evidence\n\nNo result.\n")
    (root / "preflight.md").write_text(
        "- warning: shared issue.\n- info: useful context.\n"
    )
    (root / "test-contract-review.md").write_text(
        "- warning: SHARED ISSUE.\n"
    )
    (root / "evidence-report.md").write_text("- blocker: evidence gap.\n")
    (project_tmp / ".github" / "workflows").rmdir()
    (project_tmp / "tests").rmdir()
    (project_tmp / "pytest.ini").unlink()

    result = inspect_validation_snapshot(
        capture_validation_snapshot(project_tmp, SLUG)
    )
    messages = [finding.message for finding in result.findings]

    assert "unresolved TODOs remain in: task.md." in messages
    assert "no verification command recorded." in messages
    assert "no test result evidence recorded." in messages
    assert "no CI detected." in messages
    assert "no test framework detected." in messages
    assert messages.count("preflight.md reported warning: shared issue.") == 1
    assert not any("test-contract-review.md reported warning" in item for item in messages)
    assert "evidence-report.md reported blocker: evidence gap." in messages
    assert result.info_count == 8


def test_prior_report_finding_import_is_bounded_to_thirty(project_tmp):
    _write_clean_repository(project_tmp)
    report = project_tmp / ".harness" / "tasks" / SLUG / "preflight.md"
    report.write_text(
        "".join(f"- warning: bounded finding {index}.\n" for index in range(40))
    )

    result = inspect_validation_snapshot(
        capture_validation_snapshot(project_tmp, SLUG)
    )

    assert len(result.report_findings) == 30
    assert sum("reported warning: bounded finding" in item.message for item in result.findings) == 30


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("not: [valid\n", "requirements.yaml is malformed YAML:"),
        (
            _requirements_text(status="verified"),
            "invalid requirement status: REQ-001 uses verified.",
        ),
    ],
)
def test_requirements_are_validated_from_captured_text(project_tmp, content, problem):
    _write_clean_repository(project_tmp)
    target = (
        project_tmp
        / ".harness"
        / "tasks"
        / SLUG
        / "requirements.yaml"
    )
    target.write_text(content, encoding="utf-8")

    result = inspect_validation_snapshot(
        capture_validation_snapshot(project_tmp, SLUG)
    )

    assert result.structured_requirements.schema_valid is False
    assert any(
        finding.level == "blocker" and problem in finding.message
        for finding in result.findings
    )


def test_missing_or_unreadable_semantic_reports_are_warnings(project_tmp):
    snapshot = _clean_snapshot(project_tmp)
    dependencies = list(snapshot.dependencies)
    dependencies[7] = _missing_dependency(dependencies[7].path)
    dependencies[10] = CapturedValidationDependency(
        dependencies[10].path,
        ValidationDependencyState.UNREADABLE,
        None,
        None,
        None,
    )

    result = inspect_validation_snapshot(
        ValidationInspectionSnapshot(
            SLUG, tuple(dependencies), snapshot.repository_observations
        )
    )
    messages = {finding.message for finding in result.findings}

    assert "preflight.md is missing." in messages
    assert "test-contract-review.md is unreadable." in messages
    assert result.clean is False


def test_semantics_ignore_manifest_context_manifest_and_unrelated_task_files(
    project_tmp,
):
    first_snapshot = _clean_snapshot(project_tmp)
    task_root = project_tmp / ".harness" / "tasks" / SLUG
    (project_tmp / ".harness" / "manifest.json").write_text(
        '{"managed_files": [{"path": "unrelated", "sha256": "drift"}]}\n'
    )
    generated = task_root / "generated"
    (generated / "context-manifest.yaml").write_text("changed: true\n")
    (task_root / "arbitrary-user-note.md").write_text("unrelated drift\n")

    second_snapshot = capture_validation_snapshot(project_tmp, SLUG)

    assert first_snapshot == second_snapshot
    assert inspect_validation_snapshot(first_snapshot) == inspect_validation_snapshot(
        second_snapshot
    )


def test_canonical_contract_helpers_reject_bad_slug():
    with pytest.raises(ValidationInspectionError):
        validation_dependency_paths("../bad")
    with pytest.raises(ValidationInspectionError):
        validation_observation_definitions("../bad")


def test_observation_model_rejects_unknown_predicate():
    with pytest.raises(ValidationInspectionError):
        CapturedValidationObservation(
            PurePosixPath("tests"), "exists", True
        )


def test_validation_provenance_definition_is_closed_and_separate_from_lineage(
    project_tmp,
):
    snapshot = _clean_snapshot(project_tmp)
    expected = validation_provenance_definition_for_task(SLUG)
    record = validation_provenance_from_snapshot(
        snapshot,
        output_sha256="a" * 64,
    )

    assert expected.registry_id == "validation_report"
    assert expected.producer_command == "validate"
    assert expected.producer_version == 1
    assert expected.output_path == (
        ".harness/tasks/sample-task/validation-report.md"
    )
    assert expected.dependencies == tuple(
        path.as_posix() for path in validation_dependency_paths(SLUG)
    )
    assert tuple(
        (item.relative_path, item.predicate)
        for item in expected.repository_observations
    ) == tuple(
        (path.as_posix(), predicate.value)
        for path, predicate in validation_observation_definitions(SLUG)
    )
    assert record.output_sha256 == "a" * 64
    assert len(record.dependencies) == 13
    assert len(record.repository_observations) == 7
    assert ".harness/manifest.json" not in {
        item.dependency_path for item in record.dependencies
    }
    assert not hasattr(record, "generated_at")


def test_validation_provenance_projects_exact_snapshot_values(project_tmp):
    snapshot = _clean_snapshot(project_tmp)
    record = validation_provenance_from_snapshot(
        snapshot,
        output_sha256="b" * 64,
    )
    by_dependency = {
        item.dependency_path: item for item in record.dependencies
    }
    by_observation = {
        (item.observation_path, item.predicate): item
        for item in record.repository_observations
    }

    for captured in snapshot.dependencies:
        persisted = by_dependency[captured.path.as_posix()]
        assert persisted.dependency_state == captured.state.value
        assert persisted.dependency_sha256 == captured.sha256
    for captured in snapshot.repository_observations:
        persisted = by_observation[
            (captured.path.as_posix(), captured.predicate.value)
        ]
        assert persisted.result is captured.result


def test_validation_provenance_rejects_uninspectable_observation(project_tmp):
    snapshot = _clean_snapshot(project_tmp)
    observations = list(snapshot.repository_observations)
    observations[-1] = replace(observations[-1], inspectable=False)
    unavailable = ValidationInspectionSnapshot(
        SLUG,
        snapshot.dependencies,
        tuple(observations),
    )

    with pytest.raises(
        ValidationInspectionError,
        match="requires a deterministic repository observation",
    ):
        validation_provenance_from_snapshot(
            unavailable,
            output_sha256="c" * 64,
        )


def test_validation_currentness_result_is_frozen_and_canonical():
    warning = ValidationCurrentnessFinding(
        "legacy_validation_manifest",
        "warning",
        ".harness/manifest.json",
        "Rerun validate.",
    )
    blocker = ValidationCurrentnessFinding(
        "validation_dependency_uninspectable",
        "blocker",
        ".harness/tasks/sample-task/task.md",
        "Dependency cannot be inspected.",
    )
    result = ValidationCurrentnessResult(
        SLUG,
        ValidationCurrentnessState.UNKNOWN,
        (warning, blocker, warning),
    )

    assert result.findings == (blocker, warning)
    with pytest.raises(FrozenInstanceError):
        result.state = ValidationCurrentnessState.CURRENT
