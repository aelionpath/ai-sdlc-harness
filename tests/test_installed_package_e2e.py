from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import sysconfig
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SLUG = "clean-room-workflow"
PACKAGE_DATA_FILES = (
    "share/ai-sdlc-harness/templates/task/task.md",
    "share/ai-sdlc-harness/templates/task/acceptance.md",
    "share/ai-sdlc-harness/templates/task/architecture-notes.md",
    "share/ai-sdlc-harness/templates/task/coupling-notes.md",
    "share/ai-sdlc-harness/templates/task/test-contract.md",
    "share/ai-sdlc-harness/templates/task/verification.md",
    "share/ai-sdlc-harness/templates/task/evidence.md",
)
FORBIDDEN_WHEEL_FILES = (
    "share/ai-sdlc-harness/adapters/codex/AGENTS.md.template",
    "share/ai-sdlc-harness/adapters/claude-code/CLAUDE.md.template",
    "share/ai-sdlc-harness/packs/README.md",
)
FORBIDDEN_WHEEL_DIRS = (
    "share/ai-sdlc-harness/templates/harness",
    "planning",
    "release",
    "scripts",
    "tests",
    "docs",
    "examples",
)
ADAPTER_PATHS = (
    ".agents/skills/ai-sdlc-harness/SKILL.md",
    ".claude/skills/ai-sdlc-harness/SKILL.md",
    ".gemini/skills/ai-sdlc-harness/SKILL.md",
)


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == expected, (
        f"command returned {result.returncode}, expected {expected}: {command!r}\n"
        f"{result.stdout}"
    )
    return result


def _venv_executables(venv_root: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        scripts = venv_root / "Scripts"
        return scripts / "python.exe", scripts / "ai-sdlc.exe"
    scripts = venv_root / "bin"
    return scripts / "python", scripts / "ai-sdlc"


def _wheel_payload_parts(member: str) -> tuple[str, ...]:
    """Return an installed payload path, excluding the wheel data wrapper."""

    parts = PurePosixPath(member).parts
    if len(parts) >= 3 and parts[0].endswith(".data") and parts[1] == "data":
        return parts[2:]
    return parts


def _copy_pyyaml_into_venv(venv_python: Path) -> None:
    """Seed the already-required dependency without network or source paths."""

    distribution = importlib.metadata.distribution("PyYAML")
    assert distribution.version.startswith("6.")
    source_root = Path(distribution.locate_file("")).resolve()
    target_text = subprocess.run(
        [str(venv_python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()
    target_root = Path(target_text)

    copied = 0
    for relative in distribution.files or ():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            continue
        source = Path(distribution.locate_file(relative)).resolve()
        try:
            source.relative_to(source_root)
        except ValueError:
            continue
        if not source.is_file():
            continue
        target = target_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    assert copied > 0


def _write_task_sources(task_dir: Path) -> None:
    contents = {
        "task.md": """# Task: Clean Room Workflow

Task slug: `clean-room-workflow`

## Implementation Boundary

Add one minimal target-repository module and its focused check. Keep all Harness source and managed generated files unchanged. No external interface, privilege, sensitive-data, or cross-cutting impact is expected.

## Assumptions And Open Questions

The target repository is disposable and no blocking questions remain.
""",
        "acceptance.md": """# Acceptance

## Requirements And Acceptance Criteria

- The target module returns the deterministic value `clean-room-ok`.
- The focused verification command observes that exact value.

## Protected Behavior And Non-Goals

- Do not edit the Harness source checkout.
- Do not run an external coding agent or claim broader assurance.
""",
        "architecture-notes.md": """# Architecture Notes

## Boundary

Keep the implementation in one target-repository module.

## Responsibility Change

The new module owns one deterministic value.

## Existing Patterns To Preserve

Preserve repository-local Harness state and managed-output ownership.

## Interface And Compatibility Impact

No public interface or compatibility impact is expected.

## Architecture Hygiene

Avoid new dependencies and coupling.

## Security And Privacy Risk Surface

Not applicable; no sensitive or privileged surface changes.

## Trade-Offs And Open Questions

None.
""",
        "coupling-notes.md": """# Coupling Notes

## New Or Changed Coupling

The focused check imports only the new target module.

## Maintainability Sensors

Keep the module and verification deterministic and local.
""",
        "test-contract.md": """# Test Contract

## Characterization Tests

- Harness-managed files and installed adapter skills remain non-destructive.

## Desired Behavior Tests

- Import the target module and assert its exact deterministic value.

## Regression Tests

- Run Harness status, validation, integrity, and installed-package checks.

## Negative And Edge Cases

- An unmanaged adapter collision must block all adapter writes.
- No additional manual-only check is required because the bounded behavior is covered automatically.
""",
        "verification.md": """# Verification

## Commands And Checks Run

- None before implementation.

## Results

- No checks run before implementation.

## Not Run / Why

- `python -m unittest discover -s tests -v` was not run because the target module does not exist yet.

## Manual Review Notes

- Review the generated workset before creating the target module.
""",
        "evidence.md": """# Evidence

## Final Evidence

Expected evidence after implementation: the target module, the exact focused command, and its observed result.

## Generated Or Updated Artifacts

- Expected target: `demo.py`.
""",
    }
    for filename, content in contents.items():
        (task_dir / filename).write_text(content, encoding="utf-8", newline="\n")


def _record_implementation(task_dir: Path) -> None:
    (task_dir / "verification.md").write_text(
        """# Verification

## Commands And Checks Run

python -m unittest discover -s tests -v

## Results

- Passed with exit code 0; the imported target module exposed the exact value `clean-room-ok`.

## Not Run / Why

- None; the complete bounded verification command was run.

## Manual Review Notes

- Confirmed the implementation stayed inside the target repository and the generated workset was reviewed before the edit.
""",
        encoding="utf-8",
        newline="\n",
    )
    (task_dir / "evidence.md").write_text(
        """# Evidence

## Final Evidence

Added the minimal target module because the task requires a deterministic installed-package workflow boundary. The requested acceptance criteria and protected behavior were addressed without changing the Harness source checkout.

## Generated Or Updated Artifacts

- `demo.py`
- Harness task evidence and verification sources

## Tests And Checks Run

- python -m unittest discover -s tests -v

## Results

- Passed with exit code 0 and the exact expected value.

## Not Run / Why

- None; all bounded checks in the task contract were run.

## Known Gaps And Risks

- Deviations: none.
- Known gaps: none.
- Unresolved risks: none.
- Risk acceptances: none.

## References

- `demo.py`
- `.harness/tasks/clean-room-workflow/generated/agent-workset.md`
""",
        encoding="utf-8",
        newline="\n",
    )


def test_built_wheel_runs_complete_workflow_outside_source_checkout(project_tmp):
    artifact_dir = project_tmp / "artifacts"
    venv_root = project_tmp / "venv"
    target_repo = project_tmp / "target-repository"
    artifact_dir.mkdir()
    target_repo.mkdir()
    assert ROOT != target_repo and ROOT not in target_repo.parents

    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    clean_env["PYTHONNOUSERSITE"] = "1"
    clean_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    clean_env["PIP_NO_INDEX"] = "1"

    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(artifact_dir),
            str(ROOT),
        ],
        cwd=project_tmp,
        env=clean_env,
    )
    wheels = list(artifact_dir.glob("ai_sdlc_harness-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    payload_paths = {_wheel_payload_parts(name) for name in names}
    assert "ai_sdlc_harness/cli.py" in names
    for relative in PACKAGE_DATA_FILES:
        assert PurePosixPath(relative).parts in payload_paths
    for relative in FORBIDDEN_WHEEL_FILES:
        assert PurePosixPath(relative).parts not in payload_paths
    for relative in FORBIDDEN_WHEEL_DIRS:
        prefix = PurePosixPath(relative).parts
        assert not any(path[: len(prefix)] == prefix for path in payload_paths)

    venv.EnvBuilder(with_pip=True, clear=True).create(venv_root)
    venv_python, cli = _venv_executables(venv_root)
    _copy_pyyaml_into_venv(venv_python)
    _run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--ignore-installed",
            str(wheel),
        ],
        cwd=project_tmp,
        env=clean_env,
    )
    assert cli.is_file()

    probe = _run(
        [
            str(venv_python),
            "-c",
            (
                "import ai_sdlc_harness,json,pathlib,sys,sysconfig;"
                "data=pathlib.Path(sysconfig.get_path('data'));"
                f"expected={list(PACKAGE_DATA_FILES)!r};"
                "print(json.dumps({'module':str(pathlib.Path(ai_sdlc_harness.__file__).resolve()),"
                "'sys_path':sys.path,'missing':[p for p in expected if not (data/p).is_file()]}))"
            ),
        ],
        cwd=target_repo,
        env=clean_env,
    )
    import_state = json.loads(probe.stdout)
    module_path = Path(import_state["module"])
    assert venv_root.resolve() in module_path.parents
    assert ROOT.resolve() not in module_path.parents
    checkout_root = ROOT.resolve()
    source_root = (ROOT / "src").resolve()
    import_paths = tuple(
        Path(entry).resolve() for entry in import_state["sys_path"] if entry
    )
    assert source_root not in import_paths
    assert all(
        path != checkout_root and checkout_root not in path.parents
        for path in import_paths
    )
    assert import_state["missing"] == []

    (target_repo / "pyproject.toml").write_text(
        "[project]\nname = \"clean-room-target\"\nversion = \"0.0.0\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (target_repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (target_repo / "tests").mkdir()
    workflow_dir = target_repo / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    root_instructions = target_repo / "AGENTS.md"
    root_instructions.write_bytes(b"user-owned root instructions\n")

    version = _run([str(cli), "--version"], cwd=target_repo, env=clean_env)
    assert version.stdout.strip() == "ai-sdlc 1.0.0"
    _run([str(cli), "init"], cwd=target_repo, env=clean_env)

    collision = target_repo / ADAPTER_PATHS[0]
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"user-owned adapter\n")
    blocked = _run(
        [str(cli), "adapter", "install", "all"],
        cwd=target_repo,
        env=clean_env,
        expected=1,
    )
    assert "unmanaged existing adapter file" in blocked.stdout
    assert collision.read_bytes() == b"user-owned adapter\n"
    assert not (target_repo / ADAPTER_PATHS[1]).exists()
    assert not (target_repo / ADAPTER_PATHS[2]).exists()
    collision.unlink()

    installed = _run(
        [str(cli), "adapter", "install", "all"],
        cwd=target_repo,
        env=clean_env,
    )
    assert all(
        f"agent ID: {adapter_id}" in installed.stdout
        for adapter_id in ("codex", "claude-code", "gemini-cli")
    )
    skills = [(target_repo / path).read_bytes() for path in ADAPTER_PATHS]
    assert skills[0] == skills[1] == skills[2]
    assert b"name: ai-sdlc-harness" in skills[0]
    assert root_instructions.read_bytes() == b"user-owned root instructions\n"
    assert not (target_repo / "CLAUDE.md").exists()
    assert not (target_repo / "GEMINI.md").exists()

    _run(
        [str(cli), "task", "start", "Clean Room Workflow"],
        cwd=target_repo,
        env=clean_env,
    )
    initial_status = _run(
        [str(cli), "status", "--task", SLUG, "--json"],
        cwd=target_repo,
        env=clean_env,
    )
    initial_document = json.loads(initial_status.stdout)
    assert initial_document["phase"] == "task_definition"
    assert initial_document["outcome"] == "REVIEW_REQUIRED"

    task_dir = target_repo / ".harness" / "tasks" / SLUG
    _write_task_sources(task_dir)
    _run([str(cli), "init", "--force"], cwd=target_repo, env=clean_env)
    for command in (
        "preflight",
        "spec",
        "test-contract",
        "generate",
    ):
        result = _run(
            [str(cli), command, "--task", SLUG],
            cwd=target_repo,
            env=clean_env,
        )
        if command in {"preflight", "test-contract"}:
            assert "0 blocker(s), 0 warning(s)" in result.stdout, (
                f"unexpected {command} findings:\n{result.stdout}"
            )

    implementation_status = _run(
        [str(cli), "status", "--task", SLUG, "--json"],
        cwd=target_repo,
        env=clean_env,
    )
    implementation_document = json.loads(implementation_status.stdout)
    assert implementation_document["phase"] == "implementation"
    assert implementation_document["outcome"] == "REVIEW_REQUIRED"
    assert (task_dir / "generated" / "agent-workset.md").is_file()

    (target_repo / "demo.py").write_text(
        "VALUE = \"clean-room-ok\"\n",
        encoding="utf-8",
        newline="\n",
    )
    (target_repo / "tests" / "test_demo.py").write_text(
        """import unittest

import demo


class DemoTest(unittest.TestCase):
    def test_value(self):
        self.assertEqual(demo.VALUE, "clean-room-ok")
""",
        encoding="utf-8",
        newline="\n",
    )
    _run(
        [
            str(venv_python),
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ],
        cwd=target_repo,
        env=clean_env,
    )
    _record_implementation(task_dir)
    _run([str(cli), "init", "--force"], cwd=target_repo, env=clean_env)
    _run([str(cli), "evidence", "--task", SLUG], cwd=target_repo, env=clean_env)

    stale = _run(
        [str(cli), "status", "--task", SLUG, "--json"],
        cwd=target_repo,
        env=clean_env,
    )
    stale_document = json.loads(stale.stdout)
    assert stale_document["lineage"]["preflight"] == "stale"
    assert stale_document["next_actions"][0]["command"] == f"ai-sdlc preflight --task {SLUG}"

    for command in (
        "preflight",
        "spec",
        "test-contract",
        "generate",
        "evidence",
        "validate",
    ):
        _run(
            [str(cli), command, "--task", SLUG],
            cwd=target_repo,
            env=clean_env,
        )

    final_status = _run(
        [str(cli), "status", "--task", SLUG],
        cwd=target_repo,
        env=clean_env,
    )
    assert "phase: complete" in final_status.stdout
    assert "outcome: COMPLETE" in final_status.stdout
    verification = _run([str(cli), "verify"], cwd=target_repo, env=clean_env)
    assert verification.stdout.strip() == "AI SDLC Harness verification passed"
