from __future__ import annotations

from pathlib import Path


def test_project_tmp_uses_temp_path_outside_repo(project_tmp):
    repo_root = Path.cwd().resolve()
    workspace = project_tmp.resolve()

    assert workspace != repo_root
    assert repo_root not in workspace.parents
    assert not (repo_root / ".test-workspaces").exists()
