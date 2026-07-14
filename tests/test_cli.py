from __future__ import annotations

import subprocess
import sys
import tomllib
import os
from pathlib import Path

import pytest

from ai_sdlc_harness.cli import main


ROOT = Path(__file__).resolve().parents[1]


def test_ai_sdlc_help_works(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    output = capsys.readouterr().out

    assert excinfo.value.code == 0
    assert "ai-sdlc" in output
    assert "init" in output
    assert "status" in output
    assert "verify" in output
    assert "preflight" in output
    assert "spec" in output
    assert "test-contract" in output
    assert "generate" in output
    assert "evidence" in output
    assert "validate" in output
    assert "task" in output


def test_python_module_help_works():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "ai_sdlc_harness", "--help"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "ai-sdlc" in result.stdout
    assert "init" in result.stdout
    assert "preflight" in result.stdout
    assert "spec" in result.stdout
    assert "test-contract" in result.stdout
    assert "generate" in result.stdout
    assert "evidence" in result.stdout
    assert "validate" in result.stdout
    assert "task" in result.stdout


def test_python_module_version_reports_release_version():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "ai_sdlc_harness", "--version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "ai-sdlc 1.0.0"


def test_console_entry_point_is_configured():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["scripts"]["ai-sdlc"] == "ai_sdlc_harness.cli:main"
    assert data["project"]["requires-python"] == ">=3.10"
    assert "PyYAML>=6,<7" in data["project"]["dependencies"]
