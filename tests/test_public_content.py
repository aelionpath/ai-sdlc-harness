from __future__ import annotations

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

    assert data["project"]["version"] == "1.0.0"
    assert "Development Status :: 4 - Beta" in data["project"]["classifiers"]
    assert "Development Status :: 3 - Alpha" not in data["project"]["classifiers"]
