from __future__ import annotations

import json
from pathlib import Path

from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.status import status_project
from ai_sdlc_harness.task import start_task
from ai_sdlc_harness.test_contract import run_test_contract_review
from ai_sdlc_harness.verify import verify_project


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

## Commands And Tests To Run

py -m pytest

## Results

- Expected result: focused tests pass after implementation.

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
        "## Verification Readiness",
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
    assert "- verification commands: present=yes; readiness=substantive" in text
    assert "- expected results: present=yes; readiness=substantive" in text
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
    assert "- verification commands: present=yes; readiness=substantive" in text
    assert "- expected results: present=yes; readiness=substantive" in text
    assert "- manual review checks: present=yes; readiness=substantive" in text
    assert "- evidence expectations: present=yes; readiness=substantive" in text
    assert "blocker:" not in text


def test_test_contract_warns_for_missing_ci_framework_and_unfilled_sections(project_tmp):
    slug = _start_sample_task(project_tmp)

    code, _ = run_test_contract_review(project_tmp, slug)

    text = _review_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: no test framework detected." in text
    assert "warning: no CI detected." in text
    assert "warning: protected behavior / non-goals are TODO-only." in text
    assert "warning: characterization tests are TODO-only." in text
    assert "warning: regression tests are TODO-only." in text
    assert "warning: negative / edge-case tests are TODO-only." in text
    assert "warning: verification commands are missing." in text
    assert "warning: expected results or manual review checks are missing." in text
    assert "warning: evidence expectations are TODO-only." in text
    assert "blocker: requirements / acceptance criteria are TODO-only." in text
    assert "blocker: desired behavior tests are TODO-only." in text
    assert "blocker: no usable acceptance or test-contract content is available." in text


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
    _task_path(project_tmp, slug, "verification.md").write_text(
        """# Verification

## Manual Review Notes

- Manually review the changed wording and confirm no unsupported claims.
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


def test_test_contract_warns_for_not_run_without_rationale(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_test_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run

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
