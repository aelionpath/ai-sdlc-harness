from __future__ import annotations

import hashlib
import json
from pathlib import Path


RELEASED_V1_FIXTURE = (
    Path(__file__).parent / "fixtures" / "main-manifest-v1.json"
)


def install_released_v1_with_current_task_records(root: Path) -> dict:
    """Install the frozen v1 contract plus current historically valid tasks."""

    manifest_path = root / ".harness" / "manifest.json"
    current = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = json.loads(RELEASED_V1_FIXTURE.read_text(encoding="utf-8"))
    base_paths = {record["path"] for record in data["managed_files"]}

    task_records = [
        dict(record)
        for record in current["managed_files"]
        if record["path"].startswith(".harness/tasks/")
        and record["path"] not in base_paths
    ]
    data["managed_files"].extend(task_records)
    for record in data["managed_files"]:
        if record["hash_algorithm"] != "sha256":
            continue
        target = root / record["path"]
        record["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()

    manifest_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return data
