"""Implementation of ``ai-sdlc task start``."""

from __future__ import annotations

import re
import sysconfig
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .constants import TASK_ARTIFACT_FILENAMES, TASK_SLUG_MAX_LENGTH, TASK_SLUG_PATTERN
from .files import PathSafetyError, resolve_under_root, write_text
from .manifest import is_task_artifact_path, load_manifest, write_manifest


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)
TASK_TEMPLATE_PLACEHOLDER_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
TASK_TEMPLATE_DIR: Path | None = None
TASK_TEMPLATE_PACKAGE_DATA_PATH = (
    Path("share") / "ai-sdlc-harness" / "templates" / "task"
)


class TaskTitleError(ValueError):
    """Raised when a task title cannot be turned into a safe task slug."""


class TaskTemplateError(RuntimeError):
    """Raised when a task artifact template cannot be loaded or rendered."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_path_like_title(title: str) -> bool:
    if "\x00" in title:
        return True

    stripped = title.strip()
    windows_candidate = PureWindowsPath(stripped)
    posix_text = stripped.replace("\\", "/")
    posix_candidate = PurePosixPath(posix_text)
    if windows_candidate.drive or windows_candidate.is_absolute() or posix_candidate.is_absolute():
        return True

    return any(part == ".." for part in posix_candidate.parts)


def task_slug_from_title(title: str) -> str:
    if not title or not title.strip():
        raise TaskTitleError("Task title must not be empty.")
    if _is_path_like_title(title):
        raise TaskTitleError("Task title looks like an unsafe path.")

    normalized = unicodedata.normalize("NFKD", title)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text)
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = slug[:TASK_SLUG_MAX_LENGTH].rstrip("-")

    if not slug:
        raise TaskTitleError("Task title does not produce a safe slug.")
    if TASK_SLUG_RE.fullmatch(slug) is None:
        raise TaskTitleError("Task title produces an unsafe slug.")
    return slug


def _task_path(slug: str, filename: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug / filename


def _managed_task_paths_from_manifest(root: Path, slug: str) -> set[str]:
    manifest_path = resolve_under_root(root, ".harness/manifest.json")
    manifest = load_manifest(manifest_path)
    entries = manifest.get("managed_files")
    if not isinstance(entries, list):
        return set()

    managed_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path_text = str(entry.get("path", ""))
        if is_task_artifact_path(path_text) and PurePosixPath(path_text).parts[2] == slug:
            managed_paths.add(path_text)
    return managed_paths


def _source_tree_template_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "templates" / "task"


def _installed_template_dir() -> Path:
    data_path = sysconfig.get_path("data")
    if not data_path:
        raise TaskTemplateError("Could not resolve installed task template data directory.")
    return Path(data_path) / TASK_TEMPLATE_PACKAGE_DATA_PATH


def _task_template_dir() -> Path:
    if TASK_TEMPLATE_DIR is not None:
        return TASK_TEMPLATE_DIR

    source_tree_dir = _source_tree_template_dir()
    if source_tree_dir.is_dir():
        return source_tree_dir

    installed_dir = _installed_template_dir()
    if installed_dir.is_dir():
        return installed_dir

    raise TaskTemplateError(
        "Could not find task templates. Checked "
        f"{source_tree_dir} and {installed_dir}."
    )


def _task_template_text(filename: str) -> str:
    template_path = _task_template_dir() / filename
    if not template_path.is_file():
        raise TaskTemplateError(f"Missing task template file: {template_path}")
    return template_path.read_text(encoding="utf-8")


def _artifact_content(filename: str, *, title: str, slug: str, created_at: str) -> str:
    if filename not in TASK_ARTIFACT_FILENAMES:
        raise ValueError(f"Unknown task artifact template: {filename}")

    values = {
        "task_title": title,
        "task_slug": slug,
        "created_at": created_at,
    }
    content = _task_template_text(filename)
    unknown_placeholders = sorted(
        {
            placeholder.strip()
            for placeholder in TASK_TEMPLATE_PLACEHOLDER_RE.findall(content)
            if placeholder.strip() not in values
        }
    )
    if unknown_placeholders:
        placeholders = ", ".join("{{ " + placeholder + " }}" for placeholder in unknown_placeholders)
        raise TaskTemplateError(f"Unknown task template placeholder in {filename}: {placeholders}")

    def replace_placeholder(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name in values:
            return values[name]
        return match.group(0)

    return TASK_TEMPLATE_PLACEHOLDER_RE.sub(replace_placeholder, content)


def _render_artifact_contents(filenames: list[str], *, title: str, slug: str, created_at: str) -> dict[str, str]:
    contents: dict[str, str] = {}
    for filename in filenames:
        if filename not in contents:
            contents[filename] = _artifact_content(filename, title=title, slug=slug, created_at=created_at)
    return contents


def _filenames_to_render(
    root: Path,
    expected_paths: list[PurePosixPath],
    managed_for_task: set[str],
    *,
    force: bool,
) -> list[str]:
    filenames: list[str] = []
    for managed_path in expected_paths:
        target = resolve_under_root(root, managed_path)
        path_text = managed_path.as_posix()
        if target.exists():
            if force and path_text in managed_for_task:
                filenames.append(managed_path.name)
            continue
        filenames.append(managed_path.name)
    return filenames


def start_task(root: Path, title: str, *, dry_run: bool = False, force: bool = False) -> tuple[int, list[str]]:
    harness_root = root / ".harness"
    if not harness_root.is_dir() or not (harness_root / "config.yaml").is_file() or not (harness_root / "manifest.json").is_file():
        return 1, ["AI SDLC Harness is not initialized. Run `ai-sdlc init` from the project root."]

    try:
        slug = task_slug_from_title(title)
    except TaskTitleError as exc:
        return 2, [str(exc)]

    try:
        managed_for_task = _managed_task_paths_from_manifest(root, slug)
    except Exception as exc:
        return 1, [f"Could not read .harness/manifest.json: {exc}"]

    task_dir = PurePosixPath(".harness") / "tasks" / slug
    task_target = resolve_under_root(root, task_dir)
    expected_paths = [_task_path(slug, filename) for filename in TASK_ARTIFACT_FILENAMES]
    blocked: list[str] = []
    for managed_path in expected_paths:
        target = resolve_under_root(root, managed_path)
        if target.exists() and managed_path.as_posix() not in managed_for_task:
            blocked.append(managed_path.as_posix())

    messages: list[str] = [f"task slug: {slug}"]
    if blocked:
        messages.extend(f"unmanaged existing file {path}" for path in blocked)
        messages.append("refusing to overwrite unmanaged task files")
        return 1, messages

    created_at = _timestamp()
    try:
        artifact_contents = _render_artifact_contents(
            _filenames_to_render(root, expected_paths, managed_for_task, force=force),
            title=title,
            slug=slug,
            created_at=created_at,
        )
    except TaskTemplateError as exc:
        messages.append(str(exc))
        return 1, messages

    messages.append(f"{'would create' if dry_run and not task_target.exists() else 'create' if not task_target.exists() else 'skip existing'} directory {task_dir.as_posix()}")
    created_or_rewritten: list[PurePosixPath] = []
    if not dry_run:
        task_target.mkdir(parents=True, exist_ok=True)

    for managed_path in expected_paths:
        target = resolve_under_root(root, managed_path)
        filename = managed_path.name
        path_text = managed_path.as_posix()
        if target.exists():
            if force and path_text in managed_for_task:
                messages.append(f"{'would rewrite' if dry_run else 'rewrite'} file {path_text}")
                if not dry_run:
                    write_text(target, artifact_contents[filename])
                    created_or_rewritten.append(managed_path)
            else:
                messages.append(f"skip existing file {path_text}")
            continue

        messages.append(f"{'would create' if dry_run else 'create'} file {path_text}")
        if not dry_run:
            try:
                write_text(target, artifact_contents[filename])
            except PathSafetyError as exc:
                return 1, [f"Unsafe task path: {exc}"]
            created_or_rewritten.append(managed_path)

    if dry_run:
        messages.append("dry run; no files written")
    elif created_or_rewritten:
        write_manifest(root, extra_managed_paths=created_or_rewritten)
        messages.append("refreshed manifest .harness/manifest.json")
    else:
        messages.append("skip existing manifest .harness/manifest.json")

    messages.append("root AGENTS.md and CLAUDE.md were not modified")
    return 0, messages
