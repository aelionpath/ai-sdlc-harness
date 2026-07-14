"""Lightweight repository signal detection."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def detect_project_signals(root: Path) -> dict[str, Any]:
    """Detect project signals using standard-library filesystem checks."""

    signals: dict[str, Any] = {
        "git_repo": (root / ".git").exists(),
        "files": {
            "pyproject.toml": (root / "pyproject.toml").is_file(),
            "package.json": (root / "package.json").is_file(),
            "Cargo.toml": (root / "Cargo.toml").is_file(),
            "go.mod": (root / "go.mod").is_file(),
            "pytest.ini": (root / "pytest.ini").is_file(),
            "AGENTS.md": (root / "AGENTS.md").is_file(),
            "CLAUDE.md": (root / "CLAUDE.md").is_file(),
        },
        "directories": {
            "tests": (root / "tests").is_dir(),
            ".github/workflows": (root / ".github" / "workflows").is_dir(),
        },
        "detected_languages": [],
        "detected_package_managers": [],
        "detected_test_frameworks": [],
        "detected_ci": [],
        "existing_agent_files": [],
    }

    if signals["files"]["pyproject.toml"]:
        signals["detected_languages"].append("python")
        signals["detected_package_managers"].append("python-packaging")
    if signals["files"]["package.json"]:
        signals["detected_languages"].append("javascript-or-typescript")
        signals["detected_package_managers"].append("npm-compatible")
    if signals["files"]["Cargo.toml"]:
        signals["detected_languages"].append("rust")
        signals["detected_package_managers"].append("cargo")
    if signals["files"]["go.mod"]:
        signals["detected_languages"].append("go")
        signals["detected_package_managers"].append("go-modules")
    if signals["directories"]["tests"] or signals["files"]["pytest.ini"]:
        signals["detected_test_frameworks"].append("tests-directory-or-pytest")
    if signals["directories"][".github/workflows"]:
        signals["detected_ci"].append("github-actions")
    if signals["files"]["AGENTS.md"]:
        signals["existing_agent_files"].append("AGENTS.md")
    if signals["files"]["CLAUDE.md"]:
        signals["existing_agent_files"].append("CLAUDE.md")

    return signals
