from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_sdlc_harness.files import (
    ExactBytePersistenceError,
    PathSafetyError,
    normalize_managed_path,
    persist_exact_bytes,
    resolve_managed_output_under_root,
    resolve_under_root,
    sha256_bytes,
    sha256_file,
    write_bytes_atomic,
)
from ai_sdlc_harness.manifest import sha256_file as manifest_sha256_file
from ai_sdlc_harness.redact import redact_text, redact_value


def test_posix_and_windows_style_relative_paths_are_normalized(project_tmp):
    assert normalize_managed_path(".harness/generated/agent-instructions.md").as_posix() == (
        ".harness/generated/agent-instructions.md"
    )
    assert normalize_managed_path(".harness\\packs\\selected.yaml").as_posix() == ".harness/packs/selected.yaml"
    assert resolve_under_root(project_tmp, ".harness\\state.json").parent == project_tmp.resolve() / ".harness"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        ".harness/../escape.txt",
        "/tmp/escape.txt",
        "C:/Temp/escape.txt",
        "C:\\Temp\\escape.txt",
    ],
)
def test_unsafe_managed_paths_are_rejected(project_tmp, path):
    with pytest.raises(PathSafetyError):
        resolve_under_root(project_tmp, path)


def _symlink_or_skip(link, target, *, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        if os.environ.get("AI_SDLC_REQUIRE_REAL_SYMLINKS") == "1":
            pytest.fail(f"real symlink creation is required but unavailable: {exc}", pytrace=False)
        pytest.skip(f"symlink creation is unavailable: {exc}")


class _UnavailableSymlink:
    def symlink_to(self, target, *, target_is_directory=False):
        raise OSError("simulated unavailable symlink support")


def test_symlink_helper_skips_when_real_symlinks_are_unavailable_by_default(monkeypatch):
    monkeypatch.delenv("AI_SDLC_REQUIRE_REAL_SYMLINKS", raising=False)

    with pytest.raises(pytest.skip.Exception, match="symlink creation is unavailable"):
        _symlink_or_skip(_UnavailableSymlink(), "target")


def test_symlink_helper_fails_when_real_symlinks_are_required(monkeypatch):
    monkeypatch.setenv("AI_SDLC_REQUIRE_REAL_SYMLINKS", "1")

    with pytest.raises(pytest.fail.Exception, match="real symlink creation is required"):
        _symlink_or_skip(_UnavailableSymlink(), "target")


def test_managed_output_resolver_allows_absent_and_regular_paths(project_tmp):
    relative = ".harness/tasks/sample/output.md"
    target = project_tmp / ".harness" / "tasks" / "sample" / "output.md"

    assert resolve_managed_output_under_root(project_tmp, relative) == target

    target.parent.mkdir(parents=True)
    target.write_bytes(b"regular")

    assert resolve_managed_output_under_root(project_tmp, relative) == target


def test_managed_output_resolver_rejects_mocked_final_symlink(
    project_tmp,
    monkeypatch,
):
    relative = ".harness/tasks/sample/output.md"
    target = project_tmp / ".harness" / "tasks" / "sample" / "output.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep")
    real_is_symlink = Path.is_symlink

    def report_final_symlink(path):
        return path == target or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_final_symlink)

    with pytest.raises(PathSafetyError) as exc_info:
        resolve_managed_output_under_root(project_tmp, relative)

    assert str(exc_info.value) == (
        "managed output path must not be a symlink: "
        ".harness/tasks/sample/output.md"
    )
    assert target.read_bytes() == b"keep"


def test_managed_output_resolver_rejects_mocked_parent_symlink(
    project_tmp,
    monkeypatch,
):
    relative = ".harness/tasks/sample/output.md"
    linked_parent = project_tmp / ".harness" / "tasks"
    linked_parent.mkdir(parents=True)
    sentinel = linked_parent / "keep.txt"
    sentinel.write_bytes(b"keep")
    target = linked_parent / "sample" / "output.md"
    real_is_symlink = Path.is_symlink

    def report_parent_symlink(path):
        return path == linked_parent or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", report_parent_symlink)

    with pytest.raises(PathSafetyError) as exc_info:
        resolve_managed_output_under_root(project_tmp, relative)

    assert str(exc_info.value) == (
        "managed output path parent must not be a symlink: .harness/tasks"
    )
    assert sentinel.read_bytes() == b"keep"
    assert not target.exists()


def test_managed_output_resolver_rejects_direct_in_root_symlink(project_tmp):
    target = project_tmp / "real-output.md"
    target.write_bytes(b"keep")
    link = project_tmp / ".harness" / "tasks" / "sample" / "output.md"
    link.parent.mkdir(parents=True)
    _symlink_or_skip(link, target)

    with pytest.raises(
        PathSafetyError,
        match="managed output path must not be a symlink",
    ):
        resolve_managed_output_under_root(
            project_tmp,
            ".harness/tasks/sample/output.md",
        )

    assert target.read_bytes() == b"keep"


def test_managed_output_resolver_rejects_outside_root_symlink(project_tmp):
    outside = project_tmp.parent / "outside-output.md"
    outside.write_bytes(b"outside")
    link = project_tmp / ".harness" / "tasks" / "sample" / "output.md"
    link.parent.mkdir(parents=True)
    _symlink_or_skip(link, outside)

    with pytest.raises(
        PathSafetyError,
        match="managed output path must not be a symlink",
    ):
        resolve_managed_output_under_root(
            project_tmp,
            ".harness/tasks/sample/output.md",
        )

    assert outside.read_bytes() == b"outside"


def test_managed_output_resolver_rejects_symlinked_parent(project_tmp):
    real_tasks = project_tmp / "real-tasks"
    real_tasks.mkdir()
    harness = project_tmp / ".harness"
    harness.mkdir()
    linked_tasks = harness / "tasks"
    _symlink_or_skip(linked_tasks, real_tasks, target_is_directory=True)

    with pytest.raises(
        PathSafetyError,
        match="managed output path parent must not be a symlink",
    ):
        resolve_managed_output_under_root(
            project_tmp,
            ".harness/tasks/sample/output.md",
        )


def test_secret_redaction_helper_redacts_keys_and_values():
    fake_openai_key = "sk-" + "abcdefghijklmnop"
    fake_authorization = "Authorization: " + "Bearer " + "abcdefghijklmnop"
    data = {
        "api_key": "abc123",
        "password": "do-not-display",
        "access_token": "token-" + "abcdefghijklmnop",
        "nested": {
            "message": fake_authorization,
            "safe": "visible",
        },
    }

    redacted = redact_value(data)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["nested"]["message"] == "Authorization: [REDACTED]"
    assert redacted["nested"]["safe"] == "visible"
    assert redact_text(fake_openai_key) == "[REDACTED]"


def test_sha256_bytes_supports_empty_ascii_and_exact_unicode_bytes():
    unicode_bytes = "snowman \N{SNOWMAN}".encode("utf-8")

    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_bytes(unicode_bytes) == sha256_bytes(unicode_bytes)
    assert len(sha256_bytes(unicode_bytes)) == 64
    assert sha256_bytes(b"line\r\n") != sha256_bytes(b"line\n")


def test_sha256_bytes_rejects_non_bytes():
    with pytest.raises(TypeError, match="must be bytes"):
        sha256_bytes("not bytes")  # type: ignore[arg-type]


def test_file_hash_matches_exact_bytes_and_manifest_compatibility_import(project_tmp):
    path = project_tmp / "exact.bin"
    content = "snowman \N{SNOWMAN}\r\nnext\n".encode("utf-8")
    path.write_bytes(content)

    expected = sha256_bytes(content)

    assert sha256_file(path) == expected
    assert manifest_sha256_file(path) == expected


def test_atomic_byte_write_preserves_exact_unicode_and_newlines(project_tmp):
    path = project_tmp / "atomic.bin"
    content = "snowman \N{SNOWMAN}\r\nnext\n".encode("utf-8")

    write_bytes_atomic(path, content)

    assert path.read_bytes() == content


def test_atomic_byte_write_replaces_existing_file(project_tmp):
    path = project_tmp / "atomic.bin"
    path.write_bytes(b"old")

    write_bytes_atomic(path, b"new")

    assert path.read_bytes() == b"new"


def test_atomic_replacement_failure_preserves_destination_and_cleans_temp(project_tmp, monkeypatch):
    path = project_tmp / "atomic.bin"
    path.write_bytes(b"old")

    def fail_replace(_source, _destination):
        raise OSError("replacement failed")

    monkeypatch.setattr("ai_sdlc_harness.files.os.replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        write_bytes_atomic(path, b"new")

    assert path.read_bytes() == b"old"
    assert list(project_tmp.iterdir()) == [path]


def test_persist_exact_bytes_writes_absent_file_and_returns_exact_hash(project_tmp):
    path = project_tmp / "persisted.bin"
    content = "snowman \N{SNOWMAN}\r\nnext\n".encode("utf-8")

    result = persist_exact_bytes(path, content)

    assert result.changed is True
    assert result.sha256 == sha256_bytes(content)
    assert path.read_bytes() == content


def test_persist_exact_bytes_replaces_different_existing_bytes(project_tmp):
    path = project_tmp / "persisted.bin"
    path.write_bytes(b"old")

    result = persist_exact_bytes(path, b"new")

    assert result.changed is True
    assert result.sha256 == sha256_bytes(b"new")
    assert path.read_bytes() == b"new"


def test_persist_exact_bytes_skips_identical_file_without_atomic_write(
    project_tmp,
    monkeypatch,
):
    path = project_tmp / "persisted.bin"
    content = b"same\r\nbytes\n"
    path.write_bytes(content)

    def fail_write(_path, _content):
        pytest.fail("identical exact bytes were rewritten")

    monkeypatch.setattr("ai_sdlc_harness.files.write_bytes_atomic", fail_write)

    result = persist_exact_bytes(path, content)

    assert result.changed is False
    assert result.sha256 == sha256_bytes(content)
    assert path.read_bytes() == content


def test_persist_exact_bytes_replacement_failure_preserves_destination_and_temp_cleanup(
    project_tmp,
    monkeypatch,
):
    path = project_tmp / "persisted.bin"
    path.write_bytes(b"old")

    def fail_replace(_source, _destination):
        raise OSError("replacement failed")

    monkeypatch.setattr("ai_sdlc_harness.files.os.replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        persist_exact_bytes(path, b"new")

    assert path.read_bytes() == b"old"
    assert list(project_tmp.iterdir()) == [path]


def test_persist_exact_bytes_rejects_non_bytes(project_tmp):
    with pytest.raises(TypeError, match="must be bytes"):
        persist_exact_bytes(project_tmp / "persisted.bin", "not bytes")  # type: ignore[arg-type]


def test_persist_exact_bytes_detects_post_write_mismatch(project_tmp, monkeypatch):
    path = project_tmp / "persisted.bin"

    def corrupt_write(target, _content):
        target.write_bytes(b"corrupted")

    monkeypatch.setattr("ai_sdlc_harness.files.write_bytes_atomic", corrupt_write)

    with pytest.raises(ExactBytePersistenceError, match="do not match"):
        persist_exact_bytes(path, b"intended")

    assert path.read_bytes() == b"corrupted"
    assert list(project_tmp.iterdir()) == [path]
