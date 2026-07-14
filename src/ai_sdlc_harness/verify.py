"""Implementation of ``ai-sdlc verify``."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .constants import EXPECTED_DIRECTORIES, KNOWN_PACK_IDS, MANAGED_FILE_PATHS, TASK_SLUG_PATTERN
from .files import read_text, resolve_under_root
from .manifest import is_task_artifact_path, load_manifest, sha256_file


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return yaml.safe_load(file_obj)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def verify_project(root: Path) -> tuple[int, list[str]]:
    messages: list[str] = []
    harness_root = root / ".harness"
    if not harness_root.is_dir():
        return 1, ["AI SDLC Harness is not initialized. Run `ai-sdlc init` from the project root."]

    failures: list[str] = []

    for directory in EXPECTED_DIRECTORIES:
        target = resolve_under_root(root, directory)
        if not target.is_dir():
            failures.append(f"missing directory {directory.as_posix()}")

    task_root = resolve_under_root(root, ".harness/tasks")
    if task_root.is_dir():
        for child in task_root.iterdir():
            if child.is_dir() and TASK_SLUG_RE.fullmatch(child.name) is None:
                failures.append(f"unsafe task directory name .harness/tasks/{child.name}")

    for managed_path in MANAGED_FILE_PATHS:
        target = resolve_under_root(root, managed_path)
        if not target.is_file():
            failures.append(f"missing file {managed_path.as_posix()}")

    config: dict[str, Any] = {}
    selected: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    parse_targets = {
        PurePosixPath(".harness/config.yaml"): "yaml",
        PurePosixPath(".harness/state.json"): "json",
        PurePosixPath(".harness/manifest.json"): "manifest",
        PurePosixPath(".harness/packs/selected.yaml"): "yaml",
        PurePosixPath(".harness/generated/agent-instructions.md"): "text",
    }
    for managed_path, parser in parse_targets.items():
        target = resolve_under_root(root, managed_path)
        if not target.is_file():
            continue
        try:
            if parser == "yaml":
                loaded = _read_yaml(target)
                if managed_path.name == "config.yaml" and isinstance(loaded, dict):
                    config = loaded
                if managed_path.name == "selected.yaml" and isinstance(loaded, dict):
                    selected = loaded
            elif parser == "json":
                _read_json(target)
            elif parser == "manifest":
                manifest = load_manifest(target)
            else:
                read_text(target)
        except Exception as exc:
            failures.append(f"could not read {managed_path.as_posix()}: {exc}")

    selected_ids: list[str] = []
    if isinstance(config.get("selected_packs"), list):
        selected_ids.extend(str(item) for item in config["selected_packs"])
    if isinstance(selected.get("selected_packs"), list):
        selected_ids.extend(str(item.get("id")) for item in selected["selected_packs"] if isinstance(item, dict))
    unknown_packs = sorted({pack_id for pack_id in selected_ids if pack_id not in KNOWN_PACK_IDS})
    if unknown_packs:
        failures.append(f"unknown selected pack IDs: {', '.join(unknown_packs)}")

    if manifest:
        entries = manifest.get("managed_files")
        if not isinstance(entries, list):
            failures.append("manifest managed_files must be a list")
        else:
            by_path = {str(entry.get("path")): entry for entry in entries if isinstance(entry, dict)}
            known_base_paths = {path.as_posix() for path in MANAGED_FILE_PATHS}
            for path_text in by_path:
                if path_text in known_base_paths:
                    continue
                if path_text.startswith(".harness/tasks/"):
                    if not is_task_artifact_path(path_text):
                        failures.append(f"unsafe managed task path {path_text}")
                        continue
                    try:
                        target = resolve_under_root(root, path_text)
                    except Exception as exc:
                        failures.append(f"unsafe managed task path {path_text}: {exc}")
                        continue
                    if not target.is_file():
                        failures.append(f"missing file {path_text}")
                        continue
                    try:
                        read_text(target)
                    except Exception as exc:
                        failures.append(f"could not read {path_text}: {exc}")
                    continue
                failures.append(f"unknown managed file entry {path_text}")

            for managed_path in MANAGED_FILE_PATHS:
                path_text = managed_path.as_posix()
                entry = by_path.get(path_text)
                if not entry:
                    failures.append(f"manifest missing managed file entry {path_text}")
                    continue
            for path_text, entry in by_path.items():
                if entry.get("hash_algorithm") != "sha256":
                    continue
                expected_hash = entry.get("sha256")
                if not expected_hash:
                    failures.append(f"manifest missing hash for {path_text}")
                    continue
                try:
                    target = resolve_under_root(root, path_text)
                except Exception as exc:
                    failures.append(f"unsafe managed path {path_text}: {exc}")
                    continue
                if not target.is_file():
                    failures.append(f"missing file {path_text}")
                elif sha256_file(target) != expected_hash:
                    failures.append(f"hash drift detected for {path_text}")

    if failures:
        messages.append("AI SDLC Harness verification failed")
        messages.extend(f"- {failure}" for failure in failures)
        return 1, messages

    messages.append("AI SDLC Harness verification passed")
    return 0, messages
