from __future__ import annotations

import json
from pathlib import Path

from ai_sdlc_harness.evidence import run_evidence
from ai_sdlc_harness.generate import run_generate
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.preflight import run_preflight
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


def _task_path(root: Path, slug: str, filename: str) -> Path:
    return root / ".harness" / "tasks" / slug / filename


def _workset_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "generated" / "agent-workset.md"


def _report_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "evidence-report.md"


def _manifest(root: Path) -> dict:
    return json.loads((root / ".harness" / "manifest.json").read_text(encoding="utf-8"))


def _manifest_entry(root: Path, path: str) -> dict:
    entries = {entry["path"]: entry for entry in _manifest(root)["managed_files"]}
    return entries[path]


def _start_sample_task(root: Path, title: str = "Evidence me") -> str:
    assert init_project(root)[0] == 0
    assert start_task(root, title)[0] == 0
    return "evidence-me"


def _fill_task_inputs(root: Path, slug: str) -> None:
    _task_path(root, slug, "task.md").write_text(
        """# Task: Evidence me

Task slug: `evidence-me`

## Scope

Summarize recorded evidence only.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- evidence-report.md is created for the selected task.
- The report summarizes existing task artifacts.

## Protected Behavior And Non-Goals

- Existing manifest ownership behavior is preserved.
- The evidence command does not run tests.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "architecture-notes.md").write_text(
        """# Architecture Notes

## Boundary

Keep this inside harness command handling.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "coupling-notes.md").write_text(
        """# Coupling Notes

## New Or Changed Coupling

The report is manifest-managed like other task outputs.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "test-contract.md").write_text(
        """# Test Contract

## Characterization Tests

- Existing report ownership behavior remains non-destructive.

## Desired Behavior Tests

- Evidence report summarizes task artifacts.

## Regression Tests

- Unmanaged reports are refused.

## Negative And Edge Cases

- Missing evidence notes are blockers in the report.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run Later

py -m pytest -p no:cacheprovider

## Results

Tests passed in 1.00s.

## Commands Not Run And Why

- None.

## Manual Review Notes

- Reviewed the generated report for deterministic wording and manifest ownership.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

Implemented deterministic evidence summarization.

Why it changed: to give human reviewers a clearer trace of implementation evidence.

Requirements addressed: evidence-report.md is created and summarizes existing task artifacts.

## Generated Or Updated Artifacts

- src/ai_sdlc_harness/evidence.py

## Tests And Checks Run

- py -m pytest -p no:cacheprovider

## Results

- Tests passed in 1.00s.

## Known Gaps And Risks

- Deviations: none.
- Known gaps: none.
- Unresolved risks: none.
- Risk acceptances: none.

## References

- .harness/tasks/evidence-me/evidence-report.md
""",
        encoding="utf-8",
    )


def test_evidence_fails_before_init(project_tmp):
    code, messages = run_evidence(project_tmp, "missing-task")

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_evidence_fails_if_task_does_not_exist(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_evidence(project_tmp, "missing-task")

    assert code == 1
    assert "Task folder does not exist: .harness/tasks/missing-task" in messages[0]


def test_evidence_rejects_unsafe_task_slug(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_evidence(project_tmp, "../bad")

    assert code == 2
    assert "Task slug is unsafe" in messages[0]


def test_evidence_creates_report_with_readiness_signals_and_manifest_entry(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_preflight(project_tmp, slug)[0] == 0
    assert run_test_contract_review(project_tmp, slug)[0] == 0
    assert run_generate(project_tmp, slug)[0] == 0

    code, messages = run_evidence(project_tmp, slug)

    path = _report_path(project_tmp, slug)
    text = path.read_text(encoding="utf-8")
    assert code == 0
    assert f"evidence report: .harness/tasks/{slug}/evidence-report.md" in messages
    assert path.is_file()
    assert "Task slug: `evidence-me`" in text
    assert (
        "This report summarizes task-scoped implementation and review trace for human review. It does not run tests, "
        "inspect target code deeply, prove correctness, prove security, validate compliance, scan for security issues, "
        "enforce packs, execute packs, generate tests, perform audit validation, or call AI models."
    ) in text
    assert "semantic enforcement" not in text.lower()
    for heading in (
        "## Summary",
        "## Implementation Trace",
        "## Requirements / Acceptance Evidence",
        "## Boundary / Protected Behavior Evidence",
        "## Test / Verification Evidence",
        "## Deviations, Gaps, And Risks",
        "## Supporting Artifacts",
        "## Prior Report Signals",
        "## Findings",
        "## Recommended Next Actions",
        "## Disclaimer",
    ):
        assert heading in text
    assert "`task.md`: present=yes; readable=yes" in text
    assert "`preflight.md`: present=yes; readable=yes" in text
    assert "`test-contract-review.md`: present=yes; readable=yes" in text
    assert "`generated/agent-workset.md`: present=yes; readable=yes" in text
    assert "- `evidence.md`: substantive" in text
    assert "- `verification.md`: substantive" in text
    assert "- Verification command: present" in text
    assert "- Test/check result evidence: present" in text
    assert "- What changed: substantive in `evidence.md`" in text
    assert "- Requirements / acceptance criteria: substantive in `acceptance.md`" in text
    assert "- Protected behavior / non-goals: substantive in `acceptance.md`" in text
    assert "- Files or artifacts updated: substantive in `evidence.md`" in text
    assert "- Tests/checks run: substantive in `evidence.md`" in text
    assert "- Test/check results: substantive in `evidence.md`" in text
    assert (
        "- Commands not run and why: substantive in `evidence.md`" in text
        or "- Commands not run and why: substantive in `verification.md`" in text
    )
    assert "- Manual review notes: substantive in `verification.md`" in text
    assert "- Known gaps and risks: substantive in `evidence.md`" in text
    assert "- References or links: substantive in `evidence.md`" in text
    assert "- info: harness did not run tests." in text
    assert "prove correctness" in text
    assert "compliant" not in text.lower()

    entry = _manifest_entry(project_tmp, f".harness/tasks/{slug}/evidence-report.md")
    assert entry["protected"] is True
    assert entry["hash_algorithm"] == "sha256"
    assert entry["sha256"]


def test_evidence_generates_blockers_for_missing_empty_or_todo_evidence_inputs(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "evidence.md").write_text(
        "# Evidence\n\n## Final Evidence\n\nTODO: Record results.\n",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "verification.md").unlink()

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "blocker: verification.md is missing." in text
    assert "warning: evidence.md is TODO-only." in text
    assert "blocker: evidence.md and verification.md both have no substantive content." in text


def test_evidence_generates_report_for_empty_evidence_inputs(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "evidence.md").write_text("", encoding="utf-8")
    _task_path(project_tmp, slug, "verification.md").write_text("", encoding="utf-8")

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- `evidence.md`: empty" in text
    assert "- `verification.md`: empty" in text
    assert "blocker: evidence.md and verification.md both have no substantive content." in text
    assert "blocker: implementation trace for what changed is missing section in `evidence.md`." in text


def test_evidence_generates_report_for_unreadable_evidence_input(project_tmp):
    slug = _start_sample_task(project_tmp)
    evidence = _task_path(project_tmp, slug, "evidence.md")
    evidence.unlink()
    evidence.mkdir()

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "blocker: evidence.md is unreadable." in text


def test_evidence_blocks_missing_what_changed_trace(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "evidence.md").write_text(
        """# Evidence

## Generated Or Updated Artifacts

- src/ai_sdlc_harness/evidence.py

## Tests And Checks Run

- py -m pytest -p no:cacheprovider

## Results

- Tests passed in 1.00s.

## Known Gaps And Risks

- None.

## References

- evidence-report.md
""",
        encoding="utf-8",
    )

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "blocker: implementation trace for what changed is missing section in `evidence.md`." in text


def test_evidence_warns_for_missing_results_manual_review_and_references(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

Implemented evidence trace updates because reviewers need a clearer summary.

## Generated Or Updated Artifacts

- src/ai_sdlc_harness/evidence.py

## Tests And Checks Run

- py -m pytest -p no:cacheprovider

## Known Gaps And Risks

- Deviations: none.
- Known gaps: none.
- Unresolved risks: none.
- Risk acceptances: none.
""",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run Later

py -m pytest -p no:cacheprovider

## Commands Not Run And Why

- None.
""",
        encoding="utf-8",
    )

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: test/check results are missing section in `evidence.md`." in text
    assert "warning: manual review notes are missing section in `verification.md`." in text
    assert "warning: references or supporting artifacts are missing section in `evidence.md`." in text


def test_evidence_supports_legacy_evidence_headings(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "evidence.md").write_text(
        """# Review Evidence

Implemented evidence report updates because human review needs traceability.

## Changed Artifacts

- src/ai_sdlc_harness/evidence.py

## Tests

- py -m pytest -p no:cacheprovider

## Verification Results

- Tests passed in 1.00s.

## Known Gaps

- Deviations: none.
- Known gaps: none.
- Unresolved risks: none.
- Risk acceptances: none.

## References

- evidence-report.md
""",
        encoding="utf-8",
    )

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- What changed: substantive in `evidence.md`" in text
    assert "- Files or artifacts updated: substantive in `evidence.md`" in text
    assert "- Tests/checks run: substantive in `evidence.md`" in text
    assert "- Test/check results: substantive in `evidence.md`" in text
    assert "- Known gaps and risks: substantive in `evidence.md`" in text
    assert "blocker: implementation trace for what changed" not in text


def test_evidence_warns_for_not_run_without_rationale(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run Later

py -m pytest -p no:cacheprovider

## Results

Tests passed in 1.00s.

## Commands Not Run And Why

- Integration tests not run.

## Manual Review Notes

- Reviewed generated report shape.
""",
        encoding="utf-8",
    )

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: not-run or skipped commands are recorded without rationale." in text


def test_evidence_accepts_not_run_with_rationale(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run Later

py -m pytest -p no:cacheprovider

## Results

Tests passed in 1.00s.

## Commands Not Run And Why

- Integration tests not run because this change only affects deterministic report text.

## Manual Review Notes

- Reviewed generated report shape.
""",
        encoding="utf-8",
    )

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: not-run or skipped commands are recorded without rationale." not in text


def test_evidence_copies_prior_report_findings_as_warnings(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "preflight.md").write_text(
        "- blocker: implementation boundary is missing.\n",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "test-contract-review.md").write_text(
        "- warning: expected checks are unclear.\n",
        encoding="utf-8",
    )

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "`preflight.md`: blocker: implementation boundary is missing." in text
    assert "`test-contract-review.md`: warning: expected checks are unclear." in text
    assert "warning: preflight.md reported blocker: implementation boundary is missing." in text
    assert "warning: test-contract-review.md reported warning: expected checks are unclear." in text


def test_evidence_warns_when_generated_workset_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: generated/agent-workset.md is missing." in text


def test_evidence_scans_todos_without_self_reference(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "acceptance.md").write_text(
        "# Acceptance\n\n## Acceptance Criteria\n\nTODO: refine criteria.\n",
        encoding="utf-8",
    )

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "Unresolved TODOs: present in acceptance.md" in text
    assert "present in evidence-report.md" not in text


def test_evidence_does_not_overwrite_task_inputs_or_global_generated_files(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    (project_tmp / "AGENTS.md").write_text("existing agents\n", encoding="utf-8")
    (project_tmp / "CLAUDE.md").write_text("existing claude\n", encoding="utf-8")
    tracked = {
        **{_task_path(project_tmp, slug, filename): _task_path(project_tmp, slug, filename).read_bytes() for filename in TASK_FILES},
        project_tmp / "AGENTS.md": (project_tmp / "AGENTS.md").read_bytes(),
        project_tmp / "CLAUDE.md": (project_tmp / "CLAUDE.md").read_bytes(),
        project_tmp / ".harness" / "generated" / "agent-instructions.md": (
            project_tmp / ".harness" / "generated" / "agent-instructions.md"
        ).read_bytes(),
        _workset_path(project_tmp, slug): _workset_path(project_tmp, slug).read_bytes(),
    }

    code, messages = run_evidence(project_tmp, slug, force=True)

    assert code == 0
    assert {path: path.read_bytes() for path in tracked} == tracked
    assert "root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified" in messages
    assert ".harness/generated/agent-instructions.md was not modified" in messages
    assert ".harness/tasks/evidence-me/generated/agent-workset.md was not modified" in messages


def test_evidence_dry_run_writes_nothing_and_preserves_manifest(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_evidence(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
    assert not _report_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_evidence_rerun_refreshes_manifest_managed_clean_report(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)

    monkeypatch.setattr("ai_sdlc_harness.evidence._timestamp", lambda: "2026-07-08T00:00:00+00:00")
    assert run_evidence(project_tmp, slug)[0] == 0
    path_text = f".harness/tasks/{slug}/evidence-report.md"
    first_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    monkeypatch.setattr("ai_sdlc_harness.evidence._timestamp", lambda: "2026-07-08T00:00:01+00:00")
    code, messages = run_evidence(project_tmp, slug)
    second_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    assert code == 0
    assert f"refresh file {path_text}" in messages
    assert first_hash != second_hash


def test_evidence_skips_manifest_refresh_when_content_is_byte_identical(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    path_text = f".harness/tasks/{slug}/evidence-report.md"
    monkeypatch.setattr("ai_sdlc_harness.evidence._timestamp", lambda: "2026-07-08T00:00:00+00:00")
    assert run_evidence(project_tmp, slug)[0] == 0
    assert run_evidence(project_tmp, slug)[0] == 0
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_evidence(project_tmp, slug)

    assert code == 0
    assert f"skip unchanged file {path_text}" in messages
    assert "skip existing manifest .harness/manifest.json" in messages
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_evidence_does_not_rewrite_hash_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_evidence(project_tmp, slug)[0] == 0
    report = _report_path(project_tmp, slug)
    report.write_text("custom report\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_evidence(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/evidence-me/evidence-report.md" in messages
    assert report.read_text(encoding="utf-8") == "custom report\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_evidence_force_overwrites_only_manifest_managed_report(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_evidence(project_tmp, slug)[0] == 0
    report = _report_path(project_tmp, slug)
    user_note = report.parent / "user-note.md"
    report.write_text("custom report\n", encoding="utf-8")
    user_note.write_text("keep me\n", encoding="utf-8")

    code, messages = run_evidence(project_tmp, slug, force=True)

    assert code == 0
    assert "refresh file .harness/tasks/evidence-me/evidence-report.md" in messages
    assert "custom report" not in report.read_text(encoding="utf-8")
    assert user_note.read_text(encoding="utf-8") == "keep me\n"


def test_evidence_reports_other_manifest_drift_without_repairing_it(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    acceptance_path = ".harness/tasks/evidence-me/acceptance.md"
    original_hash = _manifest_entry(project_tmp, acceptance_path)["sha256"]
    _task_path(project_tmp, slug, "acceptance.md").write_text("changed acceptance\n", encoding="utf-8")

    code, _ = run_evidence(project_tmp, slug)

    text = _report_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert f"`{acceptance_path}`: status=present and readable; hash=hash drift" in text
    assert f"blocker: manifest-managed task artifact has hash drift: {acceptance_path}." in text
    assert _manifest_entry(project_tmp, acceptance_path)["sha256"] == original_hash


def test_evidence_never_overwrites_unmanaged_existing_report(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Blocked evidence")[0] == 0
    slug = "blocked-evidence"
    report = _report_path(project_tmp, slug)
    report.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_evidence(project_tmp, slug, force=True)

    assert code == 1
    assert "unmanaged existing file .harness/tasks/blocked-evidence/evidence-report.md" in messages
    assert report.read_text(encoding="utf-8") == "user owned\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_verify_passes_after_evidence_report_creation(project_tmp):
    slug = _start_sample_task(project_tmp)
    assert run_evidence(project_tmp, slug)[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_fails_when_manifest_managed_evidence_report_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_evidence(project_tmp, slug)[0] == 0
    _report_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/evidence-me/evidence-report.md" in message for message in messages)


def test_verify_fails_when_manifest_managed_evidence_report_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_evidence(project_tmp, slug)[0] == 0
    _report_path(project_tmp, slug).write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/tasks/evidence-me/evidence-report.md" in message for message in messages)


def test_verify_does_not_require_every_task_to_have_evidence_report(project_tmp):
    _start_sample_task(project_tmp)

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_status_reports_manifest_managed_evidence_report_count_and_remains_read_only(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_evidence(project_tmp, slug)[0] == 0
    unmanaged_task = project_tmp / ".harness" / "tasks" / "unmanaged-task"
    unmanaged_task.mkdir(parents=True)
    (unmanaged_task / "evidence-report.md").write_text("not managed\n", encoding="utf-8")
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    tracked.append(_report_path(project_tmp, slug))
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert "manifest-managed evidence reports: 1" in messages
    assert before == after
