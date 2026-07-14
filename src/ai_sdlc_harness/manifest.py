"""Manifest creation and verification helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .constants import (
    AGENT_WORKSET_FILENAME,
    FUTURE_INTEGRITY_NOTE,
    HARNESS_VERSION,
    HASHED_MANAGED_FILE_PATHS,
    MANAGED_FILE_PATHS,
    PROTECTED_MANAGED_FILE_PATHS,
    MANIFEST_MANAGED_TASK_FILENAMES,
    TASK_GENERATED_DIRNAME,
    TASK_SLUG_PATTERN,
    TEMPLATE_VERSION,
)
from .files import resolve_under_root


MANIFEST_PATH = PurePosixPath(".harness/manifest.json")
TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_task_artifact_path(managed_path: str | PurePosixPath) -> bool:
    path = PurePosixPath(str(managed_path).replace("\\", "/"))
    direct_task_artifact = (
        len(path.parts) == 4
        and path.parts[0] == ".harness"
        and path.parts[1] == "tasks"
        and TASK_SLUG_RE.fullmatch(path.parts[2]) is not None
        and path.parts[3] in MANIFEST_MANAGED_TASK_FILENAMES
    )
    generated_workset = (
        len(path.parts) == 5
        and path.parts[0] == ".harness"
        and path.parts[1] == "tasks"
        and TASK_SLUG_RE.fullmatch(path.parts[2]) is not None
        and path.parts[3] == TASK_GENERATED_DIRNAME
        and path.parts[4] == AGENT_WORKSET_FILENAME
    )
    return direct_task_artifact or generated_workset


def _existing_manifest_task_paths(root: Path) -> list[PurePosixPath]:
    manifest_target = resolve_under_root(root, MANIFEST_PATH)
    if not manifest_target.is_file():
        return []

    try:
        existing = load_manifest(manifest_target)
    except Exception:
        return []

    entries = existing.get("managed_files")
    if not isinstance(entries, list):
        return []

    task_paths: list[PurePosixPath] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path_text = str(entry.get("path", ""))
        if is_task_artifact_path(path_text):
            task_paths.append(PurePosixPath(path_text))
    return task_paths


def _ordered_unique(paths: Iterable[PurePosixPath]) -> list[PurePosixPath]:
    seen: set[str] = set()
    ordered: list[PurePosixPath] = []
    for path in paths:
        path_text = path.as_posix()
        if path_text in seen:
            continue
        seen.add(path_text)
        ordered.append(path)
    return ordered


def build_manifest(root: Path, extra_managed_paths: Iterable[PurePosixPath] = ()) -> dict[str, Any]:
    """Build the manifest for existing managed files."""

    hashed_paths = {path.as_posix() for path in HASHED_MANAGED_FILE_PATHS}
    protected_paths = {path.as_posix() for path in PROTECTED_MANAGED_FILE_PATHS}
    managed_files: list[dict[str, Any]] = []
    managed_paths = _ordered_unique(
        (
            *MANAGED_FILE_PATHS,
            *_existing_manifest_task_paths(root),
            *extra_managed_paths,
        )
    )
    for managed_path in managed_paths:
        path_text = managed_path.as_posix()
        target = resolve_under_root(root, managed_path)
        should_hash = path_text in hashed_paths or is_task_artifact_path(managed_path)
        protected = path_text in protected_paths or is_task_artifact_path(managed_path)
        entry: dict[str, Any] = {
            "path": path_text,
            "protected": protected,
            "hash_algorithm": "sha256" if should_hash else None,
            "sha256": sha256_file(target) if should_hash and target.is_file() else None,
        }
        managed_files.append(entry)

    return {
        "harness_version": HARNESS_VERSION,
        "template_version": TEMPLATE_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "managed_files": managed_files,
        "integrity": {
            "manifest_self_hash": "excluded-in-phase-1",
            "release_verification": "deferred",
            "note": FUTURE_INTEGRITY_NOTE,
        },
    }


def write_manifest(root: Path, extra_managed_paths: Iterable[PurePosixPath] = ()) -> None:
    manifest = build_manifest(root, extra_managed_paths)
    target = resolve_under_root(root, MANIFEST_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    return data
