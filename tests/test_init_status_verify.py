from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.manifest import (
    ManifestV2,
    ProvenanceDependency,
    ProvenanceObservation,
    build_managed_file_record,
    load_manifest_model,
    persist_manifest_model,
    update_managed_file_records,
    write_manifest,
)
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.validate import run_validate
from ai_sdlc_harness.validation import (
    ValidationCurrentnessState,
    resolve_validation_currentness,
)
from ai_sdlc_harness.verify import verify_project


EXPECTED_FILES = [
    ".harness/config.yaml",
    ".harness/state.json",
    ".harness/manifest.json",
    ".harness/generated/agent-instructions.md",
    ".harness/packs/selected.yaml",
]
RELEASED_V1_FIXTURE = Path(__file__).parent / "fixtures" / "main-manifest-v1.json"
LINEAGE_SLUG = "lineage-task"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _released_v1_bytes(root: Path) -> bytes:
    data = json.loads(RELEASED_V1_FIXTURE.read_text(encoding="utf-8"))
    for record in data["managed_files"]:
        if record["hash_algorithm"] == "sha256":
            target = root / record["path"]
            record["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _install_released_v1(root: Path) -> bytes:
    content = _released_v1_bytes(root)
    (root / ".harness" / "manifest.json").write_bytes(content)
    return content


def _initialize_with_preflight_provenance(root: Path) -> ManifestV2:
    assert init_project(root)[0] == 0
    assert start_task(root, "Lineage Task")[0] == 0
    assert run_preflight(root, LINEAGE_SLUG)[0] == 0
    manifest = load_manifest_model(root / ".harness" / "manifest.json")
    assert isinstance(manifest, ManifestV2)
    assert len(manifest.generated_artifact_provenance) == 1
    return manifest


def test_init_creates_expected_files(project_tmp):
    code, messages = init_project(project_tmp)

    assert code == 0
    assert (project_tmp / ".harness/tasks").is_dir()
    for relative in EXPECTED_FILES:
        assert (project_tmp / relative).is_file()
    assert "root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified" in messages

    config = yaml.safe_load(_read(project_tmp / ".harness/config.yaml"))
    assert config["harness_version"] == "1.0.0"
    assert config["template_version"] == "1.0.0"
    assert "mode" not in config
    assert config["adoption_scope"] == "task-local"
    assert config["selected_packs"] == []
    assert config["context_budget"] == {
        "per_file_warning_approximate_tokens": 4000,
        "per_file_high_risk_approximate_tokens": 8000,
        "total_warning_approximate_tokens": 12000,
        "total_high_risk_approximate_tokens": 24000,
    }


def test_initialized_config_content_is_deterministic(project_tmp):
    assert init_project(project_tmp)[0] == 0

    assert _read(project_tmp / ".harness/config.yaml") == """harness_version: 1.0.0
template_version: 1.0.0
adoption_scope: task-local
selected_packs: []
context_budget:
  per_file_warning_approximate_tokens: 4000
  per_file_high_risk_approximate_tokens: 8000
  total_warning_approximate_tokens: 12000
  total_high_risk_approximate_tokens: 24000
adapters:
  codex: not-installed
  claude_code: not-installed
  gemini_cli: not-installed
project:
  git_repo: false
  detected_languages: []
  detected_package_managers: []
  detected_test_frameworks: []
  detected_ci: []
  existing_agent_files: []
"""


def test_init_records_deferred_adapter_request(project_tmp):
    code, messages = init_project(project_tmp, agent="both")

    assert code == 0
    config = yaml.safe_load(_read(project_tmp / ".harness/config.yaml"))
    assert config["adapters"] == {
        "codex": "requested-deferred",
        "claude_code": "requested-deferred",
        "gemini_cli": "not-installed",
    }
    assert any("adapter installation is not performed by init" in message for message in messages)
    selected = yaml.safe_load(_read(project_tmp / ".harness/packs/selected.yaml"))
    assert selected["selected_packs"] == []


def test_init_is_idempotent(project_tmp):
    assert init_project(project_tmp)[0] == 0
    before = {relative: _read(project_tmp / relative) for relative in EXPECTED_FILES}

    code, messages = init_project(project_tmp)
    after = {relative: _read(project_tmp / relative) for relative in EXPECTED_FILES}

    assert code == 0
    assert before == after
    assert before[".harness/config.yaml"] == after[".harness/config.yaml"]
    assert before[".harness/generated/agent-instructions.md"] == after[".harness/generated/agent-instructions.md"]
    assert any("skip existing file .harness/config.yaml" == message for message in messages)
    assert "skip existing manifest .harness/manifest.json" in messages


def test_init_does_not_rewrite_legacy_config_without_context_budget(project_tmp):
    assert init_project(project_tmp)[0] == 0
    config_path = project_tmp / ".harness" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    del config["context_budget"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    write_manifest(project_tmp)
    config_before = config_path.read_bytes()
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = init_project(project_tmp)

    assert code == 0
    assert config_path.read_bytes() == config_before
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before
    assert "skip existing file .harness/config.yaml" in messages
    assert "skip existing manifest .harness/manifest.json" in messages


def test_init_dry_run_writes_nothing(project_tmp):
    code, messages = init_project(project_tmp, dry_run=True)

    assert code == 0
    assert not (project_tmp / ".harness").exists()
    assert any(message.startswith("would create") for message in messages)


def test_init_force_rewrites_only_managed_harness_files(project_tmp):
    assert init_project(project_tmp)[0] == 0
    unmanaged = project_tmp / ".harness" / "user-note.md"
    unmanaged.write_text("keep me\n", encoding="utf-8")
    config = project_tmp / ".harness" / "config.yaml"
    config.write_text("broken: true\n", encoding="utf-8")

    code, _ = init_project(project_tmp, force=True, agent="codex")

    assert code == 0
    assert unmanaged.read_text(encoding="utf-8") == "keep me\n"
    new_config = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert new_config["adapters"]["codex"] == "requested-deferred"
    manifest = json.loads((project_tmp / ".harness" / "manifest.json").read_text(encoding="utf-8"))
    config_entry = next(item for item in manifest["managed_files"] if item["path"] == ".harness/config.yaml")
    assert config_entry["sha256"]


def test_existing_agent_files_are_never_overwritten(project_tmp):
    agents = project_tmp / "AGENTS.md"
    claude = project_tmp / "CLAUDE.md"
    gemini = project_tmp / "GEMINI.md"
    agents.write_text("existing agents\n", encoding="utf-8")
    claude.write_text("existing claude\n", encoding="utf-8")
    gemini.write_text("existing gemini\n", encoding="utf-8")

    code, _ = init_project(project_tmp, force=True, agent="both")

    assert code == 0
    assert agents.read_text(encoding="utf-8") == "existing agents\n"
    assert claude.read_text(encoding="utf-8") == "existing claude\n"
    assert gemini.read_text(encoding="utf-8") == "existing gemini\n"


def test_init_records_gemini_request_without_installing_skill(project_tmp):
    code, messages = init_project(project_tmp, agent="gemini-cli")

    assert code == 0
    config = yaml.safe_load(_read(project_tmp / ".harness/config.yaml"))
    assert config["adapters"] == {
        "codex": "not-installed",
        "claude_code": "not-installed",
        "gemini_cli": "requested-deferred",
    }
    assert not (project_tmp / ".gemini/skills/ai-sdlc-harness/SKILL.md").exists()
    assert any("adapter installation is not performed by init" in item for item in messages)


def test_old_config_without_gemini_remains_readable_and_unchanged(project_tmp):
    assert init_project(project_tmp)[0] == 0
    config_path = project_tmp / ".harness/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    del config["adapters"]["gemini_cli"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    write_manifest(project_tmp)
    before = _file_snapshot(project_tmp)

    status_code, _ = status_project(project_tmp)
    verify_code, verify_messages = verify_project(project_tmp)

    assert status_code == 0
    assert verify_code == 0
    assert verify_messages == ["AI SDLC Harness verification passed"]
    assert _file_snapshot(project_tmp) == before


def test_status_works_before_and_after_init(project_tmp):
    code, before = status_project(project_tmp)
    assert code == 0
    assert "initialized: no" in before

    assert init_project(project_tmp)[0] == 0
    code, after = status_project(project_tmp)

    assert code == 0
    assert "initialized: yes" in after
    assert not any(message.startswith("mode:") for message in after)
    assert "selected packs: none" in after


def test_status_is_read_only(project_tmp):
    assert init_project(project_tmp)[0] == 0
    before = {relative: (project_tmp / relative).read_bytes() for relative in EXPECTED_FILES}

    code, _ = status_project(project_tmp)
    after = {relative: (project_tmp / relative).read_bytes() for relative in EXPECTED_FILES}

    assert code == 0
    assert before == after


def test_status_json_is_deterministic_and_read_only(project_tmp):
    assert init_project(project_tmp)[0] == 0
    config_path = project_tmp / ".harness" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["adapters"]["access_token"] = "token-abcdefghijklmnop"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    before = _file_snapshot(project_tmp)

    first_code, first = status_project(project_tmp, json_output=True)
    second_code, second = status_project(project_tmp, json_output=True)

    assert first_code == second_code == 0
    assert first == second
    document = json.loads(first[0])
    assert document["workflow_status_schema_version"] == 1
    assert document["phase"] == "task_selection"
    assert document["outcome"] == "NEXT"
    assert "abcdefghijklmnop" not in first[0]
    assert _file_snapshot(project_tmp) == before


def test_status_redacts_secret_like_config_values(project_tmp):
    assert init_project(project_tmp)[0] == 0
    config_path = project_tmp / ".harness/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["adapters"]["access_token"] = "token-" + "abcdefghijklmnop"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    code, messages = status_project(project_tmp)
    output = "\n".join(messages)

    assert code == 0
    assert "access_token=[REDACTED]" in output
    assert "abcdefghijklmnop" not in output


def test_manifest_lists_protected_files_without_self_hash(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest = json.loads((project_tmp / ".harness/manifest.json").read_text(encoding="utf-8"))
    entries = {entry["path"]: entry for entry in manifest["managed_files"]}

    assert manifest["manifest_schema_version"] == 2
    assert manifest["generated_artifact_provenance"] == []
    assert entries[".harness/config.yaml"]["protected"] is True
    assert entries[".harness/config.yaml"]["hash_algorithm"] == "sha256"
    assert entries[".harness/config.yaml"]["sha256"]
    assert entries[".harness/manifest.json"]["protected"] is True
    assert entries[".harness/manifest.json"]["hash_algorithm"] is None
    assert entries[".harness/manifest.json"]["sha256"] is None


def test_ordinary_init_leaves_supported_v1_manifest_byte_for_byte_unchanged(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    legacy_bytes = _install_released_v1(project_tmp)

    code, messages = init_project(project_tmp)

    assert code == 0
    assert manifest_path.read_bytes() == legacy_bytes
    assert "skip existing manifest .harness/manifest.json" in messages


def test_read_only_verify_does_not_migrate_supported_v1(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    legacy_bytes = _install_released_v1(project_tmp)

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]
    assert manifest_path.read_bytes() == legacy_bytes


def test_init_force_upgrades_v1_without_rewriting_task_or_unmanaged_files(project_tmp):
    assert init_project(project_tmp)[0] == 0
    task_dir = project_tmp / ".harness" / "tasks" / "existing-task"
    task_dir.mkdir()
    task_file = task_dir / "task.md"
    task_file.write_text("existing task\n", encoding="utf-8")
    unmanaged = project_tmp / ".harness" / "user-note.md"
    unmanaged.write_text("keep me\n", encoding="utf-8")

    manifest_path = project_tmp / ".harness" / "manifest.json"
    _install_released_v1(project_tmp)
    task_before = task_file.read_bytes()
    unmanaged_before = unmanaged.read_bytes()

    code, _messages = init_project(project_tmp, force=True)

    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert code == 0
    assert upgraded["manifest_schema_version"] == 2
    assert upgraded["generated_artifact_provenance"] == []
    assert task_file.read_bytes() == task_before
    assert unmanaged.read_bytes() == unmanaged_before
    assert _read(manifest_path).endswith("\n")


def test_ordinary_init_repairs_only_missing_v2_managed_record(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    config_path = project_tmp / ".harness" / "config.yaml"
    instructions_path = project_tmp / ".harness" / "generated" / "agent-instructions.md"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_config_hash = next(
        record["sha256"] for record in before["managed_files"] if record["path"] == ".harness/config.yaml"
    )
    config_path.write_text("drifted: true\n", encoding="utf-8")
    instructions_path.unlink()

    code, _messages = init_project(project_tmp)

    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_path = {record["path"]: record for record in after["managed_files"]}
    assert code == 0
    assert after["manifest_schema_version"] == 2
    assert by_path[".harness/config.yaml"]["sha256"] == old_config_hash
    assert by_path[".harness/generated/agent-instructions.md"]["sha256"] == hashlib.sha256(
        instructions_path.read_bytes()
    ).hexdigest()
    verify_code, verify_messages = verify_project(project_tmp)
    assert verify_code == 1
    assert any("hash drift detected for .harness/config.yaml" in message for message in verify_messages)


def test_ordinary_init_repairs_only_missing_v1_managed_record(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    config_path = project_tmp / ".harness" / "config.yaml"
    instructions_path = project_tmp / ".harness" / "generated" / "agent-instructions.md"
    _install_released_v1(project_tmp)
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_config_hash = next(
        record["sha256"] for record in before["managed_files"] if record["path"] == ".harness/config.yaml"
    )
    config_path.write_text("drifted: true\n", encoding="utf-8")
    instructions_path.unlink()

    code, _messages = init_project(project_tmp)

    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_path = {record["path"]: record for record in after["managed_files"]}
    assert code == 0
    assert "manifest_schema_version" not in after
    assert "generated_artifact_provenance" not in after
    assert by_path[".harness/config.yaml"]["sha256"] == old_config_hash
    assert by_path[".harness/generated/agent-instructions.md"]["sha256"] == hashlib.sha256(
        instructions_path.read_bytes()
    ).hexdigest()
    verify_code, verify_messages = verify_project(project_tmp)
    assert verify_code == 1
    assert any("hash drift detected for .harness/config.yaml" in message for message in verify_messages)


def test_verify_fails_before_init(project_tmp):
    code, messages = verify_project(project_tmp)

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_verify_passes_after_init(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_rejects_unsupported_explicit_manifest_version(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_schema_version"] = 3
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("unsupported manifest schema version: 3" in message for message in messages)


def test_verify_rejects_duplicate_manifest_json_keys(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    text = manifest_path.read_text(encoding="utf-8")
    text = text.replace(
        '"manifest_schema_version": 2,',
        '"manifest_schema_version": 2, "manifest_schema_version": 2,',
        1,
    )
    manifest_path.write_text(text, encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("duplicate key: manifest_schema_version" in message for message in messages)


def test_verify_tolerates_legacy_default_pack_ids(project_tmp):
    assert init_project(project_tmp)[0] == 0
    legacy_ids = [
        "architecture-generic",
        "coupling-baseline",
        "observability-baseline",
        "security-baseline",
    ]
    config_path = project_tmp / ".harness" / "config.yaml"
    selected_path = project_tmp / ".harness" / "packs" / "selected.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["selected_packs"] = legacy_ids
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    selected_path.write_text(
        yaml.safe_dump(
            {
                "selected_packs": [
                    {"id": pack_id, "version": "legacy", "enabled": True} for pack_id in legacy_ids
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    write_manifest(project_tmp)

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_rejects_unknown_selected_pack_ids(project_tmp):
    assert init_project(project_tmp)[0] == 0
    config_path = project_tmp / ".harness" / "config.yaml"
    selected_path = project_tmp / ".harness" / "packs" / "selected.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["selected_packs"] = ["not-a-known-pack"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    selected_path.write_text(
        yaml.safe_dump({"selected_packs": [{"id": "also-not-known", "version": "legacy", "enabled": True}]}),
        encoding="utf-8",
    )
    write_manifest(project_tmp)

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("unknown selected pack IDs: also-not-known, not-a-known-pack" in message for message in messages)


def test_verify_fails_when_protected_file_missing(project_tmp):
    assert init_project(project_tmp)[0] == 0
    (project_tmp / ".harness/generated/agent-instructions.md").unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/generated/agent-instructions.md" in message for message in messages)


def test_verify_fails_when_protected_file_hash_drifts(project_tmp):
    assert init_project(project_tmp)[0] == 0
    (project_tmp / ".harness/config.yaml").write_text("broken: true\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/config.yaml" in message for message in messages)


@pytest.mark.parametrize("filename", ["task.md", "acceptance.md"])
def test_verify_allows_human_owned_task_source_content_edits(project_tmp, filename):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    source = project_tmp / ".harness" / "tasks" / LINEAGE_SLUG / filename
    source.write_text(f"# {filename}\n\nHuman-authored source intent.\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_still_detects_missing_human_owned_task_source(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    path_text = f".harness/tasks/{LINEAGE_SLUG}/task.md"
    (project_tmp / path_text).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert f"- missing file {path_text}" in messages


def test_verify_workflow_policy_defers_only_missing_human_owned_source(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    path_text = f".harness/tasks/{LINEAGE_SLUG}/task.md"
    (project_tmp / path_text).unlink()

    code, messages = verify_project(
        project_tmp,
        allow_human_source_readiness=True,
    )

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_rejects_human_owned_source_presented_as_final_symlink(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    path_text = f".harness/tasks/{LINEAGE_SLUG}/task.md"
    target = project_tmp / path_text
    before = _file_snapshot(project_tmp)
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)
    code, messages = verify_project(project_tmp)

    assert code == 1
    assert f"- unsafe managed path {path_text}" in messages
    assert str(project_tmp) not in "\n".join(messages)
    assert _file_snapshot(project_tmp) == before


def test_verify_rejects_human_owned_sources_under_symlinked_task_parent(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    path_text = f".harness/tasks/{LINEAGE_SLUG}/acceptance.md"
    task_root = project_tmp / ".harness" / "tasks" / LINEAGE_SLUG
    before = _file_snapshot(project_tmp)
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == task_root or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)
    code, messages = verify_project(project_tmp)

    assert code == 1
    assert f"- unsafe managed path {path_text}" in messages
    assert str(project_tmp) not in "\n".join(messages)
    assert _file_snapshot(project_tmp) == before


def test_verify_still_rejects_generated_preflight_hash_drift(project_tmp):
    _initialize_with_preflight_provenance(project_tmp)
    path_text = f".harness/tasks/{LINEAGE_SLUG}/preflight.md"
    target = project_tmp / path_text
    target.write_bytes(target.read_bytes() + b"\ngenerated drift")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert f"- hash drift detected for {path_text}" in messages


def test_verify_rejects_task_artifact_presented_as_final_symlink(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    assert run_validate(project_tmp, LINEAGE_SLUG)[0] == 0
    path_text = f".harness/tasks/{LINEAGE_SLUG}/validation-report.md"
    target = project_tmp / path_text
    before = _file_snapshot(project_tmp)
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert f"- unsafe managed path {path_text}" in messages
    assert str(project_tmp) not in "\n".join(messages)
    assert _file_snapshot(project_tmp) == before


def test_verify_rejects_task_artifact_under_symlinked_parent(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    assert run_validate(project_tmp, LINEAGE_SLUG)[0] == 0
    path_text = f".harness/tasks/{LINEAGE_SLUG}/validation-report.md"
    task_root = project_tmp / ".harness" / "tasks" / LINEAGE_SLUG
    before = _file_snapshot(project_tmp)
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == task_root or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert f"- unsafe managed path {path_text}" in messages
    assert str(project_tmp) not in "\n".join(messages)
    assert _file_snapshot(project_tmp) == before


def test_verify_rejects_main_manifest_presented_as_symlink(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    path_text = ".harness/manifest.json"
    target = project_tmp / path_text
    before = _file_snapshot(project_tmp)
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert messages == [
        "AI SDLC Harness verification failed",
        f"- unsafe managed path {path_text}",
    ]
    assert str(project_tmp) not in "\n".join(messages)
    assert _file_snapshot(project_tmp) == before


def test_verify_rejects_hash_equivalent_managed_file_presented_as_symlink(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    path_text = ".harness/config.yaml"
    target = project_tmp / path_text
    manifest = json.loads(_read(project_tmp / ".harness" / "manifest.json"))
    entry = next(item for item in manifest["managed_files"] if item["path"] == path_text)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == entry["sha256"]
    before = _file_snapshot(project_tmp)
    original_is_symlink = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == target or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert messages == [
        "AI SDLC Harness verification failed",
        f"- unsafe managed path {path_text}",
    ]
    assert not any("hash drift detected" in message for message in messages)
    assert str(project_tmp) not in "\n".join(messages)
    assert _file_snapshot(project_tmp) == before


def test_verify_accepts_current_and_old_lineage_producer_versions(project_tmp):
    manifest = _initialize_with_preflight_provenance(project_tmp)

    current_code, current_messages = verify_project(project_tmp)

    old_record = replace(
        manifest.generated_artifact_provenance[0],
        producer_version=2,
    )
    persist_manifest_model(
        project_tmp,
        replace(
            manifest,
            generated_artifact_provenance=(old_record,),
        ),
    )
    old_code, old_messages = verify_project(project_tmp)

    assert current_code == 0
    assert current_messages == ["AI SDLC Harness verification passed"]
    assert old_code == 0
    assert old_messages == ["AI SDLC Harness verification passed"]


def test_verify_does_not_calculate_dependency_or_observation_freshness(
    project_tmp,
):
    _initialize_with_preflight_provenance(project_tmp)
    task_path = (
        project_tmp
        / ".harness"
        / "tasks"
        / LINEAGE_SLUG
        / "task.md"
    )
    task_path.write_bytes(task_path.read_bytes() + b"\ndependency changed")
    update_managed_file_records(
        project_tmp,
        [
            build_managed_file_record(
                project_tmp,
                f".harness/tasks/{LINEAGE_SLUG}/task.md",
            )
        ],
        generated_at="dependency-change-test",
    )
    (project_tmp / "package.json").write_bytes(b"{}")

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


@pytest.mark.parametrize(
    "case",
    [
        "unknown_producer",
        "wrong_producer",
        "wrong_output",
        "missing_dependency",
        "extra_dependency",
        "missing_observation",
        "extra_observation",
    ],
)
def test_verify_rejects_semantically_invalid_lineage_provenance(
    project_tmp,
    case,
):
    manifest = _initialize_with_preflight_provenance(project_tmp)
    record = manifest.generated_artifact_provenance[0]
    if case == "unknown_producer":
        changed = replace(record, producer_command="unknown-producer")
    elif case == "wrong_producer":
        changed = replace(record, producer_command="spec")
    elif case == "wrong_output":
        task_path = f".harness/tasks/{LINEAGE_SLUG}/task.md"
        managed = next(
            item for item in manifest.managed_files if item.path == task_path
        )
        changed = replace(
            record,
            output_path=task_path,
            output_sha256=managed.sha256,
        )
    elif case == "missing_dependency":
        changed = replace(record, dependencies=record.dependencies[1:])
    elif case == "extra_dependency":
        changed = replace(
            record,
            dependencies=tuple(
                sorted(
                    (
                        *record.dependencies,
                        ProvenanceDependency("extra.md", "missing", None),
                    ),
                    key=lambda item: item.dependency_path,
                )
            ),
        )
    elif case == "missing_observation":
        changed = replace(
            record,
            repository_observations=record.repository_observations[1:],
        )
    else:
        changed = replace(
            record,
            repository_observations=tuple(
                sorted(
                    (
                        *record.repository_observations,
                        ProvenanceObservation(
                            "extra-observation",
                            "exists",
                            False,
                        ),
                    ),
                    key=lambda item: (
                        item.observation_path,
                        item.predicate,
                    ),
                )
            ),
        )
    persist_manifest_model(
        project_tmp,
        replace(
            manifest,
            generated_artifact_provenance=(changed,),
        ),
    )

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any(
        message.startswith(
            "- invalid generated-artifact provenance in .harness/manifest.json:"
        )
        for message in messages
    )
    assert not any(str(project_tmp) in message for message in messages)


def test_verify_does_not_call_resolver_or_inspection(
    project_tmp,
    monkeypatch,
):
    _initialize_with_preflight_provenance(project_tmp)

    def forbidden(*_args, **_kwargs):
        pytest.fail("verify called freshness resolution or inspection")

    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.resolve_derived_freshness",
        forbidden,
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.lineage.inspect_provenance_state",
        forbidden,
    )

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_accepts_validation_provenance_but_does_not_check_currentness(
    project_tmp,
):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    assert run_validate(project_tmp, LINEAGE_SLUG)[0] == 0
    task_path_text = f".harness/tasks/{LINEAGE_SLUG}/task.md"
    task_path = project_tmp / task_path_text
    task_path.write_bytes(task_path.read_bytes() + b"\nchanged dependency")
    update_managed_file_records(
        project_tmp,
        [build_managed_file_record(project_tmp, task_path_text)],
        generated_at="validation-dependency-change-test",
    )

    verify_code, verify_messages = verify_project(project_tmp)
    currentness = resolve_validation_currentness(project_tmp, LINEAGE_SLUG)

    assert verify_code == 0
    assert verify_messages == ["AI SDLC Harness verification passed"]
    assert currentness.state is ValidationCurrentnessState.STALE


def test_verify_does_not_call_validation_currentness_or_semantic_inspection(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Lineage Task")[0] == 0
    assert run_validate(project_tmp, LINEAGE_SLUG)[0] == 0

    def forbidden(*_args, **_kwargs):
        pytest.fail("verify called validation currentness or semantic inspection")

    monkeypatch.setattr(
        "ai_sdlc_harness.validation.resolve_validation_currentness",
        forbidden,
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.validation.capture_validation_snapshot",
        forbidden,
    )
    monkeypatch.setattr(
        "ai_sdlc_harness.validation.inspect_validation_snapshot",
        forbidden,
    )

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_config_state_and_selected_packs_are_parseable(project_tmp):
    assert init_project(project_tmp)[0] == 0

    assert isinstance(yaml.safe_load(_read(project_tmp / ".harness/config.yaml")), dict)
    assert isinstance(json.loads(_read(project_tmp / ".harness/state.json")), dict)
    assert isinstance(yaml.safe_load(_read(project_tmp / ".harness/packs/selected.yaml")), dict)
