from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUDITOR_PATH = ROOT / ".github/audit_release_artifacts.py"
SPEC = importlib.util.spec_from_file_location("release_artifact_auditor", AUDITOR_PATH)
assert SPEC is not None and SPEC.loader is not None
auditor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = auditor
SPEC.loader.exec_module(auditor)


def _metadata(contract, *, name: str | None = None, version: str | None = None) -> bytes:
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name or contract.name}",
        f"Version: {version or contract.version}",
        f"Requires-Python: {contract.requires_python}",
        f"License-Expression: {contract.license_expression}",
        "License-File: LICENSE",
        "License-File: NOTICE",
        "Requires-Dist: PyYAML<7,>=6",
        'Requires-Dist: pytest<9,>=8; extra == "test"',
        'Requires-Dist: setuptools>=77.0.3; extra == "test"',
        'Requires-Dist: wheel; extra == "test"',
        "Provides-Extra: test",
    ]
    lines.extend(f"Project-URL: {label}, {url}" for label, url in contract.urls.items())
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _entry_points() -> bytes:
    return b"[console_scripts]\nai-sdlc = ai_sdlc_harness.cli:main\n"


def _record_bytes(members: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted((*members, record_name)):
        if name == record_name:
            writer.writerow((name, "", ""))
            continue
        payload = members[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, f"sha256={digest}", str(len(payload))))
    return output.getvalue().encode("utf-8")


def _write_wheel(
    directory: Path,
    contract,
    *,
    missing: set[str] | None = None,
    metadata_name: str | None = None,
    metadata_version: str | None = None,
) -> Path:
    missing = missing or set()
    members: dict[str, bytes] = {}
    metadata_path = f"{contract.dist_info}/METADATA"
    wheel_path = f"{contract.dist_info}/WHEEL"
    entry_points_path = f"{contract.dist_info}/entry_points.txt"
    top_level_path = f"{contract.dist_info}/top_level.txt"
    record_path = f"{contract.dist_info}/RECORD"
    for name in auditor._expected_wheel_files(contract):
        if name in missing or name == record_path:
            continue
        if name == metadata_path:
            payload = _metadata(contract, name=metadata_name, version=metadata_version)
        elif name == wheel_path:
            payload = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
        elif name == entry_points_path:
            payload = _entry_points()
        elif name == top_level_path:
            payload = b"ai_sdlc_harness\n"
        else:
            payload = f"fixture for {name}\n".encode("utf-8")
        members[name] = payload
    if record_path not in missing:
        members[record_path] = _record_bytes(members, record_path)

    path = directory / contract.wheel_filename
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(members.items()):
            archive.writestr(name, payload)
    return path


def _write_sdist(
    directory: Path,
    contract,
    *,
    missing: set[str] | None = None,
    additions: dict[str, bytes] | None = None,
    directories: set[str] | None = None,
    unsafe_link: str | None = None,
) -> Path:
    missing = missing or set()
    additions = additions or {}
    directories = directories or set()
    root_metadata = f"{contract.sdist_root}/PKG-INFO"
    egg_metadata = f"{contract.sdist_root}/src/{contract.filename_stem}.egg-info/PKG-INFO"
    entry_points_path = f"{contract.sdist_root}/src/{contract.filename_stem}.egg-info/entry_points.txt"
    members: dict[str, bytes] = {}
    for name in auditor._expected_sdist_files(contract):
        if name in missing:
            continue
        if name in {root_metadata, egg_metadata}:
            payload = _metadata(contract)
        elif name == entry_points_path:
            payload = _entry_points()
        else:
            payload = f"fixture for {name}\n".encode("utf-8")
        members[name] = payload
    members.update(additions)

    path = directory / contract.sdist_filename
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
        for name in sorted(directories):
            info = tarfile.TarInfo(name.rstrip("/") + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        if unsafe_link is not None:
            info = tarfile.TarInfo(unsafe_link)
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            archive.addfile(info)
    return path


def _write_valid_pair(directory: Path):
    directory.mkdir(parents=True)
    contract = auditor.load_project_contract(ROOT)
    _write_wheel(directory, contract)
    _write_sdist(directory, contract)
    return contract


def test_auditor_accepts_exact_artifacts_and_verifies_deterministic_checksums(project_tmp):
    dist = project_tmp / "dist"
    contract = _write_valid_pair(dist)

    report = auditor.audit_release_artifacts(dist, project_root=ROOT)
    checksum_lines = report.checksum_path.read_text(encoding="ascii").splitlines()

    assert [line.split("  ", 1)[1] for line in checksum_lines] == sorted(
        [contract.sdist_filename, contract.wheel_filename]
    )
    assert all(auditor.CHECKSUM_FILENAME not in line for line in checksum_lines)
    assert auditor.audit_release_artifacts(dist, project_root=ROOT, verify=True) == report


def test_project_contract_requires_python_311_and_preserves_dependency_boundaries():
    contract = auditor.load_project_contract(ROOT)

    assert contract.name == "ai-sdlc-harness"
    assert contract.version == "1.0.0"
    assert contract.requires_python == ">=3.11"
    assert contract.dependencies == ("PyYAML>=6,<7",)
    assert contract.optional_dependencies["test"] == (
        "pytest>=8,<9",
        "setuptools>=77.0.3",
        "wheel",
    )


def test_project_contract_rejects_a_different_python_floor(project_tmp):
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (project_tmp / "pyproject.toml").write_text(
        pyproject.replace('requires-python = ">=3.11"', 'requires-python = ">=3.12"'),
        encoding="utf-8",
    )

    with pytest.raises(auditor.ArtifactAuditError, match="unexpected Requires-Python"):
        auditor.load_project_contract(project_tmp)


@pytest.mark.parametrize(
    "relative_name, expected_message",
    [
        ("unexpected.txt", "unexpected file"),
        ("docs/private.md", "forbidden path"),
        ("../escape.txt", "path traversal"),
    ],
)
def test_sdist_rejects_unexpected_forbidden_and_traversal_members(
    project_tmp, relative_name, expected_message
):
    dist = project_tmp / "dist"
    dist.mkdir()
    contract = auditor.load_project_contract(ROOT)
    archive_name = (
        relative_name
        if relative_name.startswith("..")
        else f"{contract.sdist_root}/{relative_name}"
    )
    path = _write_sdist(dist, contract, additions={archive_name: b"unexpected\n"})

    with pytest.raises(auditor.ArtifactAuditError, match=expected_message):
        auditor.audit_sdist(path, contract)


def test_sdist_rejects_unsafe_tar_link(project_tmp):
    dist = project_tmp / "dist"
    dist.mkdir()
    contract = auditor.load_project_contract(ROOT)
    path = _write_sdist(
        dist,
        contract,
        unsafe_link=f"{contract.sdist_root}/templates/task/unsafe-link.md",
    )

    with pytest.raises(auditor.ArtifactAuditError, match="unsafe link or special member"):
        auditor.audit_sdist(path, contract)


def test_sdist_rejects_unexpected_empty_directory(project_tmp):
    dist = project_tmp / "dist"
    dist.mkdir()
    contract = auditor.load_project_contract(ROOT)
    path = _write_sdist(
        dist,
        contract,
        directories={f"{contract.sdist_root}/unapproved-empty"},
    )

    with pytest.raises(auditor.ArtifactAuditError, match="unexpected directory"):
        auditor.audit_sdist(path, contract)


@pytest.mark.parametrize(
    "missing_name",
    [
        "templates/task/verification.md",
        "LICENSE",
        "NOTICE",
    ],
)
def test_sdist_rejects_missing_template_or_license(project_tmp, missing_name):
    dist = project_tmp / "dist"
    dist.mkdir()
    contract = auditor.load_project_contract(ROOT)
    missing = {f"{contract.sdist_root}/{missing_name}"}
    path = _write_sdist(dist, contract, missing=missing)

    with pytest.raises(auditor.ArtifactAuditError, match="missing required file"):
        auditor.audit_sdist(path, contract)


@pytest.mark.parametrize(
    "metadata_name, metadata_version, expected_message",
    [
        ("wrong-project", None, "incorrect package name"),
        (None, "9.9.9", "incorrect package version"),
    ],
)
def test_wheel_rejects_incorrect_package_metadata(
    project_tmp, metadata_name, metadata_version, expected_message
):
    dist = project_tmp / "dist"
    dist.mkdir()
    contract = auditor.load_project_contract(ROOT)
    path = _write_wheel(
        dist,
        contract,
        metadata_name=metadata_name,
        metadata_version=metadata_version,
    )

    with pytest.raises(auditor.ArtifactAuditError, match=expected_message):
        auditor.audit_wheel(path, contract)


def test_release_audit_rejects_extra_artifact(project_tmp):
    dist = project_tmp / "dist"
    _write_valid_pair(dist)
    (dist / "unapproved.zip").write_bytes(b"extra")

    with pytest.raises(auditor.ArtifactAuditError, match="unexpected artifact"):
        auditor.audit_release_artifacts(dist, project_root=ROOT)


def test_release_audit_verify_rejects_checksum_mismatch(project_tmp):
    dist = project_tmp / "dist"
    _write_valid_pair(dist)
    report = auditor.audit_release_artifacts(dist, project_root=ROOT)
    report.checksum_path.write_text("0" * 64 + "  wrong.whl\n", encoding="ascii")

    with pytest.raises(auditor.ArtifactAuditError, match="does not exactly match"):
        auditor.audit_release_artifacts(dist, project_root=ROOT, verify=True)
