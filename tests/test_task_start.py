from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ai_sdlc_harness.constants import TASK_ARTIFACT_FILENAMES
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness import task as task_module
from ai_sdlc_harness.task import start_task, task_slug_from_title
from ai_sdlc_harness.verify import verify_project


ROOT = Path(__file__).resolve().parents[1]
TASK_FILES = list(TASK_ARTIFACT_FILENAMES)

BASE_MANAGED_FILES = [
    ".harness/config.yaml",
    ".harness/state.json",
    ".harness/manifest.json",
    ".harness/generated/agent-instructions.md",
    ".harness/packs/selected.yaml",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _task_path(root: Path, slug: str, filename: str) -> Path:
    return root / ".harness" / "tasks" / slug / filename


def _manifest(root: Path) -> dict:
    return json.loads((root / ".harness" / "manifest.json").read_text(encoding="utf-8"))


def _render_expected_template(filename: str, *, title: str, slug: str, created_at: str) -> str:
    return (
        (ROOT / "templates" / "task" / filename)
        .read_text(encoding="utf-8")
        .replace("{{ task_title }}", title)
        .replace("{{ task_slug }}", slug)
        .replace("{{ created_at }}", created_at)
    )


def _write_test_templates(template_dir: Path, *, task_template: str = "# Task: {{ task_title }}\n") -> None:
    template_dir.mkdir(parents=True)
    for filename in TASK_FILES:
        content = task_template if filename == "task.md" else f"# {filename}\n"
        (template_dir / filename).write_text(content, encoding="utf-8")


def test_task_start_fails_before_init(project_tmp):
    code, messages = start_task(project_tmp, "Add task")

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_task_start_rejects_empty_title(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = start_task(project_tmp, "   ")

    assert code == 2
    assert "must not be empty" in messages[0]


def test_task_slug_accepts_normal_titles_with_slashes():
    assert task_slug_from_title("Fix API / UI validation mismatch") == "fix-api-ui-validation-mismatch"
    assert task_slug_from_title("Update auth/login behavior") == "update-auth-login-behavior"
    assert task_slug_from_title("Update auth\\login behavior") == "update-auth-login-behavior"


def test_task_slug_rejects_path_like_or_empty_values():
    unsafe_titles = [
        "../x",
        "..\\x",
        "/tmp/x",
        "C:/Temp/x",
        "C:\\Temp\\x",
        "\x00",
        "!!!",
    ]

    for title in unsafe_titles:
        try:
            task_slug_from_title(title)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected unsafe title to be rejected: {title!r}")


def test_task_start_creates_expected_folder_files_and_manifest_entries(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = start_task(project_tmp, "Fix API / UI validation mismatch")

    slug = "fix-api-ui-validation-mismatch"
    assert code == 0
    assert f"task slug: {slug}" in messages
    assert "root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified" in messages
    for filename in TASK_FILES:
        path = _task_path(project_tmp, slug, filename)
        assert path.is_file()
    task_text = _read(_task_path(project_tmp, slug, "task.md"))
    assert "Fix API / UI validation mismatch" in task_text
    assert f"Task slug: `{slug}`" in task_text
    assert "## Implementation Boundary" in task_text
    assert "expected change area" in task_text
    assert "protected areas" in task_text
    assert "external or user-visible interface impact" in task_text
    assert "security, privacy, trust-boundary" in task_text
    assert "authority/privilege impact" in task_text
    assert "cross-cutting impact" in task_text
    assert "## Assumptions And Open Questions" in task_text

    acceptance_text = _read(_task_path(project_tmp, slug, "acceptance.md"))
    assert "## Requirements And Acceptance Criteria" in acceptance_text
    assert "specific requirements inside this task" in acceptance_text
    assert "observable acceptance criteria" in acceptance_text
    assert "## Protected Behavior And Non-Goals" in acceptance_text
    assert "authorization, sensitive-data, or safe-failure invariants" in acceptance_text

    architecture_text = _read(_task_path(project_tmp, slug, "architecture-notes.md"))
    assert "## Responsibility Change" in architecture_text
    assert "## Existing Patterns To Preserve" in architecture_text
    assert "## Interface And Compatibility Impact" in architecture_text
    assert "## Security And Privacy Risk Surface" in architecture_text
    assert "changes a trust boundary" in architecture_text
    assert "authorization/least-privilege invariants" in architecture_text
    assert "security or privacy-sensitive surface" in architecture_text

    coupling_text = _read(_task_path(project_tmp, slug, "coupling-notes.md"))
    assert "## New Or Changed Coupling" in coupling_text
    assert "## Maintainability Sensors" in coupling_text
    assert "hidden coupling risk" in coupling_text
    assert "dependency, config, schema" in coupling_text
    assert "trust, maintenance, privilege, or capability risk" in coupling_text

    test_contract_text = _read(_task_path(project_tmp, slug, "test-contract.md"))
    assert "The harness does not generate or run tests." in test_contract_text
    assert "## Characterization Tests" in test_contract_text
    assert "## Desired Behavior Tests" in test_contract_text
    assert "## Regression Tests" in test_contract_text
    assert "## Negative And Edge Cases" in test_contract_text
    assert "malformed or untrusted input" in test_contract_text
    assert "denied or unauthorized paths" in test_contract_text
    assert "Security-specific tests are not required when no material risk surface" in test_contract_text
    assert "automated test is not practical" in test_contract_text
    assert "bounded manual verification approach" in test_contract_text

    verification_text = _read(_task_path(project_tmp, slug, "verification.md"))
    assert "## Commands And Checks Run" in verification_text
    assert "## Results" in verification_text
    assert "## Not Run / Why" in verification_text
    assert "actually run" in verification_text
    assert "planned check from test-contract.md" in verification_text

    evidence_text = _read(_task_path(project_tmp, slug, "evidence.md"))
    assert "supports review but does not prove correctness, security, or compliance" in evidence_text
    assert "requirements addressed" in evidence_text
    assert "material dependency, security-relevant configuration" in evidence_text
    assert "risk acceptances" in evidence_text
    assert "manual follow-up" in evidence_text

    entries = {entry["path"]: entry for entry in _manifest(project_tmp)["managed_files"]}
    for filename in TASK_FILES:
        path_text = f".harness/tasks/{slug}/{filename}"
        assert entries[path_text]["protected"] is True
        assert entries[path_text]["hash_algorithm"] == "sha256"
        assert entries[path_text]["sha256"]


def test_task_start_generates_artifacts_from_task_templates(project_tmp, monkeypatch):
    assert init_project(project_tmp)[0] == 0
    created_at = "2026-07-09T00:00:00+00:00"
    title = "Fix API / UI validation mismatch"
    slug = "fix-api-ui-validation-mismatch"
    monkeypatch.setattr(task_module, "_timestamp", lambda: created_at)

    code, messages = start_task(project_tmp, title)

    assert code == 0
    assert f"task slug: {slug}" in messages
    for filename in TASK_FILES:
        assert _read(_task_path(project_tmp, slug, filename)) == _render_expected_template(
            filename,
            title=title,
            slug=slug,
            created_at=created_at,
        )


def test_task_start_renders_only_expected_placeholders(project_tmp, tmp_path, monkeypatch):
    template_dir = tmp_path / "templates" / "task"
    _write_test_templates(
        template_dir,
        task_template=(
            "title={{task_title}}\n"
            "slug={{ task_slug }}\n"
            "created={{ created_at }}\n"
        ),
    )
    monkeypatch.setattr(task_module, "TASK_TEMPLATE_DIR", template_dir)
    monkeypatch.setattr(task_module, "_timestamp", lambda: "2026-07-09T00:00:00+00:00")
    assert init_project(project_tmp)[0] == 0

    code, _messages = start_task(project_tmp, "Add input validation to user settings")

    assert code == 0
    assert _read(_task_path(project_tmp, "add-input-validation-to-user-settings", "task.md")) == (
        "title=Add input validation to user settings\n"
        "slug=add-input-validation-to-user-settings\n"
        "created=2026-07-09T00:00:00+00:00\n"
    )


def test_task_start_uses_runtime_template_files(project_tmp, tmp_path, monkeypatch):
    template_dir = tmp_path / "templates" / "task"
    _write_test_templates(template_dir, task_template="custom template for {{ task_title }}\n")
    monkeypatch.setattr(task_module, "TASK_TEMPLATE_DIR", template_dir)
    assert init_project(project_tmp)[0] == 0

    code, _messages = start_task(project_tmp, "Runtime template")

    assert code == 0
    assert _read(_task_path(project_tmp, "runtime-template", "task.md")) == "custom template for Runtime template\n"


def test_task_start_fails_clearly_when_template_is_missing(project_tmp, tmp_path, monkeypatch):
    template_dir = tmp_path / "templates" / "task"
    _write_test_templates(template_dir)
    (template_dir / "acceptance.md").unlink()
    monkeypatch.setattr(task_module, "TASK_TEMPLATE_DIR", template_dir)
    assert init_project(project_tmp)[0] == 0

    code, messages = start_task(project_tmp, "Missing template")

    assert code == 1
    assert any("Missing task template file" in message for message in messages)
    assert not (project_tmp / ".harness" / "tasks" / "missing-template").exists()


def test_task_start_fails_clearly_for_unknown_template_placeholder(project_tmp, tmp_path, monkeypatch):
    template_dir = tmp_path / "templates" / "task"
    _write_test_templates(template_dir, task_template="{{ task_title }} {{ unknown_value }}\n")
    monkeypatch.setattr(task_module, "TASK_TEMPLATE_DIR", template_dir)
    assert init_project(project_tmp)[0] == 0

    code, messages = start_task(project_tmp, "Unknown placeholder")

    assert code == 1
    assert any(
        "Unknown task template placeholder in task.md: {{ unknown_value }}" in message
        for message in messages
    )
    assert not (project_tmp / ".harness" / "tasks" / "unknown-placeholder").exists()


def test_task_templates_match_artifact_filenames_and_packaging_config():
    template_dir = ROOT / "templates" / "task"
    template_files = {path.name for path in template_dir.glob("*.md")}
    assert template_files == set(TASK_FILES)

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packaged = {
        Path(path).name
        for path in data["tool"]["setuptools"]["data-files"]["share/ai-sdlc-harness/templates/task"]
    }
    assert packaged == set(TASK_FILES)


def test_task_start_is_idempotent_and_does_not_refresh_manifest(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Add audit trail")[0] == 0
    slug = "add-audit-trail"
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    before = {path: path.read_bytes() for path in tracked}

    code, messages = start_task(project_tmp, "Add audit trail")
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert before == after
    assert "skip existing manifest .harness/manifest.json" in messages


def test_task_start_dry_run_writes_nothing(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = start_task(project_tmp, "Dry run task", dry_run=True)

    assert code == 0
    assert any(message.startswith("would create") for message in messages)
    assert not (project_tmp / ".harness" / "tasks" / "dry-run-task").exists()


def test_task_start_does_not_overwrite_without_force(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Update docs")[0] == 0
    task_md = _task_path(project_tmp, "update-docs", "task.md")
    task_md.write_text("custom content\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = start_task(project_tmp, "Update docs")

    assert code == 0
    assert task_md.read_text(encoding="utf-8") == "custom content\n"
    assert "skip existing file .harness/tasks/update-docs/task.md" in messages
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_task_start_force_rewrites_only_manifest_managed_task_files(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Update docs")[0] == 0
    task_dir = project_tmp / ".harness" / "tasks" / "update-docs"
    task_md = task_dir / "task.md"
    user_note = task_dir / "user-note.md"
    task_md.write_text("custom content\n", encoding="utf-8")
    user_note.write_text("keep me\n", encoding="utf-8")

    code, messages = start_task(project_tmp, "Update docs", force=True)

    assert code == 0
    assert "rewrite file .harness/tasks/update-docs/task.md" in messages
    assert "custom content" not in task_md.read_text(encoding="utf-8")
    assert user_note.read_text(encoding="utf-8") == "keep me\n"


def test_task_start_force_does_not_overwrite_unmanaged_existing_expected_file(project_tmp):
    assert init_project(project_tmp)[0] == 0
    task_dir = project_tmp / ".harness" / "tasks" / "blocked-task"
    task_dir.mkdir(parents=True)
    task_md = task_dir / "task.md"
    task_md.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = start_task(project_tmp, "Blocked task", force=True)

    assert code == 1
    assert task_md.read_text(encoding="utf-8") == "user owned\n"
    assert "unmanaged existing file .harness/tasks/blocked-task/task.md" in messages
    assert not (task_dir / "acceptance.md").exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_selected_task_status_stays_workflow_focused_and_read_only(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Count me")[0] == 0
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert "task: count-me" in messages
    assert "phase: task_definition" in messages
    assert "task folders: 1" not in messages
    assert before == after


def test_verify_passes_after_task_creation(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Verify me")[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_fails_when_manifest_managed_task_file_is_missing(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Verify missing")[0] == 0
    _task_path(project_tmp, "verify-missing", "evidence.md").unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/verify-missing/evidence.md" in message for message in messages)


def test_verify_allows_manifest_managed_human_source_hash_drift(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Verify drift")[0] == 0
    _task_path(project_tmp, "verify-drift", "evidence.md").write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_fails_for_unsafe_task_directory_name(project_tmp):
    assert init_project(project_tmp)[0] == 0
    unsafe_dir = project_tmp / ".harness" / "tasks" / "Bad Slug"
    unsafe_dir.mkdir(parents=True)

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("unsafe task directory name .harness/tasks/Bad Slug" in message for message in messages)


def test_verify_fails_for_unsafe_manifest_task_path(project_tmp):
    assert init_project(project_tmp)[0] == 0
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest = _manifest(project_tmp)
    manifest["managed_files"].append(
        {
            "path": ".harness/tasks/bad/../task.md",
            "protected": True,
            "hash_algorithm": "sha256",
            "sha256": "abc",
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("unsafe managed task path .harness/tasks/bad/../task.md" in message for message in messages)
