from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from ai_sdlc_harness import test_contract as test_contract_module
from ai_sdlc_harness.files import PathSafetyError
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.test_contract import run_test_contract_review
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


def _review_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "test-contract-review.md"


def _task_path(root: Path, slug: str, filename: str) -> Path:
    return root / ".harness" / "tasks" / slug / filename


def _manifest(root: Path) -> dict:
    return json.loads((root / ".harness" / "manifest.json").read_text(encoding="utf-8"))


def _manifest_entry(root: Path, path: str) -> dict:
    entries = {entry["path"]: entry for entry in _manifest(root)["managed_files"]}
    return entries[path]


def _start_sample_task(root: Path, title: str = "Review tests") -> str:
    assert init_project(root)[0] == 0
    assert start_task(root, title)[0] == 0
    return "review-tests"


def _symlink_or_skip(link, target):
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        if os.environ.get("AI_SDLC_REQUIRE_REAL_SYMLINKS") == "1":
            pytest.fail(f"real symlink creation is required but unavailable: {exc}", pytrace=False)
        pytest.skip(f"symlink creation is unavailable: {exc}")


def _add_repo_signals(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname = \"sample\"\n", encoding="utf-8")
    (root / "tests").mkdir()
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("name: ci\n", encoding="utf-8")


def _fill_test_inputs(root: Path, slug: str) -> None:
    _task_path(root, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- The command writes a review report for the selected task.
- The command never rewrites user-editable test-contract.md.

## Protected Behavior And Non-Goals

- No generated tests.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Existing task artifact files remain unchanged.

## Desired Behavior Tests

- test-contract-review.md is generated for the selected task.

## Regression Tests

- Unmanaged review files are refused.

## Negative And Edge Cases

- Unsafe task slugs are rejected.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "verification.md").write_text(
        """# Verification

## Commands And Checks Run

py -m pytest

## Results

- Passed: focused tests completed successfully.

## Manual Review Notes

- Confirm the report wording is shallow and non-compliance-oriented.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

- Record implementation summary, tests/checks run, and known gaps for review.

## Generated Or Updated Artifacts

- Record generated reports and updated task artifacts.
""",
        encoding="utf-8",
    )


def _fill_legacy_test_inputs(root: Path, slug: str) -> None:
    _task_path(root, slug, "acceptance.md").write_text(
        """# Acceptance

## Acceptance Criteria

- The report is generated.

## Non-Goals

- No generated tests.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Existing files remain unchanged.

## Test Intent

- Review report is generated.

## Regression Tests

- Unmanaged reports are refused.

## Negative And Edge Cases

- Unsafe slugs are rejected.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run Later

py -m pytest

## Verification Results

- Expected result: command passes.

## Review Notes

- Confirm the generated report remains advisory.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "evidence.md").write_text(
        """# Evidence

## Review Evidence

- Record tests/checks run and review gaps.
""",
        encoding="utf-8",
    )


def test_test_contract_fails_before_init(project_tmp):
    code, messages = run_test_contract_review(project_tmp, "missing-task")

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_test_contract_fails_if_task_does_not_exist(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_test_contract_review(project_tmp, "missing-task")

    assert code == 1
    assert "Task folder does not exist: .harness/tasks/missing-task" in messages[0]


def test_test_contract_rejects_unsafe_task_slug(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_test_contract_review(project_tmp, "../bad")

    assert code == 2
    assert "Task slug is unsafe" in messages[0]


def test_test_contract_creates_review_with_signals_and_readiness(project_tmp):
    slug = _start_sample_task(project_tmp)
    _add_repo_signals(project_tmp)
    _fill_test_inputs(project_tmp, slug)

    code, messages = run_test_contract_review(project_tmp, slug)

    report_path = _review_path(project_tmp, slug)
    text = report_path.read_text(encoding="utf-8")
    assert code == 0
    assert f"test-contract review report: .harness/tasks/{slug}/test-contract-review.md" in messages
    assert report_path.is_file()
    assert "Task slug: `review-tests`" in text
    assert "- Review question: Are the expected checks clear enough for implementation and review?" in text
    assert "- Answer: No blocker findings; review warnings before implementation." in text
    expected_sections = [
        "## Summary",
        "## Requirements / Acceptance Readiness",
        "## Test Intent Readiness",
        "## Regression / Negative Coverage",
        "## Verification Record",
        "## Evidence Expectations",
        "## Repository Signals",
        "## Findings",
        "## Recommended Next Actions",
        "## Disclaimer",
    ]
    assert [line for line in text.splitlines() if line.startswith("## ")] == expected_sections
    assert "semantic enforcement" not in text
    assert "compliant" not in text.lower()
    assert "score" not in text.lower()
    assert "## Repository Signals" in text
    assert "Detected test frameworks: tests-directory-or-pytest" in text
    assert "Detected package managers: python-packaging" in text
    assert "Detected CI: github-actions" in text
    assert "- acceptance.md: present=yes; readable=yes; readiness=has substantive content" in text
    assert "- test-contract.md: present=yes; readable=yes; readiness=has substantive content" in text
    assert "- verification.md: present=yes; readable=yes; readiness=has substantive content" in text
    assert "- evidence.md: present=yes; readable=yes; readiness=has substantive content" in text
    assert "- Preflight report: missing" in text
    assert "- requirements / acceptance criteria: present=yes; readiness=substantive" in text
    assert "- protected behavior / non-goals: present=yes; readiness=substantive" in text
    assert "- characterization tests: present=yes; readiness=substantive" in text
    assert "- desired behavior tests: present=yes; readiness=substantive" in text
    assert "- regression tests: present=yes; readiness=substantive" in text
    assert "- negative and edge cases: present=yes; readiness=substantive" in text
    assert "- commands and checks run: present=yes; readiness=substantive" in text
    assert "- observed results: present=yes; readiness=substantive" in text
    assert "- manual review checks: present=yes; readiness=substantive" in text
    assert "- evidence expectations: present=yes; readiness=substantive" in text
    assert "- Test command marker: present" in text
    assert "- Manual verification notes: present" in text
    assert "info: no tests were run by this command." in text
    assert "does not generate tests, run tests, call AI models, enforce packs, inspect actual test files deeply" in text
    assert "blocker: no usable acceptance or test-contract content" not in text
    assert "warning: evidence expectations" not in text

    entry = _manifest_entry(project_tmp, f".harness/tasks/{slug}/test-contract-review.md")
    assert entry["protected"] is True
    assert entry["hash_algorithm"] == "sha256"
    assert entry["sha256"]
    provenance = _manifest(project_tmp)["generated_artifact_provenance"]
    assert len(provenance) == 1
    record = provenance[0]
    assert record["output_path"] == (
        f".harness/tasks/{slug}/test-contract-review.md"
    )
    assert record["output_sha256"] == hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    assert [item["dependency_path"] for item in record["dependencies"]] == sorted(
        [
            f".harness/tasks/{slug}/acceptance.md",
            f".harness/tasks/{slug}/test-contract.md",
            f".harness/tasks/{slug}/verification.md",
            f".harness/tasks/{slug}/evidence.md",
            f".harness/tasks/{slug}/preflight.md",
        ]
    )
    assert [
        (item["observation_path"], item["predicate"])
        for item in record["repository_observations"]
    ] == sorted(
        [
            ("pyproject.toml", "is_file"),
            ("package.json", "is_file"),
            ("Cargo.toml", "is_file"),
            ("go.mod", "is_file"),
            ("pytest.ini", "is_file"),
            ("tests", "is_dir"),
            (".github/workflows", "is_dir"),
        ]
    )
    assert "generated_at" not in record


def test_test_contract_recognizes_older_heading_fallbacks(project_tmp):
    slug = _start_sample_task(project_tmp)
    _add_repo_signals(project_tmp)
    _fill_legacy_test_inputs(project_tmp, slug)

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- requirements / acceptance criteria: present=yes; readiness=substantive" in text
    assert "- protected behavior / non-goals: present=yes; readiness=substantive" in text
    assert "- desired behavior tests: present=yes; readiness=substantive" in text
    assert "- commands and checks run: present=yes; readiness=substantive" in text
    assert "- observed results: present=yes; readiness=substantive" in text
    assert "- manual review checks: present=yes; readiness=substantive" in text
    assert "- evidence expectations: present=yes; readiness=substantive" in text
    assert "blocker:" not in text


def test_test_contract_warns_for_missing_ci_framework_and_unfilled_sections(project_tmp):
    slug = _start_sample_task(project_tmp)

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: no recognized test-framework signal detected." in text
    assert "warning: no CI detected." in text
    assert "warning: protected behavior / non-goals are TODO-only." in text
    assert "warning: characterization tests are TODO-only." in text
    assert "warning: regression tests are TODO-only." in text
    assert "warning: negative / edge-case tests are TODO-only." in text
    assert "warning: verification commands are missing." not in text
    assert "warning: expected results or manual review checks are missing." not in text
    assert "warning: evidence expectations are TODO-only." in text
    assert "blocker: requirements / acceptance criteria are TODO-only." in text
    assert "blocker: desired behavior tests are TODO-only." in text
    assert "blocker: no usable acceptance or test-contract content is available." in text


def test_test_contract_recognizes_documented_unittest_without_repository_test_markers(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    _fill_test_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "verification.md").write_text(
        "# Verification\n\n## Commands And Checks Run\n\npython -m unittest\n",
        encoding="utf-8",
    )

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "Detected test frameworks: python-unittest" in text
    assert "no recognized test-framework signal detected" not in text
    assert "warning: no CI detected." in text


def test_test_contract_reports_missing_empty_and_unreadable_inputs_as_findings(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "acceptance.md").write_text("", encoding="utf-8")
    _task_path(project_tmp, slug, "test-contract.md").unlink()
    evidence = _task_path(project_tmp, slug, "evidence.md")
    evidence.unlink()
    evidence.mkdir()

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "blocker: requirements / acceptance criteria are section missing." in text
    assert "blocker: desired behavior tests are missing." in text
    assert "blocker: no usable acceptance or test-contract content is available." in text
    assert "warning: evidence expectations are unreadable." in text


def test_test_contract_allows_missing_desired_tests_with_rationale_and_manual_path(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- Manual-only documentation review is required.

## Protected Behavior And Non-Goals

- Do not change application code.
""",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Review the current public wording before editing.

## Regression Tests

- Re-check the affected doc section.

## Negative And Edge Cases

- Check that unsupported claims were not introduced.

Automated tests are not practical because this is a wording-only review; manual verification is acceptable.
""",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

- Record the manual review outcome.
""",
        encoding="utf-8",
    )

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "blocker: desired behavior tests" not in text
    assert "warning: test exceptions / not-run rationale is missing." not in text


def test_test_contract_does_not_require_security_specific_tests_when_irrelevant(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    _fill_test_inputs(project_tmp, slug)
    contract = _task_path(project_tmp, slug, "test-contract.md")
    contract.write_text(
        contract.read_text(encoding="utf-8").replace(
            "- Unsafe task slugs are rejected.",
            "- Not applicable; this task does not change a material security or trust-boundary surface.",
        ),
        encoding="utf-8",
    )

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: negative / edge-case tests" not in text
    assert "blocker:" not in text


def test_test_contract_warns_for_not_run_without_rationale(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_test_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "verification.md").write_text(
        """# Verification

## Commands And Checks Run

py -m pytest

## Results

- Not run.
""",
        encoding="utf-8",
    )

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: test exceptions / not-run rationale is missing." in text


def test_test_contract_never_overwrites_test_contract_md(project_tmp):
    slug = _start_sample_task(project_tmp)
    test_contract_path = _task_path(project_tmp, slug, "test-contract.md")
    test_contract_path.write_text("custom user-owned test intent\n", encoding="utf-8")

    code, _ = run_test_contract_review(project_tmp, slug, force=True)

    assert code == 0
    assert test_contract_path.read_text(encoding="utf-8") == "custom user-owned test intent\n"


def test_test_contract_dry_run_writes_nothing(project_tmp):
    slug = _start_sample_task(project_tmp)
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_test_contract_review(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
    assert not _review_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_test_contract_rerun_refreshes_manifest_managed_hash_clean_report(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)

    monkeypatch.setattr("ai_sdlc_harness.test_contract._timestamp", lambda: "2026-07-05T00:00:00+00:00")
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    path_text = f".harness/tasks/{slug}/test-contract-review.md"
    first_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    monkeypatch.setattr("ai_sdlc_harness.test_contract._timestamp", lambda: "2026-07-05T00:00:01+00:00")
    code, messages = run_test_contract_review(project_tmp, slug)
    second_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    assert code == 0
    assert f"refresh file {path_text}" in messages
    assert first_hash != second_hash


def test_test_contract_does_not_rewrite_hash_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    report_path = _review_path(project_tmp, slug)
    report_path.write_text("custom report\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_test_contract_review(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/review-tests/test-contract-review.md" in messages
    assert report_path.read_text(encoding="utf-8") == "custom report\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_test_contract_force_overwrites_only_manifest_managed_review(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    report_path = _review_path(project_tmp, slug)
    test_contract_path = _task_path(project_tmp, slug, "test-contract.md")
    user_note = report_path.parent / "user-note.md"
    report_path.write_text("custom report\n", encoding="utf-8")
    test_contract_path.write_text("keep input\n", encoding="utf-8")
    user_note.write_text("keep me\n", encoding="utf-8")

    code, messages = run_test_contract_review(project_tmp, slug, force=True)

    assert code == 0
    assert "refresh file .harness/tasks/review-tests/test-contract-review.md" in messages
    assert "custom report" not in report_path.read_text(encoding="utf-8")
    assert test_contract_path.read_text(encoding="utf-8") == "keep input\n"
    assert user_note.read_text(encoding="utf-8") == "keep me\n"


def test_test_contract_never_overwrites_unmanaged_existing_review(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Blocked report")[0] == 0
    report_path = _review_path(project_tmp, "blocked-report")
    report_path.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_test_contract_review(project_tmp, "blocked-report", force=True)

    assert code == 1
    assert "unmanaged existing file .harness/tasks/blocked-report/test-contract-review.md" in messages
    assert report_path.read_text(encoding="utf-8") == "user owned\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_verify_passes_after_test_contract_review(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_test_contract_review(project_tmp, slug)[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_fails_when_manifest_managed_test_contract_review_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    _review_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/review-tests/test-contract-review.md" in message for message in messages)


def test_verify_fails_when_manifest_managed_test_contract_review_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    _review_path(project_tmp, slug).write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/tasks/review-tests/test-contract-review.md" in message for message in messages)


def test_status_reports_test_contract_review_count_and_remains_read_only(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    unmanaged_task = project_tmp / ".harness" / "tasks" / "unmanaged-task"
    unmanaged_task.mkdir()
    (unmanaged_task / "test-contract-review.md").write_text("not managed\n", encoding="utf-8")
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    tracked.append(_review_path(project_tmp, slug))
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert "manifest-managed test-contract review reports: 1" in messages
    assert before == after


def test_test_contract_complete_noop_preserves_output_and_manifest_bytes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.test_contract._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    output_before = _review_path(project_tmp, slug).read_bytes()
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    code, messages = run_test_contract_review(project_tmp, slug)

    assert code == 0
    assert (
        f"skip unchanged file .harness/tasks/{slug}/test-contract-review.md"
        in messages
    )
    assert _review_path(project_tmp, slug).read_bytes() == output_before
    assert manifest_path.read_bytes() == manifest_before
    assert len(_manifest(project_tmp)["generated_artifact_provenance"]) == 1


def test_test_contract_changed_output_invalidates_before_failed_write(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    monkeypatch.setattr(
        "ai_sdlc_harness.test_contract._timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    monkeypatch.setattr(
        "ai_sdlc_harness.test_contract._timestamp",
        lambda: "2026-07-29T00:00:01+00:00",
    )

    def fail_after_invalidation(_path, _content):
        records = _manifest(project_tmp)["generated_artifact_provenance"]
        assert not any(
            record["output_path"].endswith("/test-contract-review.md")
            for record in records
        )
        raise OSError("simulated review output failure")

    monkeypatch.setattr(
        "ai_sdlc_harness.test_contract.persist_exact_bytes",
        fail_after_invalidation,
    )

    code, messages = run_test_contract_review(project_tmp, slug)

    assert code == 1
    assert "simulated review output failure" in messages[0]
    assert not _manifest(project_tmp)["generated_artifact_provenance"]


def test_successful_test_contract_migrates_v1_and_records_output(project_tmp):
    slug = _start_sample_task(project_tmp)
    legacy = install_released_v1_with_current_task_records(project_tmp)
    managed_before = {
        record["path"]: record for record in legacy["managed_files"]
    }

    assert run_test_contract_review(project_tmp, slug)[0] == 0

    migrated = _manifest(project_tmp)
    assert migrated["manifest_schema_version"] == 2
    after_managed = {
        record["path"]: record for record in migrated["managed_files"]
    }
    for path, record in managed_before.items():
        assert after_managed[path] == record
    assert [
        record["output_path"]
        for record in migrated["generated_artifact_provenance"]
    ] == [f".harness/tasks/{slug}/test-contract-review.md"]


def test_test_contract_preserves_unrelated_preflight_provenance(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    preflight_path = f".harness/tasks/{slug}/preflight.md"
    before = next(
        record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"] == preflight_path
    )

    assert run_test_contract_review(project_tmp, slug)[0] == 0

    after = next(
        record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"] == preflight_path
    )
    assert after == before


def test_test_contract_renders_from_the_exact_captured_dependency_bytes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_test_inputs(project_tmp, slug)
    verification_path = _task_path(project_tmp, slug, "verification.md")
    captured_bytes = verification_path.read_bytes()
    real_capture = test_contract_module.capture_dependencies

    def capture_then_mutate(root, definition):
        captures = real_capture(root, definition)
        verification_path.write_text(
            "# Verification\n\n## Verification Commands\n\nTODO\n",
            encoding="utf-8",
        )
        return captures

    monkeypatch.setattr(
        test_contract_module,
        "capture_dependencies",
        capture_then_mutate,
    )

    assert run_test_contract_review(project_tmp, slug)[0] == 0

    report = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert "- commands and checks run: present=yes; readiness=substantive" in report
    record = next(
        record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"].endswith("/test-contract-review.md")
    )
    verification_dependency = next(
        dependency
        for dependency in record["dependencies"]
        if dependency["dependency_path"].endswith("/verification.md")
    )
    assert verification_dependency["dependency_sha256"] == hashlib.sha256(
        captured_bytes
    ).hexdigest()


def test_test_contract_aborts_when_repository_observations_change(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    real_snapshot = test_contract_module.snapshot_repository_observations
    snapshot_calls = 0

    def snapshot_then_change(root, definition):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 2:
            (project_tmp / "pyproject.toml").write_text(
                "[project]\nname = \"changed\"\n",
                encoding="utf-8",
            )
        return real_snapshot(root, definition)

    monkeypatch.setattr(
        test_contract_module,
        "snapshot_repository_observations",
        snapshot_then_change,
    )

    code, messages = run_test_contract_review(project_tmp, slug)

    assert code == 1
    assert messages == [
        "Repository observations changed during test-contract review; "
        "no files written."
    ]
    assert not _review_path(project_tmp, slug).exists()
    assert manifest_path.read_bytes() == manifest_before


def test_test_contract_rejects_symlinked_managed_output(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    review_path = _review_path(project_tmp, slug)
    target = project_tmp / "review-target.md"
    target.write_text("outside managed output\n", encoding="utf-8")
    _symlink_or_skip(review_path, target)
    manifest_before = (
        project_tmp / ".harness" / "manifest.json"
    ).read_bytes()

    code, messages = run_test_contract_review(
        project_tmp,
        slug,
        force=True,
    )

    assert code == 1
    assert "must not be a symlink" in messages[0]
    assert target.read_text(encoding="utf-8") == "outside managed output\n"
    assert (
        project_tmp / ".harness" / "manifest.json"
    ).read_bytes() == manifest_before


def test_test_contract_checks_mocked_output_safety_before_capture(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    output = _review_path(project_tmp, slug)
    output.write_bytes(b"keep review")
    manifest_path = project_tmp / ".harness" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    def reject_output(*_args, **_kwargs):
        raise PathSafetyError("simulated unsafe managed output")

    def fail_if_captured(*_args, **_kwargs):
        pytest.fail("dependency capture ran after output safety failure")

    monkeypatch.setattr(
        test_contract_module,
        "resolve_managed_output_under_root",
        reject_output,
    )
    monkeypatch.setattr(
        test_contract_module,
        "capture_dependencies",
        fail_if_captured,
    )

    code, messages = run_test_contract_review(
        project_tmp,
        slug,
        force=True,
    )

    assert code == 1
    assert "simulated unsafe managed output" in messages[0]
    assert manifest_path.read_bytes() == manifest_before
    assert output.read_bytes() == b"keep review"


def test_test_contract_final_manifest_failure_leaves_no_output_provenance(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    assert run_preflight(project_tmp, slug)[0] == 0
    preflight_path = f".harness/tasks/{slug}/preflight.md"
    unrelated_before = next(
        record
        for record in _manifest(project_tmp)["generated_artifact_provenance"]
        if record["output_path"] == preflight_path
    )
    monkeypatch.setattr(
        test_contract_module,
        "_timestamp",
        lambda: "2026-07-29T00:00:00+00:00",
    )
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    output_path = f".harness/tasks/{slug}/test-contract-review.md"
    old_managed = _manifest_entry(project_tmp, output_path)
    old_output_hash = hashlib.sha256(
        _review_path(project_tmp, slug).read_bytes()
    ).hexdigest()
    monkeypatch.setattr(
        test_contract_module,
        "_timestamp",
        lambda: "2026-07-29T00:00:01+00:00",
    )
    real_persist = test_contract_module.persist_manifest_model
    persist_calls = 0

    def fail_final_manifest(root, model):
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 2:
            raise OSError("simulated final manifest failure")
        return real_persist(root, model)

    monkeypatch.setattr(
        test_contract_module,
        "persist_manifest_model",
        fail_final_manifest,
    )

    code, messages = run_test_contract_review(project_tmp, slug)

    assert code == 1
    assert "simulated final manifest failure" in messages[0]
    manifest = _manifest(project_tmp)
    assert not any(
        record["output_path"] == output_path
        for record in manifest["generated_artifact_provenance"]
    )
    unrelated_after = next(
        record
        for record in manifest["generated_artifact_provenance"]
        if record["output_path"] == preflight_path
    )
    assert unrelated_after == unrelated_before
    assert _manifest_entry(project_tmp, output_path) == old_managed
    assert hashlib.sha256(
        _review_path(project_tmp, slug).read_bytes()
    ).hexdigest() != old_output_hash
