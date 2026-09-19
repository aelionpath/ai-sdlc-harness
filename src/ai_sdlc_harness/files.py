"""Safe path and file helpers."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class PathSafetyError(ValueError):
    """Raised when a managed path would escape its allowed root."""


class ExactBytePersistenceError(RuntimeError):
    """Raised when persisted bytes do not match the intended exact content."""


@dataclass(frozen=True)
class PersistedFileResult:
    """Result of persisting one exact byte sequence."""

    changed: bool
    sha256: str


def normalize_managed_path(relative_path: str | PurePosixPath) -> PurePosixPath:
    """Return a normalized safe relative managed path.

    Managed output paths must be relative POSIX-style paths. Absolute paths and
    traversal segments are rejected before touching the filesystem.
    """

    path_text = str(relative_path).replace("\\", "/")
    candidate = PurePosixPath(path_text)
    windows_candidate = PureWindowsPath(str(relative_path))
    if candidate.is_absolute() or windows_candidate.is_absolute() or windows_candidate.drive:
        raise PathSafetyError(f"managed path must be relative: {relative_path}")
    if not path_text or path_text in {".", ""}:
        raise PathSafetyError("managed path must not be empty")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise PathSafetyError(f"managed path contains unsafe segment: {relative_path}")
    return candidate


def resolve_under_root(root: Path, relative_path: str | PurePosixPath) -> Path:
    """Resolve a safe relative path under ``root``."""

    managed_path = normalize_managed_path(relative_path)
    root_resolved = root.resolve()
    target = (root_resolved / Path(*managed_path.parts)).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise PathSafetyError(f"managed path escapes project root: {relative_path}")
    return target


def resolve_managed_output_under_root(
    root: Path,
    relative_path: str | PurePosixPath,
) -> Path:
    """Resolve a managed output path without accepting symlink indirection."""

    managed_path = normalize_managed_path(relative_path)
    root_resolved = root.resolve()
    target = root_resolved.joinpath(*managed_path.parts)

    current = root_resolved
    for part in managed_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            parent_path = current.relative_to(root_resolved).as_posix()
            raise PathSafetyError(
                f"managed output path parent must not be a symlink: {parent_path}"
            )

    if target.is_symlink():
        raise PathSafetyError(
            f"managed output path must not be a symlink: {managed_path.as_posix()}"
        )

    resolved_target = target.resolve()
    if (
        resolved_target != root_resolved
        and root_resolved not in resolved_target.parents
    ):
        raise PathSafetyError(
            f"managed output path escapes project root: {managed_path.as_posix()}"
        )
    return target


def relative_display_path(path: Path, root: Path) -> str:
    """Format a path relative to root with POSIX separators when possible."""

    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def sha256_bytes(content: bytes) -> str:
    """Return the SHA-256 digest of ``content`` without normalization."""

    if not isinstance(content, bytes):
        raise TypeError("SHA-256 content must be bytes")
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of the exact bytes stored at ``path``."""

    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Atomically replace ``path`` with exact bytes using a temporary sibling."""

    if not isinstance(content, bytes):
        raise TypeError("atomic file content must be bytes")

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def persist_exact_bytes(path: Path, content: bytes) -> PersistedFileResult:
    """Persist one exact byte sequence and confirm the resulting content.

    The caller is responsible for resolving a safe repository-contained path
    before calling this helper.
    """

    if not isinstance(content, bytes):
        raise TypeError("exact file content must be bytes")

    intended_sha256 = sha256_bytes(content)
    try:
        existing_content = path.read_bytes()
    except FileNotFoundError:
        existing_content = None

    if existing_content == content:
        return PersistedFileResult(changed=False, sha256=sha256_bytes(existing_content))

    write_bytes_atomic(path, content)
    try:
        persisted_content = path.read_bytes()
    except OSError as exc:
        raise ExactBytePersistenceError(
            f"could not confirm exact persisted bytes for {path}: {exc}"
        ) from exc

    persisted_sha256 = sha256_bytes(persisted_content)
    if persisted_content != content or persisted_sha256 != intended_sha256:
        raise ExactBytePersistenceError(
            f"persisted bytes for {path} do not match the intended content"
        )
    return PersistedFileResult(changed=True, sha256=persisted_sha256)
