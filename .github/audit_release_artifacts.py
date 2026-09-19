"""Fail-closed audit for AI SDLC Harness wheel and sdist artifacts."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import email
import hashlib
import io
import os
import re
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Mapping, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    tomllib = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKSUM_FILENAME = "SHA256SUMS"
EXPECTED_NORMALIZED_NAME = "ai-sdlc-harness"
EXPECTED_RELEASE_VERSION = "1.0.0"
MAX_MEMBER_SIZE = 20 * 1024 * 1024
MAX_ARCHIVE_SIZE = 100 * 1024 * 1024

RUNTIME_MODULES = (
    "__init__.py",
    "__main__.py",
    "adapters.py",
    "cli.py",
    "constants.py",
    "context.py",
    "detect.py",
    "evidence.py",
    "files.py",
    "generate.py",
    "init.py",
    "lineage.py",
    "manifest.py",
    "preflight.py",
    "redact.py",
    "requirements.py",
    "spec.py",
    "status.py",
    "task.py",
    "task_metadata.py",
    "test_contract.py",
    "validate.py",
    "validation.py",
    "verify.py",
    "workflow.py",
)

TASK_TEMPLATES = (
    "acceptance.md",
    "architecture-notes.md",
    "coupling-notes.md",
    "evidence.md",
    "task.md",
    "test-contract.md",
    "verification.md",
)

FORBIDDEN_ROOTS = frozenset(
    {
        ".agents",
        ".codex",
        ".git",
        ".github",
        ".harness",
        ".pytest_cache",
        ".test-workspaces",
        ".venv",
        "build",
        "dist",
        "docs",
        "examples",
        "packs",
        "planning",
        "private-notes",
        "prompt-transcripts",
        "release",
        "research",
        "scratch",
        "scripts",
        "tests",
    }
)


class ArtifactAuditError(RuntimeError):
    """Raised when a release artifact violates the approved contract."""


@dataclass(frozen=True)
class ProjectContract:
    name: str
    version: str
    requires_python: str
    license_expression: str
    dependencies: tuple[str, ...]
    optional_dependencies: Mapping[str, tuple[str, ...]]
    urls: Mapping[str, str]
    scripts: Mapping[str, str]

    @property
    def filename_stem(self) -> str:
        return re.sub(r"[-.]+", "_", self.name)

    @property
    def wheel_filename(self) -> str:
        return f"{self.filename_stem}-{self.version}-py3-none-any.whl"

    @property
    def sdist_filename(self) -> str:
        return f"{self.filename_stem}-{self.version}.tar.gz"

    @property
    def dist_info(self) -> str:
        return f"{self.filename_stem}-{self.version}.dist-info"

    @property
    def sdist_root(self) -> str:
        return f"{self.filename_stem}-{self.version}"


@dataclass(frozen=True)
class AuditedArtifact:
    filename: str
    size: int
    sha256: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class AuditReport:
    wheel: AuditedArtifact
    sdist: AuditedArtifact
    checksum_path: Path


def normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _load_toml(path: Path) -> dict[str, object]:
    if tomllib is None:  # pragma: no cover - exercised on Python 3.10
        return _load_project_toml_subset(path)
    try:
        with path.open("rb") as file_obj:
            return tomllib.load(file_obj)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ArtifactAuditError(f"cannot read project metadata from {path}: {exc}") from exc


def _load_project_toml_subset(path: Path) -> dict[str, object]:
    """Parse the controlled project tables needed by the auditor on Python 3.10."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ArtifactAuditError(f"cannot read project metadata from {path}: {exc}") from exc

    project: dict[str, object] = {}
    optional: dict[str, object] = {}
    urls: dict[str, object] = {}
    scripts: dict[str, object] = {}
    section = ""
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if value.startswith("["):
            balance = value.count("[") - value.count("]")
            while balance > 0 and index < len(lines):
                continuation = lines[index].strip()
                index += 1
                value += "\n" + continuation
                balance += continuation.count("[") - continuation.count("]")

        destination: dict[str, object] | None = None
        if section == "project" and key in {
            "name",
            "version",
            "requires-python",
            "license",
            "dependencies",
        }:
            destination = project
        elif section == "project.optional-dependencies":
            destination = optional
        elif section == "project.urls":
            destination = urls
        elif section == "project.scripts":
            destination = scripts
        if destination is None:
            continue
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ArtifactAuditError(f"unsupported project metadata syntax for {key!r}") from exc
        destination[key] = parsed

    project["optional-dependencies"] = optional
    project["urls"] = urls
    project["scripts"] = scripts
    return {"project": project}


def load_project_contract(project_root: Path = PROJECT_ROOT) -> ProjectContract:
    data = _load_toml(project_root / "pyproject.toml")
    project = data.get("project")
    if not isinstance(project, dict):
        raise ArtifactAuditError("pyproject.toml must contain a [project] table")

    def required_string(key: str) -> str:
        value = project.get(key)
        if not isinstance(value, str) or not value:
            raise ArtifactAuditError(f"project metadata {key!r} must be a non-empty string")
        return value

    name = required_string("name")
    version = required_string("version")
    requires_python = required_string("requires-python")
    license_expression = required_string("license")
    if normalize_distribution_name(name) != EXPECTED_NORMALIZED_NAME:
        raise ArtifactAuditError(f"unexpected project name in pyproject.toml: {name!r}")
    if version != EXPECTED_RELEASE_VERSION:
        raise ArtifactAuditError(f"unexpected release version in pyproject.toml: {version!r}")

    dependencies = project.get("dependencies")
    optional = project.get("optional-dependencies")
    urls = project.get("urls")
    scripts = project.get("scripts")
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise ArtifactAuditError("project dependencies must be a string list")
    if not isinstance(optional, dict) or not all(
        isinstance(key, str)
        and isinstance(value, list)
        and all(isinstance(item, str) for item in value)
        for key, value in optional.items()
    ):
        raise ArtifactAuditError("project optional dependencies must be string lists")
    if not isinstance(urls, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in urls.items()):
        raise ArtifactAuditError("project URLs must be strings")
    if not isinstance(scripts, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in scripts.items()):
        raise ArtifactAuditError("project scripts must be strings")

    return ProjectContract(
        name=name,
        version=version,
        requires_python=requires_python,
        license_expression=license_expression,
        dependencies=tuple(dependencies),
        optional_dependencies={key: tuple(value) for key, value in optional.items()},
        urls=dict(urls),
        scripts=dict(scripts),
    )


def _safe_archive_path(name: str, *, archive: str) -> PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise ArtifactAuditError(f"{archive} contains an unsafe member path: {name!r}")
    path = PurePosixPath(name.rstrip("/"))
    windows_path = PureWindowsPath(name)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ArtifactAuditError(f"{archive} contains an absolute member path: {name!r}")
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ArtifactAuditError(f"{archive} contains path traversal: {name!r}")
    return path


def _expected_wheel_files(contract: ProjectContract) -> frozenset[str]:
    package_files = {f"ai_sdlc_harness/{name}" for name in RUNTIME_MODULES}
    template_root = f"{contract.filename_stem}-{contract.version}.data/data/share/ai-sdlc-harness/templates/task"
    template_files = {f"{template_root}/{name}" for name in TASK_TEMPLATES}
    metadata_files = {
        f"{contract.dist_info}/METADATA",
        f"{contract.dist_info}/WHEEL",
        f"{contract.dist_info}/entry_points.txt",
        f"{contract.dist_info}/top_level.txt",
        f"{contract.dist_info}/RECORD",
        f"{contract.dist_info}/licenses/LICENSE",
        f"{contract.dist_info}/licenses/NOTICE",
    }
    return frozenset(package_files | template_files | metadata_files)


def _expected_sdist_files(contract: ProjectContract) -> frozenset[str]:
    root = contract.sdist_root
    files = {
        f"{root}/LICENSE",
        f"{root}/MANIFEST.in",
        f"{root}/NOTICE",
        f"{root}/PKG-INFO",
        f"{root}/README.md",
        f"{root}/pyproject.toml",
        f"{root}/setup.cfg",
    }
    files.update(f"{root}/src/ai_sdlc_harness/{name}" for name in RUNTIME_MODULES)
    egg_info = f"{root}/src/{contract.filename_stem}.egg-info"
    files.update(
        {
            f"{egg_info}/PKG-INFO",
            f"{egg_info}/SOURCES.txt",
            f"{egg_info}/dependency_links.txt",
            f"{egg_info}/entry_points.txt",
            f"{egg_info}/requires.txt",
            f"{egg_info}/top_level.txt",
        }
    )
    files.update(f"{root}/templates/task/{name}" for name in TASK_TEMPLATES)
    return frozenset(files)


def _expected_parent_directories(files: Iterable[str]) -> frozenset[str]:
    directories: set[str] = set()
    for filename in files:
        parent = PurePosixPath(filename).parent
        while parent.parts:
            directories.add(parent.as_posix())
            parent = parent.parent
    return frozenset(directories)


def _validate_metadata(metadata_bytes: bytes, contract: ProjectContract, *, source: str) -> None:
    message = email.message_from_bytes(metadata_bytes)

    def exactly_one(header: str) -> str:
        values = message.get_all(header, [])
        if len(values) != 1:
            raise ArtifactAuditError(f"{source} must contain exactly one {header} header")
        return values[0]

    name = exactly_one("Name")
    if normalize_distribution_name(name) != EXPECTED_NORMALIZED_NAME:
        raise ArtifactAuditError(f"{source} has incorrect package name: {name!r}")
    if exactly_one("Version") != contract.version:
        raise ArtifactAuditError(f"{source} has incorrect package version")
    if exactly_one("Requires-Python") != contract.requires_python:
        raise ArtifactAuditError(f"{source} has incorrect Requires-Python")
    if exactly_one("License-Expression") != contract.license_expression:
        raise ArtifactAuditError(f"{source} has incorrect License-Expression")
    if set(message.get_all("License-File", [])) != {"LICENSE", "NOTICE"}:
        raise ArtifactAuditError(f"{source} must name exactly LICENSE and NOTICE as license files")

    actual_urls: dict[str, str] = {}
    for value in message.get_all("Project-URL", []):
        label, separator, url = value.partition(",")
        if not separator or label.strip() in actual_urls:
            raise ArtifactAuditError(f"{source} contains malformed or duplicate Project-URL metadata")
        actual_urls[label.strip()] = url.strip()
    if actual_urls != dict(contract.urls):
        raise ArtifactAuditError(f"{source} project URLs do not match pyproject.toml")

    runtime_requirements: dict[str, tuple[str, ...]] = {}
    extra_requirements: dict[str, tuple[str, ...]] = {}
    for requirement in message.get_all("Requires-Dist", []):
        requirement_text, separator, marker = requirement.partition(";")
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement_text)
        if match is None:
            raise ArtifactAuditError(f"{source} contains malformed Requires-Dist: {requirement!r}")
        dependency_name = normalize_distribution_name(match.group(1))
        specifiers = tuple(
            sorted(part.strip().replace(" ", "") for part in requirement_text[match.end() :].split(",") if part.strip())
        )
        if separator:
            if not re.fullmatch(r'''\s*extra\s*==\s*["']test["']\s*''', marker):
                raise ArtifactAuditError(f"{source} contains an unexpected dependency marker: {requirement!r}")
            if dependency_name in extra_requirements:
                raise ArtifactAuditError(f"{source} contains a duplicate test-extra dependency: {dependency_name}")
            extra_requirements[dependency_name] = specifiers
        else:
            if dependency_name in runtime_requirements:
                raise ArtifactAuditError(f"{source} contains a duplicate runtime dependency: {dependency_name}")
            runtime_requirements[dependency_name] = specifiers
            if dependency_name in {"build", "pytest", "setuptools", "twine", "wheel"}:
                raise ArtifactAuditError(f"{source} promotes a development dependency to runtime: {dependency_name}")

    expected_runtime = _requirements_by_name(contract.dependencies)
    expected_extra = _requirements_by_name(contract.optional_dependencies.get("test", ()))
    if runtime_requirements != expected_runtime:
        raise ArtifactAuditError(f"{source} runtime dependencies do not match pyproject.toml")
    if extra_requirements != expected_extra:
        raise ArtifactAuditError(f"{source} test-extra dependencies do not match pyproject.toml")
    if set(message.get_all("Provides-Extra", [])) != {"test"}:
        raise ArtifactAuditError(f"{source} must provide exactly the test extra")


def _requirements_by_name(requirements: Iterable[str]) -> dict[str, tuple[str, ...]]:
    parsed: dict[str, tuple[str, ...]] = {}
    for requirement in requirements:
        match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)(.*)", requirement)
        if match is None or ";" in requirement:
            raise ArtifactAuditError(f"unsupported dependency declaration in pyproject.toml: {requirement!r}")
        name = normalize_distribution_name(match.group(1))
        if name in parsed:
            raise ArtifactAuditError(f"duplicate dependency declaration in pyproject.toml: {name}")
        parsed[name] = tuple(
            sorted(part.strip().replace(" ", "") for part in match.group(2).split(",") if part.strip())
        )
    return parsed


def _validate_entry_points(data: bytes, contract: ProjectContract, *, source: str) -> None:
    expected = "[console_scripts]\nai-sdlc = ai_sdlc_harness.cli:main\n"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactAuditError(f"{source} entry points are not UTF-8") from exc
    if text.replace("\r\n", "\n") != expected:
        raise ArtifactAuditError(f"{source} console entry point is incorrect")
    if contract.scripts != {"ai-sdlc": "ai_sdlc_harness.cli:main"}:
        raise ArtifactAuditError("pyproject.toml console-script contract is unexpected")


def _validate_record(archive: zipfile.ZipFile, expected: frozenset[str], record_name: str) -> None:
    try:
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    except (KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise ArtifactAuditError("wheel RECORD is missing or malformed") from exc
    if any(len(row) != 3 for row in rows):
        raise ArtifactAuditError("wheel RECORD must contain three columns per row")
    record_paths = [row[0] for row in rows]
    if len(record_paths) != len(set(record_paths)) or set(record_paths) != set(expected):
        raise ArtifactAuditError("wheel RECORD inventory does not match wheel members")
    for path, digest_text, size_text in rows:
        if path == record_name:
            if digest_text or size_text:
                raise ArtifactAuditError("wheel RECORD must not hash itself")
            continue
        payload = archive.read(path)
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")
        if digest_text != f"sha256={digest}" or size_text != str(len(payload)):
            raise ArtifactAuditError(f"wheel RECORD checksum mismatch for {path}")


def audit_wheel(path: Path, contract: ProjectContract) -> AuditedArtifact:
    if path.name != contract.wheel_filename:
        raise ArtifactAuditError(f"unexpected wheel filename: {path.name}")
    if path.stat().st_size > MAX_ARCHIVE_SIZE:
        raise ArtifactAuditError("wheel exceeds the maximum audited size")
    try:
        with zipfile.ZipFile(path) as archive:
            files: dict[str, zipfile.ZipInfo] = {}
            directories: set[str] = set()
            total_size = 0
            for info in archive.infolist():
                member_path = _safe_archive_path(info.filename, archive="wheel")
                normalized = member_path.as_posix()
                if normalized in files or normalized in directories:
                    raise ArtifactAuditError(f"wheel contains a duplicate member: {normalized}")
                mode = info.external_attr >> 16
                if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ArtifactAuditError(f"wheel contains an unsafe special member: {normalized}")
                if info.is_dir():
                    directories.add(normalized)
                    continue
                if info.file_size > MAX_MEMBER_SIZE:
                    raise ArtifactAuditError(f"wheel member is too large: {normalized}")
                total_size += info.file_size
                if total_size > MAX_ARCHIVE_SIZE:
                    raise ArtifactAuditError("wheel uncompressed content exceeds the audit limit")
                files[normalized] = info

            expected = _expected_wheel_files(contract)
            for name in files:
                if PurePosixPath(name).parts[0] in FORBIDDEN_ROOTS:
                    raise ArtifactAuditError(f"wheel contains forbidden top-level content: {name}")
            unexpected = sorted(set(files) - set(expected))
            missing = sorted(set(expected) - set(files))
            unexpected_directories = sorted(directories - set(_expected_parent_directories(expected)))
            if unexpected:
                raise ArtifactAuditError(f"wheel contains unexpected file: {unexpected[0]}")
            if missing:
                raise ArtifactAuditError(f"wheel is missing required file: {missing[0]}")
            if unexpected_directories:
                raise ArtifactAuditError(f"wheel contains unexpected directory: {unexpected_directories[0]}")

            metadata_name = f"{contract.dist_info}/METADATA"
            wheel_metadata_name = f"{contract.dist_info}/WHEEL"
            entry_points_name = f"{contract.dist_info}/entry_points.txt"
            record_name = f"{contract.dist_info}/RECORD"
            _validate_metadata(archive.read(metadata_name), contract, source="wheel METADATA")
            _validate_entry_points(archive.read(entry_points_name), contract, source="wheel")
            wheel_metadata = email.message_from_bytes(archive.read(wheel_metadata_name))
            if wheel_metadata.get_all("Tag", []) != ["py3-none-any"]:
                raise ArtifactAuditError("wheel tag must be exactly py3-none-any")
            if wheel_metadata.get("Root-Is-Purelib", "").lower() != "true":
                raise ArtifactAuditError("wheel must declare Root-Is-Purelib: true")
            if archive.read(f"{contract.dist_info}/top_level.txt") != b"ai_sdlc_harness\n":
                raise ArtifactAuditError("wheel top_level.txt is incorrect")
            _validate_record(archive, expected, record_name)
            members = tuple(sorted(files))
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ArtifactAuditError(f"cannot audit wheel {path.name}: {exc}") from exc
    return _artifact_result(path, members)


def audit_sdist(path: Path, contract: ProjectContract) -> AuditedArtifact:
    if path.name != contract.sdist_filename:
        raise ArtifactAuditError(f"unexpected sdist filename: {path.name}")
    if path.stat().st_size > MAX_ARCHIVE_SIZE:
        raise ArtifactAuditError("sdist exceeds the maximum audited size")
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            files: dict[str, tarfile.TarInfo] = {}
            directories: set[str] = set()
            total_size = 0
            for info in archive.getmembers():
                member_path = _safe_archive_path(info.name, archive="sdist")
                normalized = member_path.as_posix()
                if normalized in files or normalized in directories:
                    raise ArtifactAuditError(f"sdist contains a duplicate member: {normalized}")
                if info.isdir():
                    directories.add(normalized)
                    continue
                if not info.isreg():
                    raise ArtifactAuditError(f"sdist contains an unsafe link or special member: {normalized}")
                if info.size > MAX_MEMBER_SIZE:
                    raise ArtifactAuditError(f"sdist member is too large: {normalized}")
                total_size += info.size
                if total_size > MAX_ARCHIVE_SIZE:
                    raise ArtifactAuditError("sdist uncompressed content exceeds the audit limit")
                files[normalized] = info

            all_paths = set(files) | directories
            roots = {PurePosixPath(name).parts[0] for name in all_paths}
            if roots != {contract.sdist_root}:
                raise ArtifactAuditError("sdist must contain one expected top-level project root")
            for name in all_paths:
                relative_parts = PurePosixPath(name).parts[1:]
                if relative_parts and relative_parts[0] in FORBIDDEN_ROOTS:
                    raise ArtifactAuditError(f"sdist contains forbidden path: {name}")

            expected = _expected_sdist_files(contract)
            unexpected = sorted(set(files) - set(expected))
            missing = sorted(set(expected) - set(files))
            unexpected_directories = sorted(directories - set(_expected_parent_directories(expected)))
            if unexpected:
                raise ArtifactAuditError(f"sdist contains unexpected file: {unexpected[0]}")
            if missing:
                raise ArtifactAuditError(f"sdist is missing required file: {missing[0]}")
            if unexpected_directories:
                raise ArtifactAuditError(f"sdist contains unexpected directory: {unexpected_directories[0]}")

            root_metadata = f"{contract.sdist_root}/PKG-INFO"
            egg_metadata = f"{contract.sdist_root}/src/{contract.filename_stem}.egg-info/PKG-INFO"
            entry_points = f"{contract.sdist_root}/src/{contract.filename_stem}.egg-info/entry_points.txt"
            for metadata_name in (root_metadata, egg_metadata):
                extracted = archive.extractfile(files[metadata_name])
                if extracted is None:
                    raise ArtifactAuditError(f"sdist metadata cannot be read: {metadata_name}")
                _validate_metadata(extracted.read(), contract, source=f"sdist {metadata_name}")
            extracted_entry_points = archive.extractfile(files[entry_points])
            if extracted_entry_points is None:
                raise ArtifactAuditError("sdist entry_points.txt cannot be read")
            _validate_entry_points(extracted_entry_points.read(), contract, source="sdist")
            members = tuple(sorted(files))
    except (OSError, tarfile.TarError, KeyError) as exc:
        raise ArtifactAuditError(f"cannot audit sdist {path.name}: {exc}") from exc
    return _artifact_result(path, members)


def _artifact_result(path: Path, members: Iterable[str]) -> AuditedArtifact:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return AuditedArtifact(
        filename=path.name,
        size=path.stat().st_size,
        sha256=digest.hexdigest(),
        members=tuple(members),
    )


def _checksum_bytes(artifacts: Iterable[AuditedArtifact]) -> bytes:
    lines = [f"{artifact.sha256}  {artifact.filename}\n" for artifact in sorted(artifacts, key=lambda item: item.filename)]
    return "".join(lines).encode("ascii")


def audit_release_artifacts(
    artifact_directory: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    verify: bool = False,
) -> AuditReport:
    artifact_directory = artifact_directory.resolve()
    if not artifact_directory.is_dir():
        raise ArtifactAuditError(f"artifact directory does not exist: {artifact_directory}")
    contract = load_project_contract(project_root)
    allowed_names = {contract.wheel_filename, contract.sdist_filename, CHECKSUM_FILENAME}
    entries = list(artifact_directory.iterdir())
    if any(not entry.is_file() for entry in entries):
        raise ArtifactAuditError("artifact directory must contain files only")
    unexpected = sorted(entry.name for entry in entries if entry.name not in allowed_names)
    if unexpected:
        raise ArtifactAuditError(f"artifact directory contains unexpected artifact: {unexpected[0]}")
    wheel_paths = [entry for entry in entries if entry.suffix == ".whl"]
    sdist_paths = [entry for entry in entries if entry.name.endswith(".tar.gz")]
    if len(wheel_paths) != 1 or len(sdist_paths) != 1:
        raise ArtifactAuditError("artifact directory must contain exactly one wheel and one sdist")
    if wheel_paths[0].name != contract.wheel_filename or sdist_paths[0].name != contract.sdist_filename:
        raise ArtifactAuditError("artifact filenames do not match the project name and version")

    wheel = audit_wheel(wheel_paths[0], contract)
    sdist = audit_sdist(sdist_paths[0], contract)
    checksum_path = artifact_directory / CHECKSUM_FILENAME
    expected_checksums = _checksum_bytes((wheel, sdist))
    if verify:
        try:
            actual_checksums = checksum_path.read_bytes()
        except OSError as exc:
            raise ArtifactAuditError(f"cannot read {CHECKSUM_FILENAME}: {exc}") from exc
        if actual_checksums != expected_checksums:
            raise ArtifactAuditError(f"{CHECKSUM_FILENAME} does not exactly match the audited artifacts")
    else:
        temporary = checksum_path.with_name(f".{CHECKSUM_FILENAME}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(expected_checksums)
            temporary.replace(checksum_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return AuditReport(wheel=wheel, sdist=sdist, checksum_path=checksum_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_directory", type=Path, help="Directory containing exactly one wheel and one sdist.")
    parser.add_argument("--verify", action="store_true", help="Verify the existing SHA256SUMS instead of writing it.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_release_artifacts(args.artifact_directory, verify=args.verify)
    except ArtifactAuditError as exc:
        print(f"artifact audit failed: {exc}", file=sys.stderr)
        return 1
    for artifact in (report.wheel, report.sdist):
        print(f"audited {artifact.filename}: {len(artifact.members)} files, {artifact.size} bytes, sha256 {artifact.sha256}")
    print(f"{'verified' if args.verify else 'wrote'} {report.checksum_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
