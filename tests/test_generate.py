from __future__ import annotations

import json
from pathlib import Path

import yaml

from ai_sdlc_harness.generate import run_generate
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.preflight import run_preflight
from ai_sdlc_harness.spec import run_spec
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


def _workset_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "generated" / "agent-workset.md"


def _spec_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "spec.md"


def _requirements_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "requirements.yaml"


def _task_path(root: Path, slug: str, filename: str) -> Path:
    return root / ".harness" / "tasks" / slug / filename


def _manifest(root: Path) -> dict:
    return json.loads((root / ".harness" / "manifest.json").read_text(encoding="utf-8"))


def _manifest_entry(root: Path, path: str) -> dict:
    entries = {entry["path"]: entry for entry in _manifest(root)["managed_files"]}
    return entries[path]


def _start_sample_task(root: Path, title: str = "Generate me") -> str:
    assert init_project(root)[0] == 0
    assert start_task(root, title)[0] == 0
    return "generate-me"


def _fill_task_inputs(root: Path, slug: str) -> None:
    _task_path(root, slug, "task.md").write_text(
        """# Task: Generate me

Task slug: `generate-me`

## Implementation Boundary

Compile deterministic context for this task only.

Keep changes inside `generate.py` and focused tests.

## Assumptions And Open Questions

No blocking open questions remain.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "acceptance.md").write_text(
        """# Acceptance

## Requirements And Acceptance Criteria

- agent-workset.md is created under the task generated folder.
- Readiness reports are summarized when present.

## Protected Behavior And Non-Goals

- Do not modify user-owned task inputs.
- Do not consume post-implementation reports.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "architecture-notes.md").write_text(
        """# Architecture Notes

## Boundary

Keep changes inside harness command handling.

## Responsibility Change

Generate compiles a clearer pre-implementation workset.

## Existing Patterns To Preserve

Keep manifest-managed write behavior and bounded excerpts.

## Interface And Compatibility Impact

The CLI command and options stay unchanged.

## Architecture Hygiene

Use shallow Markdown heading extraction only.

## Security And Privacy Risk Surface

No new security-sensitive behavior is expected.

## Trade-Offs And Open Questions

No architecture trade-off requires a new command.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "coupling-notes.md").write_text(
        """# Coupling Notes

## New Or Changed Coupling

Manifest-managed generated worksets are verified like other managed task outputs.

## Maintainability Sensors

Keep section extraction deterministic and local to generate.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "test-contract.md").write_text(
        """# Test Contract

## Desired Behavior Tests

- Dry run writes nothing.
- Unmanaged worksets are refused.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "verification.md").write_text(
        """# Verification

## Commands And Tests To Run

py -m pytest

## Results

Record results after implementation.

## Manual Review Notes

Confirm the workset is usable before implementation.
""",
        encoding="utf-8",
    )
    _task_path(root, slug, "evidence.md").write_text(
        """# Evidence

## Final Evidence

Record verification results here.

## Generated Or Updated Artifacts

Record generated or updated artifacts here.

## Tests And Checks Run

Record tests and checks run here.

## Results

Record command results here.

## Known Gaps And Risks

Record known gaps and unresolved risks here.

## References

Record relevant references here.
""",
        encoding="utf-8",
    )


def test_generate_fails_before_init(project_tmp):
    code, messages = run_generate(project_tmp, "missing-task")

    assert code == 1
    assert "Run `ai-sdlc init`" in messages[0]


def test_generate_fails_if_task_does_not_exist(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_generate(project_tmp, "missing-task")

    assert code == 1
    assert "Task folder does not exist: .harness/tasks/missing-task" in messages[0]


def test_generate_rejects_unsafe_task_slug(project_tmp):
    assert init_project(project_tmp)[0] == 0

    code, messages = run_generate(project_tmp, "../bad")

    assert code == 2
    assert "Task slug is unsafe" in messages[0]


def test_generate_uses_task_start_title_and_preserves_task_inputs(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Add validation for negative numbers")[0] == 0
    slug = "add-validation-for-negative-numbers"
    tracked = [_task_path(project_tmp, slug, filename) for filename in TASK_FILES]
    before = {path: path.read_bytes() for path in tracked}

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Task title: Add validation for negative numbers" in text
    assert "- Task title: Default Workflow Context" not in text
    assert {path: path.read_bytes() for path in tracked} == before


def test_generate_falls_back_to_slug_title_when_task_title_is_missing(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Add validation for negative numbers")[0] == 0
    slug = "add-validation-for-negative-numbers"
    task = _task_path(project_tmp, slug, "task.md")
    task.write_text(
        """# Task

## Default Workflow Context

This task file no longer carries an explicit title.

## Implementation Boundary

TODO: Describe the expected change area.
""",
        encoding="utf-8",
    )
    before = task.read_bytes()

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- Task title: Add validation for negative numbers" in text
    assert "- Task title: Default Workflow Context" not in text
    assert task.read_bytes() == before


def test_generate_fails_for_missing_required_input(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "acceptance.md").unlink()

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert "Cannot generate agent workset." in messages
    assert any("missing required task input .harness/tasks/generate-me/acceptance.md" in message for message in messages)
    assert not _workset_path(project_tmp, slug).exists()


def test_generate_fails_for_unreadable_required_input(project_tmp):
    slug = _start_sample_task(project_tmp)
    evidence = _task_path(project_tmp, slug, "evidence.md")
    evidence.unlink()
    evidence.mkdir()

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert any("unreadable required task input .harness/tasks/generate-me/evidence.md" in message for message in messages)
    assert not _workset_path(project_tmp, slug).exists()


def test_generate_creates_workset_with_context_warnings_packs_and_signals(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    (project_tmp / "pyproject.toml").write_text("[project]\nname = \"sample\"\n", encoding="utf-8")
    (project_tmp / "tests").mkdir()
    _task_path(project_tmp, slug, "evidence-report.md").write_text(
        "- blocker: post-implementation evidence report must not be consumed.\n",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "validation-report.md").write_text(
        "- blocker: post-implementation validation report must not be consumed.\n",
        encoding="utf-8",
    )

    code, messages = run_generate(project_tmp, slug)

    path = _workset_path(project_tmp, slug)
    text = path.read_text(encoding="utf-8")
    assert code == 0
    assert f"agent workset: .harness/tasks/{slug}/generated/agent-workset.md" in messages
    assert path.is_file()
    assert "Task slug: `generate-me`" in text
    assert (
        "This workset compiles task-scoped context for implementation. It does not call AI models, "
        "generate code, generate tests, run tests, inspect target code deeply, scan security, validate compliance, "
        "prove correctness, security, or release readiness, enforce packs, or install adapters."
    ) in text
    assert "semantic enforcement" not in text.lower()
    for heading in (
        "## Summary",
        "## Implementation Contract",
        "## Specification",
        "## Structured Requirements",
        "## Task Boundary",
        "## Requirements / Acceptance Criteria",
        "## Architecture / Coupling Notes",
        "## Security / Privacy Risk-Surface Notes",
        "## Preflight Signals",
        "## Test-Contract Signals",
        "## Verification Expectations",
        "## Evidence Expectations",
        "## Optional Packs",
        "## Repository Signals",
        "## Implementation Instructions",
        "## Human Review Notes",
        "## Disclaimer",
    ):
        assert heading in text
    assert "- Task title: Generate me" in text
    assert "Source: `task.md`" in text
    assert "### Implementation Boundary" in text
    assert "warning: workset context report `spec.md` has not been generated." in text
    assert "warning: structured requirements `requirements.yaml` has not been generated." in text
    assert "Keep changes inside `generate.py` and focused tests." in text
    assert "### Assumptions And Open Questions" in text
    assert "### Requirements And Acceptance Criteria" in text
    assert "### Protected Behavior And Non-Goals" in text
    assert "Do not consume post-implementation reports." in text
    assert "### Interface And Compatibility Impact" in text
    assert "### Security And Privacy Risk Surface" in text
    assert "### Maintainability Sensors" in text
    assert "### Commands And Tests To Run" in text
    assert "### Final Evidence" in text
    assert "### Tests And Checks Run" in text
    assert "### Known Gaps And Risks" in text
    assert "warning: workset context report `preflight.md` has not been generated." in text
    assert "warning: workset context report `test-contract-review.md` has not been generated." in text
    assert "No optional packs selected. The foundation workflow still applies." in text
    assert "architecture-generic" not in text
    assert "coupling-baseline" not in text
    assert "observability-baseline" not in text
    assert "security-baseline" not in text
    assert "Post-implementation reports are not implementation inputs: `evidence-report.md` and `validation-report.md` are not consumed." in text
    assert "post-implementation evidence report must not be consumed" not in text
    assert "post-implementation validation report must not be consumed" not in text
    assert "Detected languages: python" in text
    assert "Detected test frameworks: tests-directory-or-pytest" in text
    assert "- Implement only the task described by the task artifacts." in text
    assert "- Stay within the task boundary." in text
    assert "- Preserve protected areas, non-goals, and existing interfaces unless explicitly changed in the task artifacts." in text
    assert "- Do not silently expand scope." in text
    assert "- Implement only after blocker findings are resolved or explicitly accepted by the human." in text
    assert "- Record deviations, commands run, results, not-run rationale, gaps, and references in `evidence.md` and `verification.md`." in text
    assert "- [ ] Protected areas, interface impact, and unresolved questions reviewed." in text
    assert "- [ ] Security/privacy risk-surface notes reviewed where applicable." in text
    assert "- [ ] Verification commands and results recorded in `verification.md` or `evidence.md`." in text
    assert "- [ ] Any blocker findings were resolved or explicitly accepted by the human." in text


def test_generate_includes_spec_when_present(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_spec(project_tmp, slug)[0] == 0

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    requirements = yaml.safe_load(_requirements_path(project_tmp, slug).read_text(encoding="utf-8"))
    assert code == 0
    assert "## Specification" in text
    assert "Source: `spec.md`" in text
    assert "- Status: present and readable." in text
    assert "Spec question: Is the implementation intent clear enough to generate a useful workset and guide a coding agent?" in text
    assert "warning: workset context report `spec.md` has not been generated." not in text
    assert "## Structured Requirements" in text
    assert "Source: `requirements.yaml`" in text
    assert "Status: present, readable, and schema valid." in text
    assert "- Artifact role: advisory projection." in text
    assert "- Authority: non-authoritative." in text
    assert "- Edit model: edit source task artifacts and rerun `ai-sdlc spec --task <slug>`." in text
    assert "not the authoritative source of truth." in text
    assert "`REQ-001` [draft]: agent-workset.md is created under the task generated folder." in text
    assert "acceptance_criteria: []" in text
    assert "verification: []" in text
    assert _spec_path(project_tmp, slug).is_file()
    assert requirements["schema_version"] == 1
    assert requirements["artifact_role"] == "advisory_projection"
    assert requirements["authority"] == "non_authoritative"
    assert requirements["edit_model"] == "edit_source_task_artifacts_and_rerun_spec"

    entry = _manifest_entry(project_tmp, f".harness/tasks/{slug}/generated/agent-workset.md")
    assert entry["protected"] is True
    assert entry["hash_algorithm"] == "sha256"
    assert entry["sha256"]


def test_generate_warns_for_malformed_or_invalid_requirements_without_failing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _requirements_path(project_tmp, slug).write_text("not: [valid\n", encoding="utf-8")

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: structured requirements `requirements.yaml` is invalid:" in text
    assert "malformed YAML" in text

    _workset_path(project_tmp, slug).unlink()
    _requirements_path(project_tmp, slug).write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "artifact_role": "advisory_projection",
                "authority": "non_authoritative",
                "edit_model": "edit_source_task_artifacts_and_rerun_spec",
                "task_slug": slug,
                "source_model": "deterministic_acceptance_markdown_projection",
                "source_artifacts": [{"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"}],
                "requirements": [{"id": "REQ-001", "statement": "Do thing.", "status": "accepted"}],
                "findings": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug, force=True)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: structured requirements `requirements.yaml` is invalid:" in text
    assert "invalid requirement status" in text

    _workset_path(project_tmp, slug).unlink()
    _requirements_path(project_tmp, slug).write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "authority": "non_authoritative",
                "edit_model": "edit_source_task_artifacts_and_rerun_spec",
                "task_slug": slug,
                "source_model": "deterministic_acceptance_markdown_projection",
                "source_artifacts": [{"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"}],
                "requirements": [],
                "findings": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug, force=True)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: structured requirements `requirements.yaml` is invalid:" in text
    assert "artifact_role must be advisory_projection." in text

    _workset_path(project_tmp, slug).unlink()
    _requirements_path(project_tmp, slug).write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "artifact_role": "advisory_projection",
                "authority": "authoritative",
                "edit_model": "edit_source_task_artifacts_and_rerun_spec",
                "task_slug": slug,
                "source_model": "deterministic_acceptance_markdown_projection",
                "source_artifacts": [{"path": "acceptance.md", "section": "Requirements And Acceptance Criteria"}],
                "requirements": [],
                "findings": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug, force=True)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: structured requirements `requirements.yaml` is invalid:" in text
    assert "authority must be non_authoritative." in text


def test_generate_includes_preflight_and_test_contract_review_summaries(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_preflight(project_tmp, slug)[0] == 0
    assert run_test_contract_review(project_tmp, slug)[0] == 0

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "## Preflight Signals" in text
    assert "Source: `preflight.md`" in text
    assert "## Test-Contract Signals" in text
    assert "Source: `test-contract-review.md`" in text
    assert "- Status: present and readable." in text
    assert "- warning:" in text
    assert "- info:" in text
    assert "workset context report `preflight.md` has not been generated." not in text


def test_generate_surfaces_prior_report_findings_with_lowercase_prefixes(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "preflight.md").write_text(
        """# Preflight

- blocker: boundary must be accepted.
- warning: open question remains.
- info: preflight uses shallow checks only.
""",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "test-contract-review.md").write_text(
        """# Test Contract Review

- blocker: desired behavior tests are missing.
- warning: no CI detected.
- info: no tests were run by this command.
""",
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "- blocker: boundary must be accepted." in text
    assert "- warning: open question remains." in text
    assert "- info: preflight uses shallow checks only." in text
    assert "- blocker: desired behavior tests are missing." in text
    assert "- warning: no CI detected." in text
    assert "- info: no tests were run by this command." in text


def test_generate_lists_legacy_selected_pack_records_without_enforcement_claims(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    (project_tmp / ".harness" / "packs" / "selected.yaml").write_text(
        """selected_packs:
  - id: security-baseline
    version: legacy
    enabled: true
  - id: architecture-generic
    version: legacy
    enabled: false
""",
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "Selected pack records are listed for context only. The workset does not imply pack enforcement." in text
    assert "- selected record: security-baseline (version: legacy, enabled: yes)" in text
    assert "- selected record: architecture-generic (version: legacy, enabled: no)" in text
    assert "No optional packs selected. The foundation workflow still applies." not in text
    assert "security-baseline is active" not in text
    assert "architecture-generic is active" not in text
    assert "enforced" not in text.lower()


def test_generate_warns_for_missing_and_todo_only_extracted_sections(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    _task_path(project_tmp, slug, "architecture-notes.md").write_text(
        """# Architecture Notes

## Boundary

TODO: identify the affected boundary.

## Security And Privacy Risk Surface

Not applicable.
""",
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "warning: workset context section `Boundary` in `architecture-notes.md` is TODO-only." in text
    assert "warning: workset context section `Responsibility Change` is missing from `architecture-notes.md`." in text
    assert "warning: workset context section `Existing Patterns To Preserve` is missing from `architecture-notes.md`." in text
    assert "Not applicable." in text


def test_generate_redacts_secrets_omits_environment_lines_and_truncates(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    long_text = "\n".join(f"line {index}" for index in range(80))
    fake_secret = "sk-" + "abcdefghijklmnop"
    fake_env_line = "API_" + "TOKEN" + "=" + "token-" + "abcdefghijklmnop"
    _task_path(project_tmp, slug, "task.md").write_text(
        f"# Task\n\n## Implementation Boundary\n\n{fake_env_line}\nPath note: C:\\Temp\\owned\n{fake_secret}\n{long_text}\n",
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert fake_env_line not in text
    assert fake_secret not in text
    assert "[REDACTED]" in text
    assert "API_TOKEN=" not in text
    assert "environment-style line(s) omitted" in text
    assert "Path note: C:\\Temp\\owned" in text
    assert "Excerpt truncated; see the source artifact for full content." in text


def test_generate_does_not_overwrite_user_editable_inputs_or_static_agent_instructions(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    task_inputs_before = {filename: _task_path(project_tmp, slug, filename).read_bytes() for filename in TASK_FILES}
    static_agent_instructions = project_tmp / ".harness" / "generated" / "agent-instructions.md"
    static_before = static_agent_instructions.read_bytes()

    code, messages = run_generate(project_tmp, slug, force=True)

    task_inputs_after = {filename: _task_path(project_tmp, slug, filename).read_bytes() for filename in TASK_FILES}
    assert code == 0
    assert task_inputs_before == task_inputs_after
    assert static_agent_instructions.read_bytes() == static_before
    assert "root AGENTS.md and CLAUDE.md were not modified" in messages
    assert ".harness/generated/agent-instructions.md was not modified" in messages


def test_generate_dry_run_writes_nothing_and_preserves_manifest(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_generate(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
    assert not _workset_path(project_tmp, slug).exists()
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_generate_rerun_refreshes_manifest_managed_hash_clean_workset(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)

    monkeypatch.setattr("ai_sdlc_harness.generate._timestamp", lambda: "2026-07-08T00:00:00+00:00")
    assert run_generate(project_tmp, slug)[0] == 0
    path_text = f".harness/tasks/{slug}/generated/agent-workset.md"
    first_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    monkeypatch.setattr("ai_sdlc_harness.generate._timestamp", lambda: "2026-07-08T00:00:01+00:00")
    code, messages = run_generate(project_tmp, slug)
    second_hash = _manifest_entry(project_tmp, path_text)["sha256"]

    assert code == 0
    assert f"refresh file {path_text}" in messages
    assert first_hash != second_hash


def test_generate_skips_manifest_refresh_when_content_is_byte_identical(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr("ai_sdlc_harness.generate._timestamp", lambda: "2026-07-08T00:00:00+00:00")
    assert run_generate(project_tmp, slug)[0] == 0
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_generate(project_tmp, slug)

    assert code == 0
    assert "skip unchanged file .harness/tasks/generate-me/generated/agent-workset.md" in messages
    assert "skip existing manifest .harness/manifest.json" in messages
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_generate_does_not_rewrite_hash_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    workset = _workset_path(project_tmp, slug)
    workset.write_text("custom workset\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/generate-me/generated/agent-workset.md" in messages
    assert workset.read_text(encoding="utf-8") == "custom workset\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_generate_force_overwrites_only_manifest_managed_workset(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    workset = _workset_path(project_tmp, slug)
    user_note = workset.parent / "user-note.md"
    workset.write_text("custom workset\n", encoding="utf-8")
    user_note.write_text("keep me\n", encoding="utf-8")

    code, messages = run_generate(project_tmp, slug, force=True)

    assert code == 0
    assert "refresh file .harness/tasks/generate-me/generated/agent-workset.md" in messages
    assert "custom workset" not in workset.read_text(encoding="utf-8")
    assert user_note.read_text(encoding="utf-8") == "keep me\n"


def test_generate_never_overwrites_unmanaged_existing_workset(project_tmp):
    assert init_project(project_tmp)[0] == 0
    assert start_task(project_tmp, "Blocked workset")[0] == 0
    slug = "blocked-workset"
    _fill_task_inputs(project_tmp, slug)
    workset = _workset_path(project_tmp, slug)
    workset.parent.mkdir()
    workset.write_text("user owned\n", encoding="utf-8")
    manifest_before = (project_tmp / ".harness" / "manifest.json").read_bytes()

    code, messages = run_generate(project_tmp, slug, force=True)

    assert code == 1
    assert "unmanaged existing file .harness/tasks/blocked-workset/generated/agent-workset.md" in messages
    assert workset.read_text(encoding="utf-8") == "user owned\n"
    assert (project_tmp / ".harness" / "manifest.json").read_bytes() == manifest_before


def test_verify_passes_after_generate(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_verify_fails_when_manifest_managed_workset_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    _workset_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("missing file .harness/tasks/generate-me/generated/agent-workset.md" in message for message in messages)


def test_verify_fails_when_manifest_managed_workset_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    _workset_path(project_tmp, slug).write_text("changed\n", encoding="utf-8")

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert any("hash drift detected for .harness/tasks/generate-me/generated/agent-workset.md" in message for message in messages)


def test_verify_does_not_require_every_task_to_have_workset(project_tmp):
    _start_sample_task(project_tmp)

    code, messages = verify_project(project_tmp)

    assert code == 0
    assert messages == ["AI SDLC Harness verification passed"]


def test_status_reports_manifest_managed_workset_count_and_remains_read_only(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    unmanaged_task = project_tmp / ".harness" / "tasks" / "unmanaged-task" / "generated"
    unmanaged_task.mkdir(parents=True)
    (unmanaged_task / "agent-workset.md").write_text("not managed\n", encoding="utf-8")
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    tracked.append(_workset_path(project_tmp, slug))
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)
    after = {path: path.read_bytes() for path in tracked}

    assert code == 0
    assert "manifest-managed generated worksets: 1" in messages
    assert before == after
