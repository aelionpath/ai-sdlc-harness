from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _load_paths(policy_name: str) -> list[str]:
    policy = yaml.safe_load((ROOT / "release" / policy_name).read_text(encoding="utf-8"))
    return [str(path) for path in policy["paths"]]


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_public_exported_file(path: Path, allow_roots: list[Path], deny_roots: list[Path]) -> bool:
    if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
        return False
    if any(part == "__pycache__" or part.endswith(".egg-info") for part in path.parts):
        return False
    if any(_is_under(path, deny_root) for deny_root in deny_roots):
        return False
    return any(_is_under(path, allow_root) for allow_root in allow_roots)


def _public_files() -> list[Path]:
    allowlist = ROOT / "release" / "public-allowlist.yaml"
    denylist = ROOT / "release" / "public-denylist.yaml"
    if allowlist.is_file() and denylist.is_file():
        allow_roots = [(ROOT / relative).resolve() for relative in _load_paths("public-allowlist.yaml")]
        deny_roots = [(ROOT / relative).resolve() for relative in _load_paths("public-denylist.yaml")]
        return [
            path
            for allow_root in allow_roots
            if allow_root.exists()
            for path in ([allow_root] if allow_root.is_file() else allow_root.rglob("*"))
            if _is_public_exported_file(path.resolve(), allow_roots, deny_roots)
        ]

    ignored_roots = {".git", "__pycache__"}
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in TEXT_SUFFIXES
        and not any(part in ignored_roots or part.endswith(".egg-info") for part in path.relative_to(ROOT).parts)
    ]


def test_public_exported_files_avoid_private_scaffold_language():
    public_files = _public_files()

    assert public_files
    forbidden = (
        "mode: " + "lite",
        "lite" + " mode",
        "standard" + " mode",
        "strict" + " mode",
        "status: " + "placeholder",
        "v0" + ".1",
        "v0" + ".1.0",
        "Phase " + "1",
    )
    failures: list[str] = []
    for path in public_files:
        text = path.read_text(encoding="utf-8")
        for phrase in forbidden:
            if phrase in text:
                relative = path.relative_to(ROOT).as_posix()
                failures.append(f"{relative}: {phrase}")

    assert failures == []


def test_public_exported_files_do_not_make_security_or_compliance_guarantees():
    public_files = _public_files()

    assert public_files
    forbidden = (
        "harness guarantees " + "security",
        "harness guarantees " + "compliance",
        "security " + "validated",
        "security " + "passed",
        "security " + "certified",
        "certified " + "secure",
        "compliance " + "certified",
        "certified " + "compliant",
        "harness is " + "secure",
        "harness is " + "compliant",
    )
    failures: list[str] = []
    for path in public_files:
        text = path.read_text(encoding="utf-8").lower()
        for phrase in forbidden:
            if phrase in text:
                relative = path.relative_to(ROOT).as_posix()
                failures.append(f"{relative}: {phrase}")

    assert failures == []


def test_public_pack_surface_has_no_legacy_baseline_metadata():
    legacy_pack_files = {
        "architecture-generic.yaml",
        "coupling-baseline.yaml",
        "observability-baseline.yaml",
        "security-baseline.yaml",
    }

    assert not any((ROOT / "packs" / filename).exists() for filename in legacy_pack_files)
    assert list((ROOT / "packs").glob("*.yaml")) == []
    assert (ROOT / "packs" / "README.md").is_file()

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for filename in legacy_pack_files:
        assert f"packs/{filename}" not in pyproject


def test_public_metadata_uses_release_identity():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["build-system"]["requires"] == ["setuptools>=77.0.3", "wheel"]
    assert data["project"]["version"] == "1.0.0"
    assert data["project"]["requires-python"] == ">=3.10"
    assert data["project"]["license"] == "Apache-2.0"
    assert data["project"]["license-files"] == ["LICENSE", "NOTICE"]
    assert data["project"]["dependencies"] == ["PyYAML>=6,<7"]
    assert data["project"]["optional-dependencies"]["test"] == [
        "pytest>=8,<9",
        "setuptools>=77.0.3",
        "wheel",
    ]
    assert data["project"]["urls"] == {
        "Homepage": "https://github.com/aelionpath/ai-sdlc-harness",
        "Repository": "https://github.com/aelionpath/ai-sdlc-harness",
        "Documentation": "https://github.com/aelionpath/ai-sdlc-harness/tree/main/docs",
        "Issues": "https://github.com/aelionpath/ai-sdlc-harness/issues",
        "Changelog": "https://github.com/aelionpath/ai-sdlc-harness/releases",
        "Security": "https://github.com/aelionpath/ai-sdlc-harness/security/policy",
    }
    assert "Development Status :: 4 - Beta" not in data["project"]["classifiers"]
    assert "Development Status :: 3 - Alpha" not in data["project"]["classifiers"]
    assert "Development Status :: 5 - Production/Stable" not in data["project"]["classifiers"]
    assert "License :: OSI Approved :: Apache Software License" not in data["project"]["classifiers"]


def test_public_installation_guidance_separates_users_from_contributors():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs/quickstart.md").read_text(encoding="utf-8")
    compatibility = (ROOT / "docs/compatibility.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    for public_guide in (readme, quickstart):
        assert "uv tool install ai-sdlc-harness" in public_guide
        assert "pipx install ai-sdlc-harness" in public_guide
        assert "ai-sdlc --version" in public_guide
        assert "python -m pip install -e" not in public_guide
    assert "uv tool upgrade ai-sdlc-harness" in quickstart
    assert "uv tool install ai-sdlc-harness==1.0.0" in quickstart
    assert "uv tool uninstall ai-sdlc-harness" in quickstart
    assert "python -m pip install -e \".[test]\"" in contributing
    for public_guide in (readme, quickstart, compatibility):
        assert "Package-registry publication is not part" not in public_guide


def test_public_security_guidance_uses_private_vulnerability_reporting():
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert (
        "https://github.com/aelionpath/ai-sdlc-harness/security/advisories/new"
        in security
    )
    assert "exploit details in public issues" in security


def test_public_contributor_guidance_uses_the_public_repository_workflow():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "branch from `main`" in contributing
    assert "pull requests against `main`" in contributing
    assert "branch from `integration`" not in contributing
    assert "ai-sdlc-harness-public-test" not in contributing
    assert "release-boundary and hygiene tests" not in contributing


def test_public_codeowners_uses_the_approved_location_and_owner():
    assert not (ROOT / "CODEOWNERS").exists()
    assert (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8") == "* @e2hln\n"


def test_all_six_real_symlink_cases_use_fail_closed_ci_enforcement():
    expected_cases = {
        "tests/test_files_and_redaction.py": {
            "test_managed_output_resolver_rejects_direct_in_root_symlink",
            "test_managed_output_resolver_rejects_outside_root_symlink",
            "test_managed_output_resolver_rejects_symlinked_parent",
        },
        "tests/test_preflight.py": {
            "test_preflight_rejects_symlinked_output_before_ownership_checks",
        },
        "tests/test_spec.py": {
            "test_spec_rejects_symlinked_output_before_ownership_checks",
        },
        "tests/test_test_contract.py": {
            "test_test_contract_rejects_symlinked_managed_output",
        },
    }

    assert sum(len(names) for names in expected_cases.values()) == 6
    for relative_path, expected_names in expected_cases.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert 'os.environ.get("AI_SDLC_REQUIRE_REAL_SYMLINKS") == "1"' in text
        assert "pytest.fail(" in text
        functions = {
            node.name: node
            for node in ast.parse(text).body
            if isinstance(node, ast.FunctionDef)
        }
        for name in expected_names:
            assert any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_symlink_or_skip"
                for node in ast.walk(functions[name])
            )


def test_public_ci_matrix_permissions_and_pinned_actions_are_fail_closed():
    workflow_path = ROOT / ".github/workflows/ci.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)

    assert workflow["name"] == "CI"
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"] == {
        "pull_request": {"branches": ["main"]},
        "push": {"branches": ["main"]},
    }
    assert "pull_request_target" not in workflow_text
    assert "secrets." not in workflow_text
    assert "id-token:" not in workflow_text
    assert "packages:" not in workflow_text

    jobs = workflow["jobs"]
    test_job = jobs["test"]
    assert test_job["strategy"]["fail-fast"] == "false"
    assert test_job["strategy"]["matrix"]["include"] == [
        {"os": "ubuntu-24.04", "python-version": "3.10"},
        {"os": "ubuntu-24.04", "python-version": "3.11"},
        {"os": "ubuntu-24.04", "python-version": "3.12"},
        {"os": "ubuntu-24.04", "python-version": "3.13"},
        {"os": "ubuntu-24.04", "python-version": "3.14"},
        {"os": "windows-2022", "python-version": "3.10"},
        {"os": "windows-2022", "python-version": "3.14"},
        {"os": "macos-14", "python-version": "3.10"},
        {"os": "macos-14", "python-version": "3.14"},
    ]

    pinned_actions = re.findall(r"^\s*uses:\s*(\S+)", workflow_text, flags=re.MULTILINE)
    assert pinned_actions == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    ]
    assert "# v7.0.1" in workflow_text
    assert "# v7.0.0" in workflow_text

    test_steps = {step["name"]: step for step in test_job["steps"]}
    setup_step = test_steps["Set up stable Python"]
    assert setup_step["with"]["allow-prereleases"] == "false"
    assert setup_step["with"]["check-latest"] == "true"
    posix_step = test_steps["Run tests with required real symlinks"]
    windows_step = test_steps["Run tests on Windows"]
    assert posix_step["if"] == "runner.os != 'Windows'"
    assert posix_step["env"] == {"AI_SDLC_REQUIRE_REAL_SYMLINKS": "1"}
    assert windows_step["if"] == "runner.os == 'Windows'"
    assert "env" not in windows_step


def test_public_ci_distribution_and_required_jobs_are_non_publishing_and_fail_closed():
    workflow_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]

    distribution = jobs["distribution"]
    assert distribution["runs-on"] == "ubuntu-24.04"
    distribution_steps = {step["name"]: step for step in distribution["steps"]}
    distribution_setup = distribution_steps["Set up stable Python"]
    assert distribution_setup["with"] == {
        "python-version": "3.12",
        "allow-prereleases": "false",
        "check-latest": "true",
    }
    commands = "\n".join(
        step.get("run", "") for step in distribution["steps"]
    )
    assert 'python -m pip install ".[test]" build twine' in commands
    assert "python -m build --outdir" in commands
    assert "python -m twine check --strict" in commands
    assert "python .github/audit_release_artifacts.py \"$DIST_DIR\"" in commands
    assert "python .github/audit_release_artifacts.py --verify \"$DIST_DIR\"" in commands
    assert 'python -m venv "$RUNNER_TEMP/wheel-venv"' in commands
    assert 'python -m venv "$RUNNER_TEMP/sdist-venv"' in commands
    assert "ai-sdlc\" --version" in commands
    assert "ai-sdlc\" --help" in commands
    assert "ai-sdlc\" init" in commands
    assert "upload" not in commands.lower()
    assert "publish" not in commands.lower()

    required = jobs["required"]
    assert required["name"] == "required"
    assert required["if"] == "always()"
    assert required["needs"] == ["test", "distribution"]
    required_step = required["steps"][0]
    assert required_step["env"] == {
        "TEST_RESULT": "${{ needs.test.result }}",
        "DISTRIBUTION_RESULT": "${{ needs.distribution.result }}",
    }
    assert 'test "$TEST_RESULT" = "success"' in required_step["run"]
    assert 'test "$DISTRIBUTION_RESULT" = "success"' in required_step["run"]
