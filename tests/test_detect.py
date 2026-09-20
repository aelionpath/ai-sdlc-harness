from __future__ import annotations

import pytest

from ai_sdlc_harness.detect import (
    detect_documented_test_frameworks,
    merge_documented_test_frameworks,
)


@pytest.mark.parametrize(
    "command",
    (
        "py -m unittest -v",
        "python -m unittest",
        "python3 -m unittest",
    ),
)
def test_documented_unittest_commands_are_recognized(command):
    assert detect_documented_test_frameworks(
        f"## Commands And Checks Run\n\n`{command}`\n"
    ) == ["python-unittest"]


@pytest.mark.parametrize(
    "text",
    (
        "The unittest module is used by this repository.",
        "import unittest",
        "class Example(unittest.TestCase):",
        "python -m unittesting",
    ),
)
def test_incidental_unittest_prose_and_code_are_not_commands(text):
    assert detect_documented_test_frameworks(text) == []


def test_documented_framework_merge_preserves_existing_signal_order():
    signals = {"detected_test_frameworks": ["tests-directory-or-pytest"]}

    merged = merge_documented_test_frameworks(
        signals,
        "Run py -m unittest -v.",
    )

    assert merged["detected_test_frameworks"] == [
        "tests-directory-or-pytest",
        "python-unittest",
    ]
    assert signals == {"detected_test_frameworks": ["tests-directory-or-pytest"]}
