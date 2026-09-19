"""Implementation of ``ai-sdlc verify``."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .constants import (
    EXPECTED_DIRECTORIES,
    KNOWN_PACK_IDS,
    MANAGED_FILE_PATHS,
    TASK_ARTIFACT_FILENAMES,
    TASK_SLUG_PATTERN,
)
from .files import (
    PathSafetyError,
    read_text,
    resolve_managed_output_under_root,
    resolve_under_root,
)
from .lineage import LineageValidationError, validate_manifest_provenance
from .manifest import (
    ManifestV2,
    is_adapter_artifact_path,
    is_task_artifact_path,
    load_manifest_model,
    manifest_to_data,
    sha256_file,
)


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)


def _is_human_owned_task_source(path_text: str) -> bool:
    path = PurePosixPath(path_text)
    return (
        len(path.parts) == 4
        and path.parts[:2] == (".harness", "tasks")
        and TASK_SLUG_RE.fullmatch(path.parts[2]) is not None
        and path.name in TASK_ARTIFACT_FILENAMES
    )


def _is_deferred_human_source_readiness(
    path_text: str,
    target: Path,
    *,
    allow_human_source_readiness: bool,
) -> bool:
    """Return whether workflow navigation may defer this missing source file."""

    return (
        allow_human_source_readiness
        and _is_human_owned_task_source(path_text)
        and not target.exists()
    )


def _read_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return yaml.safe_load(file_obj)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def _resolve_managed_path(
    root: Path,
    managed_path: str | PurePosixPath,
    failures: list[str],
    unsafe_managed_paths: set[str],
) -> Path | None:
    path_text = (
        managed_path.as_posix()
        if isinstance(managed_path, PurePosixPath)
        else managed_path
    )
    if path_text in unsafe_managed_paths:
        return None
    try:
        return resolve_managed_output_under_root(root, managed_path)
    except (PathSafetyError, OSError, RuntimeError):
        unsafe_managed_paths.add(path_text)
        failures.append(f"unsafe managed path {path_text}")
        return None


def verify_project(
    root: Path,
    *,
    allow_human_source_readiness: bool = False,
) -> tuple[int, list[str]]:
    """Verify managed repository integrity.

    Workflow navigation may opt to defer genuinely absent human-owned task
    sources to its readiness gates. All other integrity failures remain strict.
    """

    messages: list[str] = []
    harness_root = root / ".harness"
    if not harness_root.is_dir():
        return 1, ["AI SDLC Harness is not initialized. Run `ai-sdlc init` from the project root."]

    failures: list[str] = []
    unsafe_managed_paths: set[str] = set()

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
        target = _resolve_managed_path(
            root,
            managed_path,
            failures,
            unsafe_managed_paths,
        )
        if target is None:
            continue
        if not target.is_file():
            failures.append(f"missing file {managed_path.as_posix()}")

    config: dict[str, Any] = {}
    selected: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    manifest_model = None
    parse_targets = {
        PurePosixPath(".harness/config.yaml"): "yaml",
        PurePosixPath(".harness/state.json"): "json",
        PurePosixPath(".harness/manifest.json"): "manifest",
        PurePosixPath(".harness/packs/selected.yaml"): "yaml",
        PurePosixPath(".harness/generated/agent-instructions.md"): "text",
    }
    for managed_path, parser in parse_targets.items():
        target = _resolve_managed_path(
            root,
            managed_path,
            failures,
            unsafe_managed_paths,
        )
        if target is None:
            continue
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
                manifest_model = load_manifest_model(target)
                manifest = manifest_to_data(manifest_model)
            else:
                read_text(target)
        except Exception as exc:
            failures.append(f"could not read {managed_path.as_posix()}: {exc}")

    if isinstance(manifest_model, ManifestV2):
        try:
            validate_manifest_provenance(manifest_model)
        except LineageValidationError as exc:
            failures.append(
                "invalid generated-artifact provenance in .harness/manifest.json: "
                f"{exc}"
            )

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
                if path_text.startswith(".harness/tasks/") or is_adapter_artifact_path(path_text):
                    if (
                        path_text.startswith(".harness/tasks/")
                        and not is_task_artifact_path(path_text)
                    ):
                        failures.append(f"unsafe managed task path {path_text}")
                        continue
                    if is_adapter_artifact_path(path_text):
                        entry = by_path[path_text]
                        if (
                            entry.get("protected") is not True
                            or entry.get("hash_algorithm") != "sha256"
                            or not entry.get("sha256")
                        ):
                            failures.append(
                                f"invalid managed adapter record {path_text}"
                            )
                    target = _resolve_managed_path(
                        root,
                        path_text,
                        failures,
                        unsafe_managed_paths,
                    )
                    if target is None:
                        continue
                    if not target.is_file():
                        if _is_deferred_human_source_readiness(
                            path_text,
                            target,
                            allow_human_source_readiness=allow_human_source_readiness,
                        ):
                            continue
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
                target = _resolve_managed_path(
                    root,
                    path_text,
                    failures,
                    unsafe_managed_paths,
                )
                if target is None:
                    continue
                if not target.is_file():
                    if _is_deferred_human_source_readiness(
                        path_text,
                        target,
                        allow_human_source_readiness=allow_human_source_readiness,
                    ):
                        continue
                    failures.append(f"missing file {path_text}")
                else:
                    try:
                        actual_hash = sha256_file(target)
                    except OSError:
                        failures.append(f"could not read {path_text}")
                        continue
                    if (
                        not _is_human_owned_task_source(path_text)
                        and actual_hash != expected_hash
                    ):
                        failures.append(f"hash drift detected for {path_text}")

    if failures:
        messages.append("AI SDLC Harness verification failed")
        messages.extend(f"- {failure}" for failure in failures)
        return 1, messages

    messages.append("AI SDLC Harness verification passed")
    return 0, messages
