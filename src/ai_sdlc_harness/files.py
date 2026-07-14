"""Safe path and file helpers."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


class PathSafetyError(ValueError):
    """Raised when a managed path would escape its allowed root."""


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
