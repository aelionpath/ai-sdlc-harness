"""Canonical main-manifest models, parsing, rendering, and persistence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence, Union

from .constants import (
    ADAPTER_ARTIFACT_PATHS,
    AGENT_WORKSET_FILENAME,
    CONTEXT_MANIFEST_FILENAME,
    FUTURE_INTEGRITY_NOTE,
    HARNESS_VERSION,
    HASHED_MANAGED_FILE_PATHS,
    MANAGED_FILE_PATHS,
    MANIFEST_MANAGED_TASK_FILENAMES,
    PROTECTED_MANAGED_FILE_PATHS,
    TASK_GENERATED_DIRNAME,
    TASK_SLUG_PATTERN,
    TEMPLATE_VERSION,
)
from .files import resolve_under_root, sha256_file, write_bytes_atomic


MANIFEST_PATH = PurePosixPath(".harness/manifest.json")
MANIFEST_SCHEMA_VERSION = 2
TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEPENDENCY_STATES = frozenset({"present", "missing", "unreadable", "not_regular"})
OBSERVATION_PREDICATES = frozenset({"exists", "is_file", "is_dir"})

_V1_FIELDS = frozenset(
    {
        "harness_version",
        "template_version",
        "generated_at",
        "managed_files",
        "integrity",
    }
)
_V2_FIELDS = frozenset(
    {
        "manifest_schema_version",
        "harness_version",
        "template_version",
        "generated_at",
        "managed_files",
        "generated_artifact_provenance",
        "integrity",
    }
)
_MANAGED_FILE_FIELDS = frozenset({"path", "protected", "hash_algorithm", "sha256"})
_INTEGRITY_FIELDS = frozenset({"manifest_self_hash", "release_verification", "note"})
_PROVENANCE_FIELDS = frozenset(
    {
        "output_path",
        "producer_command",
        "producer_version",
        "task_slug",
        "output_sha256",
        "dependencies",
        "repository_observations",
    }
)
_DEPENDENCY_FIELDS = frozenset({"dependency_path", "dependency_state", "dependency_sha256"})
_OBSERVATION_FIELDS = frozenset({"observation_path", "predicate", "result"})
_BASE_PATH_ORDER = {path.as_posix(): index for index, path in enumerate(MANAGED_FILE_PATHS)}


class ManifestValidationError(ValueError):
    """Raised when a main manifest does not match a supported schema."""


@dataclass(frozen=True)
class ManagedFileRecord:
    path: str
    protected: bool
    hash_algorithm: str | None
    sha256: str | None


@dataclass(frozen=True)
class IntegrityRecord:
    manifest_self_hash: str
    release_verification: str
    note: str


@dataclass(frozen=True)
class ProvenanceDependency:
    dependency_path: str
    dependency_state: str
    dependency_sha256: str | None


@dataclass(frozen=True)
class ProvenanceObservation:
    observation_path: str
    predicate: str
    result: bool


@dataclass(frozen=True)
class GeneratedArtifactProvenance:
    output_path: str
    producer_command: str
    producer_version: int
    task_slug: str
    output_sha256: str
    dependencies: tuple[ProvenanceDependency, ...]
    repository_observations: tuple[ProvenanceObservation, ...]


@dataclass(frozen=True)
class ManifestV1:
    harness_version: str
    template_version: str
    generated_at: str
    managed_files: tuple[ManagedFileRecord, ...]
    integrity: IntegrityRecord

    @property
    def schema_version(self) -> int:
        return 1


@dataclass(frozen=True)
class ManifestV2:
    harness_version: str
    template_version: str
    generated_at: str
    managed_files: tuple[ManagedFileRecord, ...]
    generated_artifact_provenance: tuple[GeneratedArtifactProvenance, ...]
    integrity: IntegrityRecord
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION

    @property
    def schema_version(self) -> int:
        return MANIFEST_SCHEMA_VERSION


MainManifest = Union[ManifestV1, ManifestV2]


def is_task_artifact_path(managed_path: str | PurePosixPath) -> bool:
    path = PurePosixPath(str(managed_path).replace("\\", "/"))
    direct_task_artifact = (
        len(path.parts) == 4
        and path.parts[0] == ".harness"
        and path.parts[1] == "tasks"
        and TASK_SLUG_RE.fullmatch(path.parts[2]) is not None
        and path.parts[3] in MANIFEST_MANAGED_TASK_FILENAMES
    )
    generated_artifact = (
        len(path.parts) == 5
        and path.parts[0] == ".harness"
        and path.parts[1] == "tasks"
        and TASK_SLUG_RE.fullmatch(path.parts[2]) is not None
        and path.parts[3] == TASK_GENERATED_DIRNAME
        and path.parts[4] in {
            AGENT_WORKSET_FILENAME,
            CONTEXT_MANIFEST_FILENAME,
        }
    )
    return direct_task_artifact or generated_artifact


def is_adapter_artifact_path(managed_path: str | PurePosixPath) -> bool:
    """Return whether ``managed_path`` is one exact supported adapter skill."""

    path = PurePosixPath(str(managed_path).replace("\\", "/"))
    return path in ADAPTER_ARTIFACT_PATHS


def parse_manifest_json(text: str) -> MainManifest:
    """Parse duplicate-aware JSON into a canonical immutable manifest model."""

    if not isinstance(text, str):
        raise ManifestValidationError("manifest JSON must be text")
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ManifestValidationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ManifestValidationError(f"manifest JSON is invalid: {exc}") from None

    top = _mapping(data, "manifest")
    if "manifest_schema_version" not in top:
        return _parse_v1(top)

    version = top["manifest_schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestValidationError("manifest manifest_schema_version must be integer 2")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError(f"unsupported manifest schema version: {version}")
    return _parse_v2(top)


def load_manifest_model(path: Path) -> MainManifest:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ManifestValidationError(f"manifest is not valid UTF-8: {exc}") from None
    return parse_manifest_json(text)


def load_manifest(path: Path) -> dict[str, Any]:
    """Load validated canonical manifest data for legacy mapping-based callers."""

    return manifest_to_data(load_manifest_model(path))


def render_manifest_json(document: MainManifest) -> str:
    """Render a canonical manifest deterministically with one final newline."""

    canonical = validate_manifest(document)
    if isinstance(canonical, ManifestV1):
        return json.dumps(
            manifest_to_data(canonical),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ) + "\n"
    return json.dumps(
        manifest_to_data(canonical),
        indent=2,
        sort_keys=False,
        ensure_ascii=False,
    ) + "\n"


def validate_manifest(document: MainManifest) -> MainManifest:
    if isinstance(document, ManifestV1):
        data = manifest_to_data(document)
        return _parse_v1(_mapping(data, "manifest"))
    if isinstance(document, ManifestV2):
        data = manifest_to_data(document)
        return _parse_v2(_mapping(data, "manifest"))
    raise ManifestValidationError("manifest must use a canonical ManifestV1 or ManifestV2 model")


def manifest_to_data(document: MainManifest) -> dict[str, Any]:
    managed_files = [_managed_file_to_data(record) for record in document.managed_files]
    integrity = {
        "manifest_self_hash": document.integrity.manifest_self_hash,
        "release_verification": document.integrity.release_verification,
        "note": document.integrity.note,
    }
    if isinstance(document, ManifestV1):
        return {
            "harness_version": document.harness_version,
            "template_version": document.template_version,
            "generated_at": document.generated_at,
            "managed_files": managed_files,
            "integrity": integrity,
        }
    return {
        "manifest_schema_version": document.manifest_schema_version,
        "harness_version": document.harness_version,
        "template_version": document.template_version,
        "generated_at": document.generated_at,
        "managed_files": managed_files,
        "generated_artifact_provenance": [
            _provenance_to_data(record) for record in document.generated_artifact_provenance
        ],
        "integrity": integrity,
    }


def build_manifest_model(
    root: Path,
    extra_managed_paths: Iterable[PurePosixPath] = (),
    *,
    schema_version: int = MANIFEST_SCHEMA_VERSION,
    generated_at: str | None = None,
) -> MainManifest:
    """Build a complete manifest model for the files currently managed."""

    if isinstance(schema_version, bool) or schema_version not in {1, MANIFEST_SCHEMA_VERSION}:
        raise ManifestValidationError(f"unsupported manifest schema version: {schema_version}")
    managed_paths = _canonical_managed_paths(
        (
            *MANAGED_FILE_PATHS,
            *_existing_manifest_optional_paths(root),
            *extra_managed_paths,
        )
    )
    managed_files = tuple(build_managed_file_record(root, path) for path in managed_paths)
    integrity = _default_integrity()
    timestamp = generated_at if generated_at is not None else _timestamp()
    if schema_version == 1:
        return validate_manifest(
            ManifestV1(
                harness_version=HARNESS_VERSION,
                template_version=TEMPLATE_VERSION,
                generated_at=timestamp,
                managed_files=managed_files,
                integrity=integrity,
            )
        )
    return validate_manifest(
        ManifestV2(
            harness_version=HARNESS_VERSION,
            template_version=TEMPLATE_VERSION,
            generated_at=timestamp,
            managed_files=managed_files,
            generated_artifact_provenance=(),
            integrity=integrity,
        )
    )


def build_manifest(root: Path, extra_managed_paths: Iterable[PurePosixPath] = ()) -> dict[str, Any]:
    """Build canonical schema-v2 manifest data for a new repository."""

    return manifest_to_data(build_manifest_model(root, extra_managed_paths))


def write_manifest(
    root: Path,
    extra_managed_paths: Iterable[PurePosixPath] = (),
    *,
    upgrade_to_v2: bool = False,
) -> None:
    """Write a manifest atomically without accidental legacy migration."""

    target = resolve_under_root(root, MANIFEST_PATH)
    requested_paths = tuple(extra_managed_paths)

    existing = load_manifest_model(target) if target.is_file() else None
    schema_version = (
        MANIFEST_SCHEMA_VERSION
        if existing is None or upgrade_to_v2
        else existing.schema_version
    )
    document = build_manifest_model(
        root,
        requested_paths,
        schema_version=schema_version,
    )
    if isinstance(existing, ManifestV2):
        if not isinstance(document, ManifestV2):
            raise ManifestValidationError("schema-v2 manifest cannot be rewritten as schema v1")
        document = validate_manifest(
            ManifestV2(
                harness_version=document.harness_version,
                template_version=document.template_version,
                generated_at=document.generated_at,
                managed_files=document.managed_files,
                generated_artifact_provenance=existing.generated_artifact_provenance,
                integrity=document.integrity,
            )
        )
    _write_manifest_model(target, document)


def replace_managed_file_records(
    document: MainManifest,
    replacements: Iterable[ManagedFileRecord],
    *,
    generated_at: str,
    upgrade_to_v2: bool = False,
) -> MainManifest:
    """Replace only named managed records while preserving every unrelated record."""

    canonical = validate_manifest(document)
    replacement_records = tuple(_validate_managed_file_model(record) for record in replacements)
    replacement_paths = [record.path for record in replacement_records]
    if len(replacement_paths) != len(set(replacement_paths)):
        raise ManifestValidationError("manifest managed_files contains duplicate replacement paths")

    by_path = {record.path: record for record in canonical.managed_files}
    for record in replacement_records:
        by_path[record.path] = record
    managed_files = tuple(sorted(by_path.values(), key=_managed_file_sort_key))

    if isinstance(canonical, ManifestV1) and not upgrade_to_v2:
        return validate_manifest(
            ManifestV1(
                harness_version=canonical.harness_version,
                template_version=canonical.template_version,
                generated_at=_string(generated_at, "manifest generated_at"),
                managed_files=managed_files,
                integrity=canonical.integrity,
            )
        )

    provenance = canonical.generated_artifact_provenance if isinstance(canonical, ManifestV2) else ()
    return validate_manifest(
        ManifestV2(
            harness_version=canonical.harness_version,
            template_version=canonical.template_version,
            generated_at=_string(generated_at, "manifest generated_at"),
            managed_files=managed_files,
            generated_artifact_provenance=provenance,
            integrity=canonical.integrity,
        )
    )


def remove_provenance_records(
    document: MainManifest,
    output_paths: Iterable[str],
    *,
    generated_at: str,
    upgrade_to_v2: bool = False,
) -> MainManifest:
    """Remove only named provenance records without filesystem inspection."""

    canonical = validate_manifest(document)
    requested = tuple(
        _repository_path(path, "provenance removal output path")
        for path in output_paths
    )
    if len(requested) != len(set(requested)):
        raise ManifestValidationError(
            "provenance removal contains duplicate output paths"
        )

    if isinstance(canonical, ManifestV1):
        if not upgrade_to_v2:
            return canonical
        return validate_manifest(
            ManifestV2(
                harness_version=canonical.harness_version,
                template_version=canonical.template_version,
                generated_at=_string(generated_at, "manifest generated_at"),
                managed_files=canonical.managed_files,
                generated_artifact_provenance=(),
                integrity=canonical.integrity,
            )
        )

    requested_set = set(requested)
    remaining = tuple(
        record
        for record in canonical.generated_artifact_provenance
        if record.output_path not in requested_set
    )
    if remaining == canonical.generated_artifact_provenance:
        return canonical
    return validate_manifest(
        ManifestV2(
            harness_version=canonical.harness_version,
            template_version=canonical.template_version,
            generated_at=_string(generated_at, "manifest generated_at"),
            managed_files=canonical.managed_files,
            generated_artifact_provenance=remaining,
            integrity=canonical.integrity,
        )
    )


def replace_managed_and_provenance_records(
    document: MainManifest,
    managed_replacements: Iterable[ManagedFileRecord],
    provenance_replacements: Iterable[GeneratedArtifactProvenance],
    *,
    generated_at: str,
    upgrade_to_v2: bool = False,
) -> MainManifest:
    """Replace one bounded, matching set of managed and provenance records."""

    canonical = validate_manifest(document)
    managed = tuple(
        _validate_managed_file_model(record)
        for record in managed_replacements
    )
    managed_paths = tuple(record.path for record in managed)
    if len(managed_paths) != len(set(managed_paths)):
        raise ManifestValidationError(
            "manifest managed_files contains duplicate replacement paths"
        )

    provenance_records: list[GeneratedArtifactProvenance] = []
    for record in provenance_replacements:
        if not isinstance(record, GeneratedArtifactProvenance):
            raise ManifestValidationError(
                "provenance replacement must use GeneratedArtifactProvenance"
            )
        provenance_records.append(
            _parse_provenance([_provenance_to_data(record)])[0]
        )
    provenance = tuple(provenance_records)
    provenance_paths = tuple(record.output_path for record in provenance)
    if len(provenance_paths) != len(set(provenance_paths)):
        raise ManifestValidationError(
            "manifest provenance contains duplicate replacement output paths"
        )
    if set(managed_paths) != set(provenance_paths):
        raise ManifestValidationError(
            "managed and provenance replacement output paths must match exactly"
        )
    if isinstance(canonical, ManifestV1) and provenance and not upgrade_to_v2:
        raise ManifestValidationError(
            "schema-v1 manifest requires an authorized upgrade for provenance"
        )

    managed_by_path = {record.path: record for record in canonical.managed_files}
    for record in managed:
        managed_by_path[record.path] = record
    merged_managed = tuple(
        sorted(managed_by_path.values(), key=_managed_file_sort_key)
    )

    existing_provenance = (
        canonical.generated_artifact_provenance
        if isinstance(canonical, ManifestV2)
        else ()
    )
    provenance_by_path = {
        record.output_path: record for record in existing_provenance
    }
    for record in provenance:
        provenance_by_path[record.output_path] = record
    merged_provenance = tuple(
        sorted(
            provenance_by_path.values(),
            key=lambda record: (
                record.task_slug,
                record.producer_command,
                record.output_path,
            ),
        )
    )

    if (
        isinstance(canonical, ManifestV2)
        and merged_managed == canonical.managed_files
        and merged_provenance == canonical.generated_artifact_provenance
    ):
        return canonical

    return validate_manifest(
        ManifestV2(
            harness_version=canonical.harness_version,
            template_version=canonical.template_version,
            generated_at=_string(generated_at, "manifest generated_at"),
            managed_files=merged_managed,
            generated_artifact_provenance=merged_provenance,
            integrity=canonical.integrity,
        )
    )


def persist_manifest_model(root: Path, document: MainManifest) -> None:
    """Atomically persist one already-bounded canonical manifest model."""

    target = resolve_under_root(root, MANIFEST_PATH)
    _write_manifest_model(target, validate_manifest(document))


def update_managed_file_records(
    root: Path,
    replacements: Iterable[ManagedFileRecord],
    *,
    generated_at: str | None = None,
    upgrade_to_v2: bool = False,
) -> None:
    """Atomically persist a bounded set of managed-record replacements."""

    target = resolve_under_root(root, MANIFEST_PATH)
    current = load_manifest_model(target)
    updated = replace_managed_file_records(
        current,
        replacements,
        generated_at=generated_at if generated_at is not None else _timestamp(),
        upgrade_to_v2=upgrade_to_v2,
    )
    _write_manifest_model(target, updated)


def _write_manifest_model(path: Path, document: MainManifest) -> None:
    content = render_manifest_json(document).encode("utf-8")
    write_bytes_atomic(path, content)


def _parse_v1(data: Mapping[str, Any]) -> ManifestV1:
    _require_exact_keys(data, _V1_FIELDS, "schema-v1 manifest")
    document = ManifestV1(
        harness_version=_string(data["harness_version"], "manifest harness_version"),
        template_version=_string(data["template_version"], "manifest template_version"),
        generated_at=_string(data["generated_at"], "manifest generated_at"),
        managed_files=_parse_managed_files(data["managed_files"]),
        integrity=_parse_integrity(data["integrity"]),
    )
    _validate_cross_record_invariants(document)
    return document


def _parse_v2(data: Mapping[str, Any]) -> ManifestV2:
    _require_exact_keys(data, _V2_FIELDS, "schema-v2 manifest")
    version = data["manifest_schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError("manifest manifest_schema_version must be integer 2")
    document = ManifestV2(
        harness_version=_string(data["harness_version"], "manifest harness_version"),
        template_version=_string(data["template_version"], "manifest template_version"),
        generated_at=_string(data["generated_at"], "manifest generated_at"),
        managed_files=_parse_managed_files(data["managed_files"]),
        generated_artifact_provenance=_parse_provenance(data["generated_artifact_provenance"]),
        integrity=_parse_integrity(data["integrity"]),
    )
    _validate_cross_record_invariants(document)
    return document


def _parse_managed_files(value: Any) -> tuple[ManagedFileRecord, ...]:
    values = _list(value, "manifest managed_files")
    records: list[ManagedFileRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        label = f"manifest managed_files[{index}]"
        data = _mapping(item, label)
        _require_exact_keys(data, _MANAGED_FILE_FIELDS, label)
        path_value = data["path"]
        try:
            path = _repository_path(path_value, f"{label} path")
        except ManifestValidationError as exc:
            if isinstance(path_value, str) and path_value.startswith(".harness/tasks/"):
                raise ManifestValidationError(f"unsafe managed task path {path_value}: {exc}") from None
            raise
        record = _validate_managed_file_model(
            ManagedFileRecord(
                path=path,
                protected=_boolean(data["protected"], f"{label} protected"),
                hash_algorithm=_nullable_string(data["hash_algorithm"], f"{label} hash_algorithm"),
                sha256=_nullable_string(data["sha256"], f"{label} sha256"),
            )
        )
        if record.path in seen:
            raise ManifestValidationError(f"manifest managed_files contains duplicate path: {record.path}")
        seen.add(record.path)
        records.append(record)
    return tuple(sorted(records, key=_managed_file_sort_key))


def _validate_managed_file_model(record: ManagedFileRecord) -> ManagedFileRecord:
    if not isinstance(record, ManagedFileRecord):
        raise ManifestValidationError("managed-file replacement must use ManagedFileRecord")
    path = _repository_path(record.path, "managed-file path")
    protected = _boolean(record.protected, f"managed file {path} protected")
    algorithm = _nullable_string(record.hash_algorithm, f"managed file {path} hash_algorithm")
    digest = _nullable_string(record.sha256, f"managed file {path} sha256")

    if path == MANIFEST_PATH.as_posix():
        if not protected or algorithm is not None or digest is not None:
            raise ManifestValidationError(
                "manifest self record must be protected with null hash_algorithm and null sha256"
            )
        return ManagedFileRecord(path, protected, algorithm, digest)

    if protected:
        if algorithm != "sha256":
            raise ManifestValidationError(
                f"protected managed file {path} must use sha256"
            )
    elif algorithm is not None or digest is not None:
        raise ManifestValidationError(
            f"unprotected managed file {path} must have null hash_algorithm and null sha256"
        )
    if algorithm not in {None, "sha256"}:
        raise ManifestValidationError(f"managed file {path} uses unsupported hash_algorithm: {algorithm}")
    if digest is not None:
        _sha256(digest, f"managed file {path} sha256")
    return ManagedFileRecord(path, protected, algorithm, digest)


def _parse_integrity(value: Any) -> IntegrityRecord:
    data = _mapping(value, "manifest integrity")
    _require_exact_keys(data, _INTEGRITY_FIELDS, "manifest integrity")
    record = IntegrityRecord(
        manifest_self_hash=_string(data["manifest_self_hash"], "manifest integrity manifest_self_hash"),
        release_verification=_string(
            data["release_verification"],
            "manifest integrity release_verification",
        ),
        note=_string(data["note"], "manifest integrity note"),
    )
    if record.manifest_self_hash != "excluded-in-phase-1":
        raise ManifestValidationError("manifest integrity manifest_self_hash must be excluded-in-phase-1")
    if record.release_verification != "deferred":
        raise ManifestValidationError("manifest integrity release_verification must be deferred")
    if record.note != FUTURE_INTEGRITY_NOTE:
        raise ManifestValidationError("manifest integrity note does not match the supported contract")
    return record


def _parse_provenance(value: Any) -> tuple[GeneratedArtifactProvenance, ...]:
    values = _list(value, "manifest generated_artifact_provenance")
    records: list[GeneratedArtifactProvenance] = []
    seen_outputs: set[str] = set()
    for index, item in enumerate(values):
        label = f"manifest generated_artifact_provenance[{index}]"
        data = _mapping(item, label)
        _require_exact_keys(data, _PROVENANCE_FIELDS, label)
        output_path = _repository_path(data["output_path"], f"{label} output_path")
        if output_path in seen_outputs:
            raise ManifestValidationError(
                f"manifest generated_artifact_provenance contains duplicate output_path: {output_path}"
            )
        seen_outputs.add(output_path)
        producer = _string(data["producer_command"], f"{label} producer_command")
        producer_version = _positive_integer(data["producer_version"], f"{label} producer_version")
        task_slug = _string(data["task_slug"], f"{label} task_slug")
        if TASK_SLUG_RE.fullmatch(task_slug) is None:
            raise ManifestValidationError(f"{label} task_slug is unsafe: {task_slug}")
        records.append(
            GeneratedArtifactProvenance(
                output_path=output_path,
                producer_command=producer,
                producer_version=producer_version,
                task_slug=task_slug,
                output_sha256=_sha256(data["output_sha256"], f"{label} output_sha256"),
                dependencies=_parse_dependencies(data["dependencies"], label),
                repository_observations=_parse_observations(
                    data["repository_observations"],
                    label,
                ),
            )
        )
    return tuple(
        sorted(
            records,
            key=lambda record: (record.task_slug, record.producer_command, record.output_path),
        )
    )


def _parse_dependencies(value: Any, parent_label: str) -> tuple[ProvenanceDependency, ...]:
    values = _list(value, f"{parent_label} dependencies")
    records: list[ProvenanceDependency] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        label = f"{parent_label} dependencies[{index}]"
        data = _mapping(item, label)
        _require_exact_keys(data, _DEPENDENCY_FIELDS, label)
        path = _repository_path(data["dependency_path"], f"{label} dependency_path")
        if path in seen:
            raise ManifestValidationError(f"{parent_label} contains duplicate dependency_path: {path}")
        seen.add(path)
        state = _string(data["dependency_state"], f"{label} dependency_state")
        if state not in DEPENDENCY_STATES:
            raise ManifestValidationError(f"{label} has unsupported dependency_state: {state}")
        digest = _nullable_string(data["dependency_sha256"], f"{label} dependency_sha256")
        if state == "present":
            if digest is None:
                raise ManifestValidationError(f"{label} present dependency requires dependency_sha256")
            _sha256(digest, f"{label} dependency_sha256")
        elif digest is not None:
            raise ManifestValidationError(f"{label} non-present dependency must have null dependency_sha256")
        records.append(ProvenanceDependency(path, state, digest))
    return tuple(sorted(records, key=lambda record: record.dependency_path))


def _parse_observations(value: Any, parent_label: str) -> tuple[ProvenanceObservation, ...]:
    values = _list(value, f"{parent_label} repository_observations")
    records: list[ProvenanceObservation] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(values):
        label = f"{parent_label} repository_observations[{index}]"
        data = _mapping(item, label)
        _require_exact_keys(data, _OBSERVATION_FIELDS, label)
        path = _repository_path(data["observation_path"], f"{label} observation_path")
        predicate = _string(data["predicate"], f"{label} predicate")
        if predicate not in OBSERVATION_PREDICATES:
            raise ManifestValidationError(f"{label} has unsupported predicate: {predicate}")
        identity = (path, predicate)
        if identity in seen:
            raise ManifestValidationError(
                f"{parent_label} contains duplicate repository observation: {path} {predicate}"
            )
        seen.add(identity)
        records.append(
            ProvenanceObservation(
                observation_path=path,
                predicate=predicate,
                result=_boolean(data["result"], f"{label} result"),
            )
        )
    return tuple(sorted(records, key=lambda record: (record.observation_path, record.predicate)))


def _validate_cross_record_invariants(document: MainManifest) -> None:
    paths = {record.path for record in document.managed_files}
    if MANIFEST_PATH.as_posix() not in paths:
        raise ManifestValidationError("manifest managed_files must include .harness/manifest.json")
    if not isinstance(document, ManifestV2):
        return
    managed_by_path = {record.path: record for record in document.managed_files}
    for provenance in document.generated_artifact_provenance:
        managed = managed_by_path.get(provenance.output_path)
        if managed is None or not managed.protected:
            raise ManifestValidationError(
                f"provenance output must have a protected managed-file record: {provenance.output_path}"
            )
        if managed.sha256 != provenance.output_sha256:
            raise ManifestValidationError(
                f"provenance output hash contradicts managed-file hash: {provenance.output_path}"
            )


def build_managed_file_record(
    root: Path,
    managed_path: str | PurePosixPath,
) -> ManagedFileRecord:
    """Build one validated record from an eligible persisted managed path."""

    raw_path = managed_path.as_posix() if isinstance(managed_path, PurePosixPath) else managed_path
    path_text = _repository_path(raw_path, "managed path")
    known_base_paths = {path.as_posix() for path in MANAGED_FILE_PATHS}
    if (
        path_text not in known_base_paths
        and not is_task_artifact_path(path_text)
        and not is_adapter_artifact_path(path_text)
    ):
        raise ManifestValidationError(f"path is not eligible for manifest management: {path_text}")
    target = resolve_under_root(root, path_text)
    hashed_paths = {path.as_posix() for path in HASHED_MANAGED_FILE_PATHS}
    protected_paths = {path.as_posix() for path in PROTECTED_MANAGED_FILE_PATHS}
    optional_protected = is_task_artifact_path(path_text) or is_adapter_artifact_path(path_text)
    should_hash = path_text in hashed_paths or optional_protected
    protected = path_text in protected_paths or optional_protected
    digest = sha256_file(target) if should_hash and target.is_file() else None
    record = ManagedFileRecord(
        path=path_text,
        protected=protected,
        hash_algorithm="sha256" if should_hash else None,
        sha256=digest,
    )
    return _validate_managed_file_model(record)


def _existing_manifest_optional_paths(root: Path) -> tuple[PurePosixPath, ...]:
    target = resolve_under_root(root, MANIFEST_PATH)
    if not target.is_file():
        return ()
    try:
        existing = load_manifest_model(target)
    except (OSError, ManifestValidationError):
        return ()
    return tuple(
        PurePosixPath(record.path)
        for record in existing.managed_files
        if is_task_artifact_path(record.path) or is_adapter_artifact_path(record.path)
    )


def _canonical_managed_paths(paths: Iterable[PurePosixPath]) -> tuple[PurePosixPath, ...]:
    by_text: dict[str, PurePosixPath] = {}
    for path in paths:
        text = _repository_path(path.as_posix(), "managed path")
        by_text[text] = PurePosixPath(text)
    return tuple(sorted(by_text.values(), key=lambda path: _managed_path_sort_key(path.as_posix())))


def _managed_file_sort_key(record: ManagedFileRecord) -> tuple[int, int | str]:
    return _managed_path_sort_key(record.path)


def _managed_path_sort_key(path: str) -> tuple[int, int | str]:
    if path in _BASE_PATH_ORDER:
        return (0, _BASE_PATH_ORDER[path])
    return (1, path)


def _managed_file_to_data(record: ManagedFileRecord) -> dict[str, Any]:
    return {
        "path": record.path,
        "protected": record.protected,
        "hash_algorithm": record.hash_algorithm,
        "sha256": record.sha256,
    }


def _provenance_to_data(record: GeneratedArtifactProvenance) -> dict[str, Any]:
    return {
        "output_path": record.output_path,
        "producer_command": record.producer_command,
        "producer_version": record.producer_version,
        "task_slug": record.task_slug,
        "output_sha256": record.output_sha256,
        "dependencies": [
            {
                "dependency_path": dependency.dependency_path,
                "dependency_state": dependency.dependency_state,
                "dependency_sha256": dependency.dependency_sha256,
            }
            for dependency in record.dependencies
        ],
        "repository_observations": [
            {
                "observation_path": observation.observation_path,
                "predicate": observation.predicate,
                "result": observation.result,
            }
            for observation in record.repository_observations
        ],
    }


def _default_integrity() -> IntegrityRecord:
    return IntegrityRecord(
        manifest_self_hash="excluded-in-phase-1",
        release_verification="deferred",
        note=FUTURE_INTEGRITY_NOTE,
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"manifest JSON contains duplicate key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{label} must be a JSON object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{label} must be a JSON array")
    return value


def _require_exact_keys(data: Mapping[str, Any], required: frozenset[str], label: str) -> None:
    actual = set(data)
    missing = sorted(required - actual)
    unknown = sorted(actual - required)
    if missing:
        raise ManifestValidationError(f"{label} is missing required field(s): {', '.join(missing)}")
    if unknown:
        raise ManifestValidationError(f"{label} contains unknown field(s): {', '.join(unknown)}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{label} must be a non-empty string")
    return value


def _nullable_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestValidationError(f"{label} must be a Boolean")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestValidationError(f"{label} must be a positive integer")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ManifestValidationError(f"{label} must be a lowercase 64-character SHA-256")
    return value


def _repository_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{label} must be a non-empty repository-relative path")
    if "\x00" in value:
        raise ManifestValidationError(f"{label} contains an unsafe NUL character")
    if "\\" in value:
        raise ManifestValidationError(f"{label} must use POSIX separators, not backslashes")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ManifestValidationError(f"{label} must be repository-relative: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestValidationError(f"{label} contains an unsafe or non-normalized segment: {value}")
    if posix_path.as_posix() != value:
        raise ManifestValidationError(f"{label} must already be normalized: {value}")
    return value
