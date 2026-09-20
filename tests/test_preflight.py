from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from ai_sdlc_harness import preflight as preflight_module
from ai_sdlc_harness.files import PathSafetyError
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.spec import run_spec
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.verify import verify_project
from tests.lineage_test_helpers import (
    install_released_v1_with_current_task_records,
)


TASK_FILES = [
    "task.md",
    "acceptance.md",
    "architecture-notes.md",
    "coupling-notes.md",
    "test-contract.md",
    "verification.md",
    "evidence.md",
]

BASE_MANAGED_FILES = [
    ".harness/config.yaml",
    ".harness/state.json",
    ".harness/manifest.json",
    ".harness/generated/agent-instructions.md",
    ".harness/packs/selected.yaml",
]


def _preflight_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "preflight.md"


def _task_path(root: Path, slug: str, filename: str) -> Path:
    return root / ".harness" / "tasks" / slug / filename


def _manifest(root: Path) -> dict:
    return json.loads((root / ".harness" / "manifest.json").read_text(encoding="utf-8"))


def _manifest_entry(root: Path, path: str) -> dict:
    entries = {entry["path"]: entry for entry in _manifest(root)["managed_files"]}
    return entries[path]


def _start_sample_task(root: Path, title: str = "Preflight me") -> str:
    assert init_project(root)[0] == 0
    assert start_task(root, title)[0] == 0
    return "preflight-me"


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        if os.environ.get("AI_SDLC_REQUIRE_REAL_SYMLINKS") == "1":
            pytest.fail(f"real symlink creation is required but unavailable: {exc}", pytrace=False)
        pytest.skip(f"symlink creation is unavailable: {exc}")


def _fill_boundary_ready_task(root: Path, slug: str) -> None:
    _task_path(root, slug, "task.md").write_text(
        """# Task: Preflight me

Task slug: `preflight-me`

## Implementation Boundary

Expected change area: `src/ai_sdlc_harness/preflight.py`.
Limit implementation to the preflight report and tests.
No public CLI flags or task artifact files should change.

## Assumptions And Open Questions

No open questions.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- `preflight.md` answers whether the task is bounded enough for implementation.
- Findings remain machine-readable with blocker, warning, and info prefixes.

## Protected Behavior And Non-Goals

- Do not add interactive preflight.
- Do not add YAML, JSON, commands, or task artifact files.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "architecture-notes.md").write_text(
        """# Architecture Notes

## Boundary

The preflight command owns deterministic task-readiness reporting.

## Responsibility Change

Preflight now evaluates boundary readiness from existing task artifacts.

## Existing Patterns To Preserve

Preserve manifest-managed report writes and non-destructive overwrite checks.

## Interface And Compatibility Impact

No CLI interface change.

## Architecture Hygiene

Keep checks shallow and deterministic.

## Security And Privacy Risk Surface

Not applicable; no security or privacy-sensitive surface is expected.

## Trade-Offs And Open Questions

No open questions.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "coupling-notes.md").write_text(
        """# Coupling Notes

## New Or Changed Coupling

Generate continues to consume machine-readable findings from `preflight.md`.

## Maintainability Sensors

The report keeps concise readiness sections and a bounded finding list.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Manifest overwrite safety remains unchanged.

## Desired Behavior Tests

- Boundary-first report sections are rendered in deterministic order.

## Regression Tests

- Generated worksets still parse preflight findings.

## Negative And Edge Cases

- TODO-only artifacts produce findings but do not fail the command.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run

py -m pytest -p no:cacheprovider tests/test_preflight.py

## Results

Not run yet; expected after implementation.

## Manual Review Notes

Review generated `preflight.md`.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

Record implementation evidence after verification.

## Generated Or Updated Artifacts

Record changed source and test files.
""",
        encoding="utf-8",
    )


def _assert_heading_order(text: str, headings: list[str]) -> None:
    previous = -1
    for heading in headings:
        index = text.index(f"## {heading}")
        assert index > previous
        previous = index


def test_preflight_fails_before_init(project_tmp):
    code, messages = run_preflight(project_tmp, "missing-task")

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_preflight_fails_if_task_does_not_exist(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_preflight(project_tmp, "missing-task")

    assert code == 1
    assert "Task folder does not exist: .harness/tasks/missing-task" in messages[0]


def test_preflight_rejects_unsafe_task_slug(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_preflight(project_tmp, "../bad")

    assert code == 2
    assert "Task slug is unsafe" in messages[0]


def test_preflight_creates_report_with_signals_packs_and_readiness(project_tmp):
    slug = _start_sample_task(project_tmp)

    code, messages = run_preflight(project_tmp, slug)

    report_path = _preflight_path(project_tmp, slug)
    text = report_path.read_text(encoding="utf-8")
    assert code == 0
    assert f"preflight report: .harness/tasks/{slug}/preflight.md" in messages
    assert report_path.is_file()
    assert "Task slug: `preflight-me`" in text
    _assert_heading_order(
        text,
        [
            "Summary",
            "Boundary Readiness",
            "Requirements Readiness",
            "Architecture / Coupling Readiness",
            "Security / Privacy Risk-Surface Readiness",
            "Test / Verification Readiness",
            "Open Questions",
            "Repository Signals",
            "Selected Packs",
            "Findings",
            "Recommended Next Actions",
            "Disclaimer",
        ],
    )
    assert "- Boundary answer: Needs boundary clarification before implementation." in text
    assert "## Boundary Readiness" in text
    assert "- Implementation boundary: todo only" in text
    assert "- Requirements and acceptance criteria: todo only" in text
    assert "- Security and privacy risk surface: todo only" in text
    assert "- Verification commands: todo only" in text
    assert "## Repository Signals" in text
    assert "Git repo detected:" in text
    assert "Detected languages:" in text
    assert "Detected package managers:" in text
    assert "Detected test frameworks:" in text
    assert "Detected CI:" in text
    assert "Existing agent files:" in text
    assert "It does not ask interactive questions, call AI models, scan for security issues, run tests" in text
    assert "semanticenforcement" not in text
    assert "A pack is an optional reusable guardrail bundle" in text
    assert "- none selected" in text
    assert "architecture-generic" not in text
    assert "coupling-baseline" not in text
    assert "observability-baseline" not in text
    assert "security-baseline" not in text
    for filename in TASK_FILES:
        assert f"- {filename}: present=yes; readable=yes;" in text
    assert "blocker: implementation boundary is todo only." in text
    assert "blocker: expected change area is missing or still TODO." in text
    assert "blocker: requirements and acceptance criteria are todo only." in text
    assert "warning: protected behavior and non-goals are todo only." in text
    assert "warning: security/privacy risk surface is unknown or still TODO." in text
    assert "warning: test intent is missing or still TODO." in text
    assert "warning: no verification commands or checks are recorded as run." in text
    assert "warning: no recognized test-framework signal detected." in text
    assert "warning: no CI detected." in text
    assert "info: preflight uses shallow section and marker-based checks only." in text
    assert "info: preflight does not call AI models, scan security, run tests, validate compliance, or enforce packs." in text
    assert "compliant" not in text.lower()
    assert "score" not in text.lower()

    entry = _manifest_entry(project_tmp, f".harness/tasks/{slug}/preflight.md")
    assert entry["protected"] is True
    assert entry["hash_algorithm"] == "sha256"
    assert entry["sha256"]
    provenance = _manifest(project_tmp)["generated_artifact_provenance"]
    assert len(provenance) == 1
    record = provenance[0]
    assert record["output_path"] == f".harness/tasks/{slug}/preflight.md"
    assert record["output_sha256"] == hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    assert [item["dependency_path"] for item in record["dependencies"]] == sorted(
        [
            *(f".harness/tasks/{slug}/{filename}" for filename in TASK_FILES),
            ".harness/packs/selected.yaml",
        ]
    )
    assert [
        (item["observation_path"], item["predicate"])
        for item in record["repository_observations"]
    ] == sorted(
        [
            (".git", "exists"),
            ("pyproject.toml", "is_file"),
            ("package.json", "is_file"),
            ("Cargo.toml", "is_file"),
            ("go.mod", "is_file"),
            ("pytest.ini", "is_file"),
            ("AGENTS.md", "is_file"),
            ("CLAUDE.md", "is_file"),
            ("tests", "is_dir"),
            (".github/workflows", "is_dir"),
        ]
    )
    assert "generated_at" not in record


def test_preflight_recognizes_documented_unittest_without_repository_test_markers(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    _fill_boundary_ready_task(project_tmp, slug)
    _task_path(project_tmp, slug, "verification.md").write_text(
        "# Verification\n\n## Commands And Tests To Run\n\npy -m unittest -v\n",
        encoding="utf-8",
    )

    code, _ = run_preflight(project_tmp, slug)

    text = _preflight_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "Detected test frameworks: python-unittest" in text
    assert "no recognized test-framework signal detected" not in text
    assert "warning: no CI detected." in text


def test_preflight_reports_no_boundary_blockers_when_key_sections_are_filled(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_boundary_ready_task(project_tmp, slug)

    code, _messages = run_preflight(project_tmp, slug)

    text = _preflight_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Boundary answer: No blocker findings; review warnings before implementation." in text
    assert "- Implementation boundary: ready" in text
    assert "- Requirements and acceptance criteria: ready" in text
    assert "- Security and privacy risk surface: ready" in text
    assert "- blocker:" not in text
    assert "info: no security/privacy-sensitive surface was identified in task notes." in text


def test_preflight_blocks_missing_acceptance_criteria(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_boundary_ready_task(project_tmp, slug)
    _task_path(project_tmp, slug, "acceptance.md").write_text(
        """# Acceptance

## Protected Behavior And Non-Goals

- Do not change public CLI flags.
""",
        encoding="utf-8",
    )

    code, _messages = run_preflight(project_tmp, slug)

    text = _preflight_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "blocker: requirements and acceptance criteria are missing." in text


def test_preflight_warns_when_protected_behavior_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_boundary_ready_task(project_tmp, slug)
    _task_path(project_tmp, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- Boundary-first report sections are rendered.
""",
        encoding="utf-8",
    )

    code, _messages = run_preflight(project_tmp, slug)

    text = _preflight_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: protected behavior and non-goals are missing." in text


def test_preflight_warns_when_security_privacy_surface_is_unknown(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_boundary_ready_task(project_tmp, slug)
    architecture = _task_path(project_tmp, slug, "architecture-notes.md").read_text(encoding="utf-8")
    _task_path(project_tmp, slug, "architecture-notes.md").write_text(
        architecture.replace(
            "Not applicable; no security or privacy-sensitive surface is expected.",
            "Unknown until the changed files are selected.",
        ),
        encoding="utf-8",
    )

    code, _messages = run_preflight(project_tmp, slug)

    text = _preflight_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: security/privacy risk surface is unknown or still TODO." in text


def test_preflight_warns_for_nonblocking_open_questions(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_boundary_ready_task(project_tmp, slug)
    task_text = _task_path(project_tmp, slug, "task.md").read_text(encoding="utf-8")
    _task_path(project_tmp, slug, "task.md").write_text(
        task_text.replace("No open questions.", "Open question: Should warnings mention CI?"),
        encoding="utf-8",
    )

    code, _messages = run_preflight(project_tmp, slug)

    text = _preflight_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: open questions remain for human review." in text
    assert "blocker: blocking open questions remain before implementation." not in text


def test_preflight_dry_run_writes_nothing(project_tmp):
    slug = _start_sample_task(project_tmp)
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_preflight(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
    assert not _preflight_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_preflight_rerun_refreshes_manifest_managed_hash_clean_report(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)

    monkeypatch.setattr("ai_sdlc_harness.preflight._timestamp", lambda: "2026-07-05T00:00:00+00:00")
    assert run_preflight(project_tmp, slug)[0] == 0
    path_text = f".harness/tasks/{slug}/preflight.md"
    first_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    monkeypatch.setattr("ai_sdlc_harness.preflight._timestamp", lambda: "2026-07-05T00:00:01+00:00")
    code, messages = run_preflight(project_tmp, slug)
    second_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    assert code == 0
    assert f"refresh file {path_text}" in messages
    assert first_hash != second_hash


def test_preflight_does_not_rewrite_hash_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    report_path = _preflight_path(project_tmp, slug)
    report_path.write_text("custom report\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_preflight(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/preflight-me/preflight.md" in messages
    assert report_path.read_text(encoding="utf-8") == "custom report\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_preflight_force_overwrites_only_manifest_managed_preflight(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    report_path = _preflight_path(project_tmp, slug)
    user_note = report_path.parent / "user-note.md"
    report_path.write_text("custom report\n", encoding="utf-8")
    user_note.write_text("keep me\n", encoding="utf-8")

    code, messages = run_preflight(project_tmp, slug, force=True)

    assert code == 0
    assert "refresh file .harness/tasks/preflight-me/preflight.md" in messages
    assert "custom report" not in report_path.read_text(encoding="utf-8")
    assert user_note.read_text(encoding="utf-8") == "keep me\n"


def test_preflight_never_overwrites_unmanaged_existing_report(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Blocked report")[0] == 0
    report_path = _preflight_path(project_tmp, "blocked-report")
    report_path.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_preflight(project_tmp, "blocked-report", force=True)

    assert code == 1
    assert "unmanaged existing file .harness/tasks/blocked-report/preflight.md" in messages
    assert report_path.read_text(encoding="utf-8") == "user owned\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_verify_passes_after_preflight(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_fails_when_manifest_managed_preflight_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    _preflight_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/preflight-me/preflight.md" in message for message in messages)


def test_verify_fails_when_manifest_managed_preflight_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    _preflight_path(project_tmp, slug).write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/tasks/preflight-me/preflight.md" in message for message in messages)


def test_selected_task_status_omits_preflight_count_and_remains_read_only(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    tracked.append(_preflight_path(project_tmp, slug))
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert f"task: {slug}" in messages
    assert "manifest-managed preflight reports: 1" not in messages
    assert before == after


def test_preflight_complete_noop_preserves_output_and_manifest_bytes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.preflight._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_preflight(project_tmp, slug)[0] == 0
    output_before = _preflight_path(project_tmp, slug).read_bytes()
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    code, messages = run_preflight(project_tmp, slug)

    assert code == 0
    assert messages.count(
        f"skip unchanged file .harness/tasks/{slug}/preflight.md"
    ) == 1
    assert _preflight_path(project_tmp, slug).read_bytes() == output_before
    assert manifest_path.read_bytes() == manifest_before
    assert len(_manifest(project_tmp)["generated_artifact_provenance"]) == 1


def test_preflight_changed_output_invalidates_before_failed_write(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.preflight._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_preflight(project_tmp, slug)[0] == 0
    monkeypatch.setattr(
        "ai_sdlc_harness.preflight._timestamp",
        lambda: "2026-07-29T00:00:01+00:00",
    )

    def fail_after_invalidation(_path, _content):
        records = _manifest(project_tmp)["generated_artifact_provenance"]
        assert not any(
            record["output_path"].endswith("/preflight.md")
            for record in records
        )
        raise OSError("simulated output failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.preflight.persist_exact_bytes",
        fail_after_invalidation,
    )

    code, messages = run_preflight(project_tmp, slug)

    assert code == 1
    assert "simulated output failure" in messages[0]
    assert not _manifest(project_tmp)["generated_artifact_provenance"]


def test_successful_preflight_migrates_v1_and_preserves_managed_records(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    legacy = install_released_v1_with_current_task_records(project_tmp)
    managed_before = legacy["managed_files"]

    assert run_preflight(project_tmp, slug)[0] == 0

    migrated = _manifest(project_tmp)
    assert migrated["manifest_schema_version"] == 2
    before_by_path = {record["path"]: record for record in managed_before}
    after_by_path = {record["path"]: record for record in migrated["managed_files"]}
    for path, record in before_by_path.items():
        assert after_by_path[path] == record
    assert [
        record["output_path"]
        for record in migrated["generated_artifact_provenance"]
    ] == [f".harness/tasks/{slug}/preflight.md"]


def test_preflight_preserves_unrelated_spec_provenance(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_spec(project_tmp, slug)[0] == 0
    before = {
        record["output_path"]: record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
    }

    assert run_preflight(project_tmp, slug)[0] == 0

    after = {
        record["output_path"]: record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
    }
    for path, record in before.items():
        assert after[path] == record


def test_preflight_rendering_uses_the_exact_captured_task_bytes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_boundary_ready_task(project_tmp, slug)
    task_path = _task_path(project_tmp, slug, "task.md")
    captured_bytes = task_path.read_bytes()
    real_capture = preflight_module.capture_dependencies

    def capture_then_change(root, expected):
        captures = real_capture(root, expected)
        task_path.write_text(
            "# Task\n\n## Implementation Boundary\n\nTODO\n",
            encoding="utf-8",
        )
        return captures

    monkeypatch.setattr(
        "ai_sdlc_harness.preflight.capture_dependencies",
        capture_then_change,
    )

    code, _messages = run_preflight(project_tmp, slug)

    assert code == 0
    report = _preflight_path(project_tmp, slug).read_text(encoding="utf-8")
    assert "- Implementation boundary: ready" in report
    record = _manifest(project_tmp)["generated_artifact_provenance"][0]
    dependency = next(
        item
        for item in record["dependencies"]
        if item["dependency_path"].endswith("/task.md")
    )
    assert dependency["dependency_sha256"] == hashlib.sha256(
        captured_bytes
    ).hexdigest()


def test_preflight_observation_change_aborts_before_writes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    real_snapshot = preflight_module.snapshot_repository_observations
    calls = 0

    def change_before_second_snapshot(root, expected):
        nonlocal calls
        calls += 1
        if calls == 2:
            (project_tmp / "pyproject.toml").write_text(
                "[project]\n",
                encoding="utf-8",
            )
        return real_snapshot(root, expected)

    monkeypatch.setattr(
        "ai_sdlc_harness.preflight.snapshot_repository_observations",
        change_before_second_snapshot,
    )

    code, messages = run_preflight(project_tmp, slug)

    assert code == 1
    assert "Repository observations changed during preflight" in messages[0]
    assert not _preflight_path(project_tmp, slug).exists()
    assert manifest_path.read_bytes() == manifest_before


def test_preflight_rejects_symlinked_output_before_ownership_checks(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    target = project_tmp / "redirected-preflight.md"
    target.write_bytes(b"keep target")
    output = _preflight_path(project_tmp, slug)
    _symlink_or_skip(output, target)
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    code, messages = run_preflight(project_tmp, slug, force=True)

    assert code == 1
    assert "managed output path must not be a symlink" in messages[0]
    assert output.is_symlink()
    assert target.read_bytes() == b"keep target"
    assert manifest_path.read_bytes() == manifest_before


def test_preflight_checks_mocked_output_safety_before_capture(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    output = _preflight_path(project_tmp, slug)
    output.write_bytes(b"keep preflight")
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    def reject_output(*_args, **_kwargs):
        raise PathSafetyError("simulated unsafe managed output")

    def fail_if_captured(*_args, **_kwargs):
        pytest.fail("dependency capture ran after output safety failure")

    monkeypatch.setattr(
        preflight_module,
        "resolve_managed_output_under_root",
        reject_output,
    )
    monkeypatch.setattr(
        preflight_module,
        "capture_dependencies",
        fail_if_captured,
    )

    code, messages = run_preflight(project_tmp, slug, force=True)

    assert code == 1
    assert "simulated unsafe managed output" in messages[0]
    assert manifest_path.read_bytes() == manifest_before
    assert output.read_bytes() == b"keep preflight"


def test_preflight_final_manifest_failure_leaves_old_provenance_absent(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.spec._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_spec(project_tmp, slug)[0] == 0
    monkeypatch.setattr(
        "ai_sdlc_harness.preflight._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_preflight(project_tmp, slug)[0] == 0
    path_text = f".harness/tasks/{slug}/preflight.md"
    old_managed_hash = _manifest_entry(project_tmp, path_text)["sha256"]
    unrelated_before = {
        record["output_path"]: record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"] != path_text
    }
    monkeypatch.setattr(
        "ai_sdlc_harness.preflight._timestamp",
        lambda: "2026-07-29T00:00:01+00:00",
    )
    real_persist = preflight_module.persist_manifest_model
    calls = 0

    def fail_final(root, document):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_persist(root, document)
        raise OSError("simulated final manifest failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.preflight.persist_manifest_model",
        fail_final,
    )

    code, messages = run_preflight(project_tmp, slug)

    assert code == 1
    assert "simulated final manifest failure" in messages[0]
    manifest = _manifest(project_tmp)
    assert not any(
        record["output_path"] == path_text
        for record in manifest["generated_artifact_provenance"]
    )
    assert {
        record["output_path"]: record
        for record in manifest["generated_artifact_provenance"]
    } == unrelated_before
    assert _manifest_entry(project_tmp, path_text)["sha256"] == old_managed_hash
    assert hashlib.sha256(
        _preflight_path(project_tmp, slug).read_bytes()
    ).hexdigest() != old_managed_hash


def test_v1_final_manifest_failure_does_not_migrate_or_record_provenance(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    install_released_v1_with_current_task_records(project_tmp)
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    def fail_final(_root, _document):
        raise OSError("simulated v1 final manifest failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.preflight.persist_manifest_model",
        fail_final,
    )

    code, messages = run_preflight(project_tmp, slug)

    assert code == 1
    assert "simulated v1 final manifest failure" in messages[0]
    assert _preflight_path(project_tmp, slug).is_file()
    assert manifest_path.read_bytes() == manifest_before
    manifest = _manifest(project_tmp)
    assert "manifest_schema_version" not in manifest
    assert "generated_artifact_provenance" not in manifest
