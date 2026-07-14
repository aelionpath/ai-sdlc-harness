from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project_tmp(tmp_path):
    return tmp_path
