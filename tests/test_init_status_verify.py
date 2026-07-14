from __future__ import annotations

import json
from pathlib import Path

import yaml

from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.manifest import write_manifest
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.verify import verify_project


EXPECTED_FILES = [
    ".harness/config.yaml",
    ".harness/state.json",
    ".harness/manifest.json",
    ".harness/generated/agent-instructions.md",
    ".harness/packs/selected.yaml",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_init_creates_expected_files(project_tmp):
    code, messages = init_project(project_tmp)

    assert code == 0
    assert (project_tmp / ".harness/tasks").is_dir()
    for relative in EXPECTED_FILES:
        assert (project_tmp / relative).is_file()
    assert "root AGENTS.md and CLAUDE.md were not modified" in messages

    config = yaml.safe_load(_read(project_tmp / ".harness/config.yaml"))
    assert config["harness_version"] == "1.0.0"
    assert config["template_version"] == "1.0.0"
    assert "mode" not in config
    assert config["adoption_scope"] == "task-local"
    assert config["selected_packs"] == []


def test_init_records_deferred_adapter_request(project_tmp):
    code, messages = init_project(project_tmp, agent="both")

    assert code == 0
    config = yaml.safe_load(_read(project_tmp / ".harness/config.yaml"))
    assert config["adapters"] == {
        "codex": "requested-deferred",
        "claude_code": "requested-deferred",
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
    agents.write_text("existing agents\n", encoding="utf-8")
    claude.write_text("existing claude\n", encoding="utf-8")

    code, _ = init_project(project_tmp, force=True, agent="both")

    assert code == 0
    assert agents.read_text(encoding="utf-8") == "existing agents\n"
    assert claude.read_text(encoding="utf-8") == "existing claude\n"


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

    assert entries[".harness/config.yaml"]["protected"] is True
    assert entries[".harness/config.yaml"]["hash_algorithm"] == "sha256"
    assert entries[".harness/config.yaml"]["sha256"]
    assert entries[".harness/manifest.json"]["protected"] is True
    assert entries[".harness/manifest.json"]["hash_algorithm"] is None
    assert entries[".harness/manifest.json"]["sha256"] is None


def test_verify_fails_before_init(project_tmp):
    code, messages = verify_project(project_tmp)

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_verify_passes_after_init(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


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


def test_config_state_and_selected_packs_are_parseable(project_tmp):
    assert init_project(project_tmp)[0] == 0

    assert isinstance(yaml.safe_load(_read(project_tmp / ".harness/config.yaml")), dict)
    assert isinstance(json.loads(_read(project_tmp / ".harness/state.json")), dict)
    assert isinstance(yaml.safe_load(_read(project_tmp / ".harness/packs/selected.yaml")), dict)
