"""Lightweight repository signal detection."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


DOCUMENTED_UNITTEST_COMMAND_RE = re.compile(
    r"(?<![\w.-])(?:py|python|python3)\s+-m\s+unittest(?=$|[\s`])",
    re.IGNORECASE | re.MULTILINE,
)


def detect_documented_test_frameworks(*documents: str) -> list[str]:
    """Detect test runners from explicit commands in captured task text."""

    if any(DOCUMENTED_UNITTEST_COMMAND_RE.search(document) for document in documents):
        return ["python-unittest"]
    return []


def merge_documented_test_frameworks(
    signals: dict[str, Any],
    *documents: str,
) -> dict[str, Any]:
    """Return project signals extended by deterministic documented commands."""

    merged = dict(signals)
    frameworks = list(signals.get("detected_test_frameworks", []))
    for framework in detect_documented_test_frameworks(*documents):
        if framework not in frameworks:
            frameworks.append(framework)
    merged["detected_test_frameworks"] = frameworks
    return merged


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
