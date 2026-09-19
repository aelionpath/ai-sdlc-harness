from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

import ai_sdlc_harness.adapters as adapters_module
from ai_sdlc_harness.adapters import (
    ADAPTER_DEFINITIONS,
    ADAPTER_IDS,
    ADAPTER_SKILL_MAX_BYTES,
    install_adapters,
    render_adapter_skill,
)
from ai_sdlc_harness.cli import main
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.manifest import (
    load_manifest_model,
    write_manifest,
)
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.verify import verify_project


EXPECTED_PATHS = {
    "codex": ".agents/skills/ai-sdlc-harness/SKILL.md",
    "claude-code": ".claude/skills/ai-sdlc-harness/SKILL.md",
    "gemini-cli": ".gemini/skills/ai-sdlc-harness/SKILL.md",
}
ROOT_INSTRUCTION_FILES = {
    "codex": "AGENTS.md",
    "claude-code": "CLAUDE.md",
    "gemini-cli": "GEMINI.md",
}


def _path(root: Path, adapter_id: str) -> Path:
    return root / EXPECTED_PATHS[adapter_id]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in root.rglob("*")
        if item.is_file()
    }


def _records(root: Path):
    manifest = load_manifest_model(root / ".harness/manifest.json")
    return manifest, {record.path: record for record in manifest.managed_files}


def test_adapter_registry_has_exact_ids_paths_and_root_boundaries():
    assert ADAPTER_IDS == ("codex", "claude-code", "gemini-cli")
    assert {
        item.adapter_id: item.skill_path.as_posix()
        for item in ADAPTER_DEFINITIONS
    } == EXPECTED_PATHS
    assert {
        item.adapter_id: item.root_instruction_file
        for item in ADAPTER_DEFINITIONS
    } == ROOT_INSTRUCTION_FILES


def test_rendered_skills_are_deterministic_identical_valid_and_bounded():
    rendered = [render_adapter_skill(adapter_id) for adapter_id in ADAPTER_IDS]

    assert rendered[0] == rendered[1] == rendered[2]
    assert render_adapter_skill("codex") == rendered[0]
    assert len(rendered[0]) <= ADAPTER_SKILL_MAX_BYTES
    assert rendered[0].decode("utf-8").encode("utf-8") == rendered[0]
    frontmatter = rendered[0].decode("utf-8").split("---\n", 2)[1]
    metadata = yaml.safe_load(frontmatter)
    assert metadata == {
        "name": "ai-sdlc-harness",
        "description": metadata["description"],
    }
    assert "initialized with AI SDLC Harness (.harness)" in metadata["description"]


def test_rendered_skill_contains_required_workflow_contract_without_root_rewrites():
    text = render_adapter_skill("codex").decode("utf-8")

    required = (
        "ai-sdlc status --json",
        "ai-sdlc status --task <slug> --json",
        "selected_task",
        "phase",
        "outcome",
        "next_actions",
        "four-key `lineage`",
        "BLOCKED",
        "REVIEW_REQUIRED",
        "generated/agent-workset.md",
        "do not recursively load the whole `.harness` tree",
        "Never directly edit",
        "requirements.yaml",
        "generated/context-manifest.yaml",
        "baseline secure-engineering guardrails in the workset",
        ".harness/config.yaml",
        ".harness/state.json",
        ".harness/manifest.json",
        ".harness/generated/agent-instructions.md",
        ".harness/packs/selected.yaml",
        "factual implementation evidence in `evidence.md`",
        "observed results in `verification.md`",
        "COMPLETE",
        "human review",
        "not proof of correctness, security, or compliance",
    )
    assert all(item in text for item in required)
    assert "AGENTS.md" not in text
    assert "CLAUDE.md" not in text
    assert "GEMINI.md" not in text


@pytest.mark.parametrize("adapter_id", ADAPTER_IDS)
def test_install_one_creates_only_its_skill_and_exact_manifest_record(
    project_tmp,
    adapter_id,
):
    assert init_project(project_tmp)[0] == 0
    roots_before = {
        name: (project_tmp / name).read_bytes()
        for name in ROOT_INSTRUCTION_FILES.values()
        if (project_tmp / name).exists()
    }

    code, messages = install_adapters(project_tmp, adapter_id)

    assert code == 0
    assert _path(project_tmp, adapter_id).read_bytes() == render_adapter_skill(adapter_id)
    for other_id in ADAPTER_IDS:
        assert _path(project_tmp, other_id).exists() is (other_id == adapter_id)
    manifest, records = _records(project_tmp)
    record = records[EXPECTED_PATHS[adapter_id]]
    assert record.protected is True
    assert record.hash_algorithm == "sha256"
    assert record.sha256 == hashlib.sha256(_path(project_tmp, adapter_id).read_bytes()).hexdigest()
    assert all(
        EXPECTED_PATHS[other_id] not in records
        for other_id in ADAPTER_IDS
        if other_id != adapter_id
    )
    assert manifest.generated_artifact_provenance == ()
    assert f"agent ID: {adapter_id}" in messages
    assert any(EXPECTED_PATHS[adapter_id] in item for item in messages)
    assert roots_before == {
        name: (project_tmp / name).read_bytes()
        for name in roots_before
    }
    assert verify_project(project_tmp) == (0, ["AI SDLC Harness verification passed"])


def test_install_all_creates_all_skills_after_common_preflight(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = install_adapters(project_tmp, "all")

    assert code == 0
    assert all(_path(project_tmp, adapter_id).is_file() for adapter_id in ADAPTER_IDS)
    _manifest, records = _records(project_tmp)
    assert all(path in records for path in EXPECTED_PATHS.values())
    assert all(f"agent ID: {adapter_id}" in messages for adapter_id in ADAPTER_IDS)


def test_repeated_exact_install_is_byte_for_byte_noop(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert install_adapters(project_tmp, "codex")[0] == 0
    skill = _path(project_tmp, "codex")
    manifest = project_tmp / ".harness/manifest.json"
    skill_before = skill.read_bytes()
    manifest_before = manifest.read_bytes()

    code, messages = install_adapters(project_tmp, "codex")

    assert code == 0
    assert skill.read_bytes() == skill_before
    assert manifest.read_bytes() == manifest_before
    assert f"skipped Codex adapter: {EXPECTED_PATHS['codex']}" in messages


@pytest.mark.parametrize("adapter_id", (*ADAPTER_IDS, "all"))
def test_dry_run_writes_nothing(project_tmp, adapter_id):
    assert init_project(project_tmp)[0] == 0
    before = _snapshot(project_tmp)

    code, messages = install_adapters(project_tmp, adapter_id, dry_run=True)

    assert code == 0
    assert _snapshot(project_tmp) == before
    assert "dry run; no files written" in messages
    assert any(item.startswith("planned ") for item in messages)


@pytest.mark.parametrize("force", [False, True])
def test_unmanaged_existing_adapter_blocks_even_with_force(project_tmp, force):
    assert init_project(project_tmp)[0] == 0
    target = _path(project_tmp, "codex")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user-owned\n")
    before = _snapshot(project_tmp)

    code, messages = install_adapters(project_tmp, "codex", force=force)

    assert code == 1
    assert any("unmanaged existing adapter file" in item for item in messages)
    assert _snapshot(project_tmp) == before


def test_all_conflict_blocks_before_any_adapter_write(project_tmp):
    assert init_project(project_tmp)[0] == 0
    conflict = _path(project_tmp, "claude-code")
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"user-owned\n")
    before = _snapshot(project_tmp)

    code, messages = install_adapters(project_tmp, "all")

    assert code == 1
    assert any("unmanaged existing adapter file" in item for item in messages)
    assert _snapshot(project_tmp) == before
    assert not _path(project_tmp, "codex").exists()
    assert not _path(project_tmp, "gemini-cli").exists()


def test_managed_drift_blocks_without_force_and_force_restores_only_selected(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert install_adapters(project_tmp, "all")[0] == 0
    codex = _path(project_tmp, "codex")
    claude = _path(project_tmp, "claude-code")
    gemini = _path(project_tmp, "gemini-cli")
    codex.write_bytes(b"drift\n")
    claude_before = claude.read_bytes()
    gemini_before = gemini.read_bytes()
    manifest_before = (project_tmp / ".harness/manifest.json").read_bytes()

    blocked_code, blocked_messages = install_adapters(project_tmp, "codex")
    restored_code, _ = install_adapters(project_tmp, "codex", force=True)

    assert blocked_code == 1
    assert any("hash drift detected" in item for item in blocked_messages)
    assert restored_code == 0
    assert codex.read_bytes() == render_adapter_skill("codex")
    assert claude.read_bytes() == claude_before
    assert gemini.read_bytes() == gemini_before
    assert (project_tmp / ".harness/manifest.json").read_bytes() == manifest_before
    assert verify_project(project_tmp)[0] == 0


@pytest.mark.parametrize("adapter_id", ADAPTER_IDS)
def test_missing_managed_adapter_requires_force_to_restore(project_tmp, adapter_id):
    assert init_project(project_tmp)[0] == 0
    assert install_adapters(project_tmp, adapter_id)[0] == 0
    _path(project_tmp, adapter_id).unlink()

    blocked_code, blocked_messages = install_adapters(project_tmp, adapter_id)
    restored_code, _ = install_adapters(project_tmp, adapter_id, force=True)

    assert blocked_code == 1
    assert any("managed adapter file is missing" in item for item in blocked_messages)
    assert restored_code == 0
    assert _path(project_tmp, adapter_id).read_bytes() == render_adapter_skill(adapter_id)


@pytest.mark.parametrize("kind", ["final", "parent"])
def test_symlinked_adapter_path_is_blocked_without_absolute_path_leak(
    project_tmp,
    monkeypatch,
    kind,
):
    assert init_project(project_tmp)[0] == 0
    target = _path(project_tmp, "codex")
    marked = target if kind == "final" else project_tmp / ".agents"
    original = Path.is_symlink

    def mocked_is_symlink(path: Path) -> bool:
        return path == marked or original(path)

    monkeypatch.setattr(Path, "is_symlink", mocked_is_symlink)
    before = _snapshot(project_tmp)

    code, messages = install_adapters(project_tmp, "codex")

    assert code == 1
    assert any("could not safely inspect adapter path" in item for item in messages)
    assert str(project_tmp) not in "\n".join(messages)
    assert _snapshot(project_tmp) == before


def test_final_manifest_failure_never_claims_adapter_ownership(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness/manifest.json"
    manifest_before = manifest_path.read_bytes()

    def fail_manifest(_root, _document):
        raise OSError("simulated final manifest failure")

    monkeypatch.setattr(adapters_module, "persist_manifest_model", fail_manifest)
    code, messages = install_adapters(project_tmp, "codex")

    assert code == 1
    assert _path(project_tmp, "codex").read_bytes() == render_adapter_skill("codex")
    assert manifest_path.read_bytes() == manifest_before
    _manifest, records = _records(project_tmp)
    assert EXPECTED_PATHS["codex"] not in records
    assert any("manifest could not be refreshed" in item for item in messages)
    assert "does not claim the current adapter state" in messages[-1]
    assert str(project_tmp) not in "\n".join(messages)


@pytest.mark.parametrize("adapter_id", ADAPTER_IDS)
def test_verify_detects_adapter_drift_and_missing_files(project_tmp, adapter_id):
    assert init_project(project_tmp)[0] == 0
    assert install_adapters(project_tmp, adapter_id)[0] == 0
    target = _path(project_tmp, adapter_id)
    target.write_bytes(target.read_bytes() + b"drift")

    drift_code, drift_messages = verify_project(project_tmp)
    target.unlink()
    missing_code, missing_messages = verify_project(
        project_tmp,
        allow_human_source_readiness=True,
    )

    assert drift_code == 1
    assert any(
        f"hash drift detected for {EXPECTED_PATHS[adapter_id]}" in item
        for item in drift_messages
    )
    assert missing_code == 1
    assert any(
        f"missing file {EXPECTED_PATHS[adapter_id]}" in item
        for item in missing_messages
    )


def test_unknown_agent_skill_files_are_not_claimed_or_rejected(project_tmp):
    assert init_project(project_tmp)[0] == 0
    unknown = project_tmp / ".agents/skills/user-skill/SKILL.md"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"user skill\n")

    code, messages = verify_project(project_tmp)
    _manifest, records = _records(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]
    assert ".agents/skills/user-skill/SKILL.md" not in records


def test_verify_requires_protected_sha256_adapter_record(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert install_adapters(project_tmp, "codex")[0] == 0
    manifest_path = project_tmp / ".harness/manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = next(
        item
        for item in data["managed_files"]
        if item["path"] == EXPECTED_PATHS["codex"]
    )
    record.update(protected=False, hash_algorithm=None, sha256=None)
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any(
        f"invalid managed adapter record {EXPECTED_PATHS['codex']}" in item
        for item in messages
    )


def test_status_and_verify_do_not_install_or_repair_adapters(project_tmp):
    assert init_project(project_tmp)[0] == 0
    before = _snapshot(project_tmp)

    assert status_project(project_tmp)[0] == 0
    assert verify_project(project_tmp)[0] == 0
    assert _snapshot(project_tmp) == before

    assert install_adapters(project_tmp, "codex")[0] == 0
    target = _path(project_tmp, "codex")
    target.write_bytes(b"drift\n")
    drift_before = target.read_bytes()
    assert status_project(project_tmp)[0] == 0
    assert verify_project(project_tmp)[0] == 1
    assert target.read_bytes() == drift_before


def test_adapter_record_survives_unrelated_full_manifest_refresh(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert install_adapters(project_tmp, "gemini-cli")[0] == 0

    write_manifest(project_tmp)

    _manifest, records = _records(project_tmp)
    assert EXPECTED_PATHS["gemini-cli"] in records
    assert verify_project(project_tmp)[0] == 0


@pytest.mark.parametrize("adapter_id", (*ADAPTER_IDS, "all"))
def test_cli_installs_supported_adapter_ids(project_tmp, monkeypatch, capsys, adapter_id):
    assert init_project(project_tmp)[0] == 0
    monkeypatch.chdir(project_tmp)

    code = main(["adapter", "install", adapter_id])
    output = capsys.readouterr().out

    assert code == 0
    expected_ids = ADAPTER_IDS if adapter_id == "all" else (adapter_id,)
    assert all(f"agent ID: {item}" in output for item in expected_ids)
    assert all(_path(project_tmp, item).is_file() for item in expected_ids)
