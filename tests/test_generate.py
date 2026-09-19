from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
import yaml

import ai_sdlc_harness.context as context_module
import ai_sdlc_harness.generate as generate_module
from ai_sdlc_harness.context import (
    BudgetConfiguration,
    BudgetStatus,
    ContextFinding,
    FindingLevel,
    FreshnessState,
    RepositoryObservations,
    parse_context_manifest_yaml,
    render_context_manifest_yaml,
)
from ai_sdlc_harness.files import sha256_bytes
from ai_sdlc_harness.generate import (
    GenerationPreparation,
    _build_context_manifest,
    _canonical_findings,
    _load_budget_configuration,
    _prepare_generation,
    _render_workset,
    _repository_observations,
    run_generate,
)
from ai_sdlc_harness.init import init_project
from ai_sdlc_harness.manifest import write_manifest
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


def _context_manifest_path(root: Path, slug: str) -> Path:
    return root / ".harness" / "tasks" / slug / "generated" / "context-manifest.yaml"


def _task_path(root: Path, slug: str, filename: str) -> Path:
    return root / ".harness" / "tasks" / slug / filename


def _manifest_path(root: Path) -> Path:
    return root / ".harness" / "manifest.json"


def _manifest(root: Path) -> dict:
    return json.loads(_manifest_path(root).read_text(encoding="utf-8"))


def _manifest_entry(root: Path, path: str) -> dict:
    entries = {entry["path"]: entry for entry in _manifest(root)["managed_files"]}
    return entries[path]


def _write_manifest_data(root: Path, data: dict) -> None:
    _manifest_path(root).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


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

Use shallow deterministic selection only.

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

Keep selection deterministic and local to generate orchestration.
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
    write_manifest(root)


def _prepared_project(root: Path) -> tuple[str, GenerationPreparation]:
    slug = _start_sample_task(root)
    _fill_task_inputs(root, slug)
    return slug, _prepare_generation(root, slug)


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


def test_preparation_calls_lineage_detection_and_selector_once_with_exact_values(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    original_resolve = generate_module.resolve_derived_freshness
    original_select = context_module.select_context
    original_detect = generate_module.detect_project_signals
    calls = {"lineage": 0, "select": 0, "detect": 0}
    captured: dict[str, object] = {}

    def wrapped_resolve(root, task_slug):
        calls["lineage"] += 1
        result = original_resolve(root, task_slug)
        captured["freshness"] = result.derived_freshness
        return result

    def wrapped_detect(root):
        calls["detect"] += 1
        return original_detect(root)

    def wrapped_select(root, task_slug, **kwargs):
        calls["select"] += 1
        captured.update(kwargs)
        return original_select(root, task_slug, **kwargs)

    monkeypatch.setattr(generate_module, "resolve_derived_freshness", wrapped_resolve)
    monkeypatch.setattr(generate_module, "detect_project_signals", wrapped_detect)
    monkeypatch.setattr(generate_module, "select_context", wrapped_select)

    preparation = _prepare_generation(project_tmp, slug)

    assert calls == {"lineage": 1, "select": 1, "detect": 1}
    assert captured["derived_freshness"] is captured["freshness"]
    assert isinstance(captured["budget_configuration"], BudgetConfiguration)
    assert captured["repository_observations"] == RepositoryObservations(
        available=True,
        git_repo=False,
        existing_agent_files=(),
    )
    assert (
        preparation.selection.repository_observations
        == captured["repository_observations"]
    )


def test_preparation_merges_canonical_exact_findings_outside_selector(
    project_tmp,
    monkeypatch,
):
    slug, baseline = _prepared_project(project_tmp)
    duplicate = ContextFinding("same", FindingLevel.WARNING, None, "same")
    lineage_blocker = ContextFinding("lineage_blocker", FindingLevel.BLOCKER, None, "lineage")
    selector_info = ContextFinding("selector_info", FindingLevel.INFO, None, "selector")
    budget_warning = ContextFinding("budget_warning", FindingLevel.WARNING, None, "budget")
    freshness = MappingProxyType(
        {
            "spec": FreshnessState.MISSING,
            "requirements_projection": FreshnessState.MISSING,
            "preflight": FreshnessState.MISSING,
            "test_contract_review": FreshnessState.MISSING,
        }
    )

    monkeypatch.setattr(
        generate_module,
        "resolve_derived_freshness",
        lambda *_: SimpleNamespace(
            derived_freshness=freshness,
            findings=(duplicate, lineage_blocker),
        ),
    )
    monkeypatch.setattr(
        generate_module,
        "_load_budget_configuration",
        lambda *_: (baseline.selection.budget.configuration, (budget_warning, duplicate)),
    )
    monkeypatch.setattr(
        generate_module,
        "select_context",
        lambda *args, **kwargs: replace(
            baseline.selection,
            findings=(duplicate, selector_info),
        ),
    )

    preparation = _prepare_generation(project_tmp, slug)

    assert preparation.findings == _canonical_findings(
        [duplicate, lineage_blocker, budget_warning, duplicate, selector_info]
    )
    assert preparation.blockers == (lineage_blocker,)
    assert preparation.selection.findings == (duplicate, selector_info)


def test_generation_preparation_is_frozen_tuple_backed_and_equal(project_tmp):
    _, preparation = _prepared_project(project_tmp)

    assert preparation == GenerationPreparation(
        selection=preparation.selection,
        findings=preparation.findings,
    )
    assert isinstance(preparation.findings, tuple)
    with pytest.raises(FrozenInstanceError):
        preparation.findings = ()  # type: ignore[misc]
    with pytest.raises(TypeError, match="findings must be a tuple"):
        GenerationPreparation(
            preparation.selection,
            list(preparation.findings),  # type: ignore[arg-type]
        )


def test_valid_custom_budget_thresholds_reach_selection_unchanged(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    custom = {
        "per_file_warning_approximate_tokens": 101,
        "per_file_high_risk_approximate_tokens": 202,
        "total_warning_approximate_tokens": 303,
        "total_high_risk_approximate_tokens": 404,
    }
    (project_tmp / ".harness" / "config.yaml").write_text(
        yaml.safe_dump({"context_budget": custom}),
        encoding="utf-8",
    )
    captured = {}
    original_select = context_module.select_context

    def wrapped_select(root, task_slug, **kwargs):
        captured["budget"] = kwargs["budget_configuration"]
        return original_select(root, task_slug, **kwargs)

    monkeypatch.setattr(generate_module, "select_context", wrapped_select)

    _prepare_generation(project_tmp, slug)

    assert captured["budget"] == BudgetConfiguration(**custom)


def test_absent_budget_section_uses_defaults_and_existing_info(project_tmp):
    assert init_project(project_tmp)[0] == 0
    config = project_tmp / ".harness" / "config.yaml"
    loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
    loaded.pop("context_budget")
    config.write_text(yaml.safe_dump(loaded), encoding="utf-8")

    budget, findings = _load_budget_configuration(project_tmp)

    assert budget == BudgetConfiguration(4000, 8000, 12000, 24000)
    assert [finding.code for finding in findings] == [
        "context_budget_defaults_applied"
    ]


def test_malformed_budget_section_uses_complete_defaults_and_warning(project_tmp):
    assert init_project(project_tmp)[0] == 0
    (project_tmp / ".harness" / "config.yaml").write_text(
        "context_budget:\n  total_warning_approximate_tokens: 1\n",
        encoding="utf-8",
    )

    budget, findings = _load_budget_configuration(project_tmp)

    assert budget == BudgetConfiguration(4000, 8000, 12000, 24000)
    assert [finding.code for finding in findings] == [
        "malformed_context_budget_configuration"
    ]


@pytest.mark.parametrize(
    "configuration_content",
    [
        "not: [valid\n",
        "- top-level\n- list\n",
        "plain scalar\n",
    ],
)
def test_invalid_configuration_shapes_use_stable_blocker(
    project_tmp,
    configuration_content,
):
    assert init_project(project_tmp)[0] == 0
    config = project_tmp / ".harness" / "config.yaml"
    config.write_text(configuration_content, encoding="utf-8")

    budget, findings = _load_budget_configuration(project_tmp)

    assert budget == BudgetConfiguration(4000, 8000, 12000, 24000)
    assert findings == (generate_module._CONFIGURATION_UNAVAILABLE,)
    blocker = findings[0]
    assert blocker.level == FindingLevel.BLOCKER
    assert blocker.path == ".harness/config.yaml"
    assert blocker.message == (
        "Harness configuration could not be read for context selection: "
        ".harness/config.yaml."
    )
    assert str(project_tmp) not in blocker.message


def test_missing_nonregular_and_unreadable_configuration_use_same_blocker(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    config = project_tmp / ".harness" / "config.yaml"
    config.unlink()
    _, missing = _load_budget_configuration(project_tmp)

    config.mkdir()
    _, nonregular = _load_budget_configuration(project_tmp)

    config.rmdir()
    config.write_text("schema_version: 1\n", encoding="utf-8")
    monkeypatch.setattr(
        generate_module,
        "read_text",
        lambda *_: (_ for _ in ()).throw(OSError("private path")),
    )
    _, unreadable = _load_budget_configuration(project_tmp)

    expected = generate_module._CONFIGURATION_UNAVAILABLE
    expected_result = (
        BudgetConfiguration(4000, 8000, 12000, 24000),
        (expected,),
    )
    assert _load_budget_configuration(project_tmp) == expected_result
    assert missing == (expected,)
    assert nonregular == (expected,)
    assert unreadable == (expected,)
    assert config.read_text(encoding="utf-8") == "schema_version: 1\n"


def test_unsafe_configuration_path_uses_only_stable_blocker(
    project_tmp,
    monkeypatch,
):
    assert init_project(project_tmp)[0] == 0
    original_resolve = generate_module.resolve_under_root

    def unsafe_configuration(root, relative_path):
        if str(relative_path) == generate_module._CONFIGURATION_PATH:
            raise generate_module.PathSafetyError("private unsafe path")
        return original_resolve(root, relative_path)

    monkeypatch.setattr(generate_module, "resolve_under_root", unsafe_configuration)

    assert _load_budget_configuration(project_tmp) == (
        BudgetConfiguration(4000, 8000, 12000, 24000),
        (generate_module._CONFIGURATION_UNAVAILABLE,),
    )


def test_repository_observation_conversion_is_exact_sorted_and_safe():
    observations = _repository_observations(
        {
            "git_repo": 1,
            "detected_languages": ["python", "go", "python"],
            "detected_package_managers": {"pip", "cargo", "pip"},
            "detected_test_frameworks": ("pytest", "pytest"),
            "detected_ci": ["github-actions", 7],
            "existing_agent_files": None,
            "ignored_deeper_signal": ["must-not-appear"],
        }
    )

    assert observations == RepositoryObservations(
        available=True,
        git_repo=True,
        detected_languages=("go", "python"),
        detected_package_managers=("cargo", "pip"),
        detected_test_frameworks=("pytest",),
        detected_ci=("7", "github-actions"),
        existing_agent_files=(),
    )
    assert _repository_observations({}) == RepositoryObservations(available=True)


def test_missing_required_source_blocks_before_all_writes(project_tmp):
    slug = _start_sample_task(project_tmp)
    _task_path(project_tmp, slug, "acceptance.md").unlink()
    manifest_before = _manifest_path(project_tmp).read_bytes()
    generated_dir = _workset_path(project_tmp, slug).parent

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert messages[0] == "Cannot generate agent workset."
    assert any(
        message.startswith("- missing_required_source:")
        and ".harness/tasks/generate-me/acceptance.md" in message
        for message in messages
    )
    assert _manifest_path(project_tmp).read_bytes() == manifest_before
    assert not generated_dir.exists()


def test_unreadable_required_source_blocks_before_all_writes(project_tmp):
    slug = _start_sample_task(project_tmp)
    evidence = _task_path(project_tmp, slug, "evidence.md")
    evidence.unlink()
    evidence.mkdir()

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert any(
        message.startswith("- unreadable_required_source:")
        and ".harness/tasks/generate-me/evidence.md" in message
        for message in messages
    )
    assert not _workset_path(project_tmp, slug).exists()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"force": True},
        {"dry_run": True},
        {"force": True, "dry_run": True},
    ],
)
def test_lineage_blocker_is_not_bypassed_and_preserves_bytes(
    project_tmp,
    monkeypatch,
    options,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    fixed = "2026-07-30T00:00:00+00:00"
    monkeypatch.setattr(generate_module, "_timestamp", lambda: fixed)
    assert run_generate(project_tmp, slug)[0] == 0
    workset_before = _workset_path(project_tmp, slug).read_bytes()
    manifest_before = _manifest_path(project_tmp).read_bytes()
    preparation = _prepare_generation(project_tmp, slug)
    blocker = ContextFinding(
        "lineage_test_blocker",
        FindingLevel.BLOCKER,
        ".harness/manifest.json",
        "Lineage test blocker.",
    )
    blocked = GenerationPreparation(
        preparation.selection,
        _canonical_findings([*preparation.findings, blocker]),
    )
    monkeypatch.setattr(generate_module, "_prepare_generation", lambda *_: blocked)

    code, messages = run_generate(project_tmp, slug, **options)

    assert code == 1
    assert messages[0] == "Cannot generate agent workset."
    assert "- lineage_test_blocker: Lineage test blocker. [.harness/manifest.json]" in messages
    assert _workset_path(project_tmp, slug).read_bytes() == workset_before
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_configuration_blocker_prevents_output_and_manifest_writes(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    (project_tmp / ".harness" / "config.yaml").write_text(
        "not: [valid\n",
        encoding="utf-8",
    )
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert any(
        message == (
            "- generation_configuration_unavailable: Harness configuration "
            "could not be read for context selection: .harness/config.yaml."
        )
        for message in messages
    )
    assert not _workset_path(project_tmp, slug).exists()
    assert _manifest_path(project_tmp).read_bytes() == manifest_before
    assert str(project_tmp) not in "\n".join(messages)


def test_unsafe_selector_finding_and_multiple_blockers_use_canonical_order(
    project_tmp,
    monkeypatch,
):
    slug, preparation = _prepared_project(project_tmp)
    later = ContextFinding(
        "z_blocker",
        FindingLevel.BLOCKER,
        None,
        "Later blocker.",
    )
    unsafe = ContextFinding(
        "unsafe_resolved_path",
        FindingLevel.BLOCKER,
        ".harness/tasks/generate-me/task.md",
        "Registered context path resolves outside the repository root.",
    )
    blocked = GenerationPreparation(
        preparation.selection,
        _canonical_findings([later, unsafe]),
    )
    monkeypatch.setattr(generate_module, "_prepare_generation", lambda *_: blocked)

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert messages == [
        "Cannot generate agent workset.",
        (
            "- unsafe_resolved_path: Registered context path resolves outside "
            "the repository root. [.harness/tasks/generate-me/task.md]"
        ),
        "- z_blocker: Later blocker.",
    ]


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        (FreshnessState.MISSING, "missing_optional_artifact"),
        (FreshnessState.UNKNOWN, "unknown_derived_artifact_excluded"),
        (FreshnessState.STALE, "stale_derived_artifact_excluded"),
    ],
)
def test_nonfresh_optional_derived_artifact_is_excluded_but_generation_succeeds(
    project_tmp,
    monkeypatch,
    state,
    expected_code,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    spec = _task_path(project_tmp, slug, "spec.md")
    spec.write_text("# Specification\n\nUNIQUE DERIVED BODY\n", encoding="utf-8")
    original = generate_module.resolve_derived_freshness(project_tmp, slug)
    freshness = dict(original.derived_freshness)
    freshness["spec"] = state
    immutable_freshness = MappingProxyType(freshness)
    monkeypatch.setattr(
        generate_module,
        "resolve_derived_freshness",
        lambda *_: SimpleNamespace(
            derived_freshness=immutable_freshness,
            findings=(),
        ),
    )
    real_read_bytes = Path.read_bytes
    spec_read_attempted = False

    def guarded_read_bytes(path):
        nonlocal spec_read_attempted
        if path.resolve() == spec.resolve():
            spec_read_attempted = True
            raise AssertionError("non-fresh spec.md must not be read")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert not spec_read_attempted
    assert "UNIQUE DERIVED BODY" not in text
    assert f"`{expected_code}`" in text
    assert f"rerun `ai-sdlc spec --task {slug}`" in text


def test_advisory_budget_warning_and_malformed_budget_continue(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    (project_tmp / ".harness" / "config.yaml").write_text(
        "context_budget:\n  incomplete: true\n",
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert "`malformed_context_budget_configuration`" in text
    assert "Budget findings are advisory" in text


@pytest.mark.parametrize(
    ("per_file_high_risk", "expected_status", "expected_finding"),
    [
        (100_000, BudgetStatus.WARNING, "context_file_budget_warning"),
        (2, BudgetStatus.HIGH_RISK, "context_file_budget_high_risk"),
    ],
)
def test_real_advisory_budget_outcomes_are_rendered_and_do_not_block(
    project_tmp,
    per_file_high_risk,
    expected_status,
    expected_finding,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    configuration = {
        "context_budget": {
            "per_file_warning_approximate_tokens": 1,
            "per_file_high_risk_approximate_tokens": per_file_high_risk,
            "total_warning_approximate_tokens": 100_000,
            "total_high_risk_approximate_tokens": 200_000,
        }
    }
    (project_tmp / ".harness" / "config.yaml").write_text(
        yaml.safe_dump(configuration),
        encoding="utf-8",
    )

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert f"- Status: `{expected_status.value}`" in text
    assert f"`{expected_finding}`" in text


def test_fresh_derived_outputs_are_selected_in_authority_order(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_preflight(project_tmp, slug)[0] == 0
    assert run_spec(project_tmp, slug)[0] == 0
    assert run_test_contract_review(project_tmp, slug)[0] == 0

    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    ids = [
        "`task`",
        "`acceptance`",
        "`architecture_notes`",
        "`coupling_notes`",
        "`test_contract`",
        "`verification`",
        "`evidence`",
        "`spec`",
        "`requirements_projection`",
        "`preflight`",
        "`test_contract_review`",
        "`selected_packs`",
        "`repository_signals`",
    ]
    positions = [text.index(f"### {entry_id}") for entry_id in ids]
    assert positions == sorted(positions)
    assert "Spec question:" in text
    assert "Artifact role: advisory projection." in text
    assert "# Preflight Report" in text
    assert "# Test-Contract Readiness Review" in text
    assert "Selected report findings:" in text


def test_workset_render_uses_selection_only_and_is_deterministic(project_tmp):
    slug, preparation = _prepared_project(project_tmp)
    generated_at = "2026-07-30T00:00:00+00:00"

    first = _render_workset(
        slug=slug,
        generated_at=generated_at,
        selection=preparation.selection,
        findings=preparation.findings,
    )
    for filename in TASK_FILES:
        _task_path(project_tmp, slug, filename).write_text(
            "MUTATED AFTER SELECTION\n",
            encoding="utf-8",
        )
    second = _render_workset(
        slug=slug,
        generated_at=generated_at,
        selection=preparation.selection,
        findings=preparation.findings,
    )

    assert first == second
    assert first.endswith("\n")
    assert not first.endswith("\n\n")
    assert "MUTATED AFTER SELECTION" not in first
    for entry in preparation.selection.selected_entries:
        assert f"### `{entry.entry_id}`" in first
        if entry.path is not None:
            assert f"- Path: `{entry.path}`" in first
        assert f"- Classification: `{entry.classification.value}`" in first
        assert f"- Authority: `{entry.authority_level.value}`" in first
        assert f"- Inclusion: `{entry.inclusion_mode.value}`" in first
        assert f"- Freshness: `{entry.freshness_state.value}`" in first
        assert entry.selected_content.splitlines()[0] in first


def test_workset_includes_conditional_baseline_secure_engineering_guardrails(
    project_tmp,
):
    slug, preparation = _prepared_project(project_tmp)
    text = _render_workset(
        slug=slug,
        generated_at="2026-07-30T00:00:00+00:00",
        selection=preparation.selection,
        findings=preparation.findings,
    )
    lowered = text.lower()

    assert "## Baseline Secure-Engineering Guardrails" in text
    assert "Apply when relevant to the task" in text
    assert "hard-coded secrets" in text
    assert "sensitive values in logs, errors, evidence, or generated artifacts" in text
    assert "untrusted input at trust boundaries" in text
    assert "reject malformed or unsupported input safely" in text
    assert "authorization checks and least privilege" in text
    assert "do not broaden access, capability, or authority implicitly" in text
    assert "Fail safely without exposing credentials" in text
    assert "negative or abuse-path verification" in text
    assert "when automation is impractical" in text
    assert "dependency or security-relevant configuration changes" in text
    assert "explicit review and evidence items" in text
    assert "A clean Harness workflow is not proof of correctness, security, or compliance" in text
    assert "scan for security issues or vulnerabilities" in text
    assert "query external security or reputation services" in text
    assert "security " + "score" not in lowered
    assert "security " + "passed" not in lowered
    assert "security " + "validated" not in lowered


def test_workset_budget_and_merged_findings_are_exact(project_tmp):
    slug, preparation = _prepared_project(project_tmp)
    text = _render_workset(
        slug=slug,
        generated_at="2026-07-30T00:00:00+00:00",
        selection=preparation.selection,
        findings=preparation.findings,
    )
    budget = preparation.selection.budget

    assert f"- Status: `{budget.result.status.value}`" in text
    assert f"- Selected entry count: {budget.result.selected_entry_count}" in text
    assert f"- Selected bytes: {budget.result.selected_bytes}" in text
    assert (
        f"- Selected approximate tokens: "
        f"{budget.result.selected_approximate_tokens}"
    ) in text
    finding_lines = [
        line
        for line in text.splitlines()
        if line.startswith(("- blocker: `", "- warning: `", "- info: `"))
    ]
    assert len(finding_lines) == len(preparation.findings)
    assert [
        line.split("`", 2)[1]
        for line in finding_lines
    ] == [finding.code for finding in preparation.findings]


def test_selection_redacts_secrets_omits_environment_lines_and_excludes_outputs(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    long_text = "\n".join(f"line {index}" for index in range(80))
    fake_secret = "sk-" + "abcdefghijklmnop"
    fake_env_line = "API_" + "TOKEN" + "=" + "token-" + "abcdefghijklmnop"
    _task_path(project_tmp, slug, "task.md").write_text(
        (
            "# Task\n\n## Implementation Boundary\n\n"
            f"{fake_env_line}\n{fake_secret}\n{long_text}\n"
        ),
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "evidence-report.md").write_text(
        "EXCLUDED EVIDENCE REPORT CONTENT\n",
        encoding="utf-8",
    )
    _task_path(project_tmp, slug, "validation-report.md").write_text(
        "EXCLUDED VALIDATION REPORT CONTENT\n",
        encoding="utf-8",
    )
    code, _ = run_generate(project_tmp, slug)

    text = _workset_path(project_tmp, slug).read_text(encoding="utf-8")
    assert code == 0
    assert fake_env_line not in text
    assert fake_secret not in text
    assert "[REDACTED]" in text
    assert "line 79" not in text
    assert "EXCLUDED EVIDENCE REPORT CONTENT" not in text
    assert "EXCLUDED VALIDATION REPORT CONTENT" not in text
    assert "context-manifest.yaml" not in text


def test_legacy_generate_readers_are_removed_and_detection_has_one_caller():
    for name in (
        "_read_required_inputs",
        "_read_optional_report",
        "_load_selected_packs",
    ):
        assert not hasattr(generate_module, name)


def test_generate_does_not_overwrite_task_inputs_or_global_agent_files(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    root_agents = project_tmp / "AGENTS.md"
    root_claude = project_tmp / "CLAUDE.md"
    root_agents.write_text("root agents\n", encoding="utf-8")
    root_claude.write_text("root claude\n", encoding="utf-8")
    global_instructions = (
        project_tmp / ".harness" / "generated" / "agent-instructions.md"
    )
    before = {
        path: path.read_bytes()
        for path in [
            *(_task_path(project_tmp, slug, filename) for filename in TASK_FILES),
            root_agents,
            root_claude,
            global_instructions,
        ]
    }

    code, messages = run_generate(project_tmp, slug)

    assert code == 0
    assert {path: path.read_bytes() for path in before} == before
    assert "root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified" in messages
    assert ".harness/generated/agent-instructions.md was not modified" in messages
    assert _context_manifest_path(project_tmp, slug).is_file()


def test_generate_dry_run_writes_nothing_and_preserves_manifest(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug, dry_run=True)

    assert code == 0
    assert "dry run; no files written" in messages
    assert not _workset_path(project_tmp, slug).exists()
    assert not _context_manifest_path(project_tmp, slug).exists()
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_generate_skips_exact_noop_and_preserves_workset_and_manifest_bytes(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr(
        generate_module,
        "_timestamp",
        lambda: "2026-07-30T00:00:00+00:00",
    )
    assert run_generate(project_tmp, slug)[0] == 0
    assert run_generate(project_tmp, slug)[0] == 0
    workset_before = _workset_path(project_tmp, slug).read_bytes()
    context_manifest_before = _context_manifest_path(project_tmp, slug).read_bytes()
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug)

    assert code == 0
    assert "skip unchanged file .harness/tasks/generate-me/generated/agent-workset.md" in messages
    assert "skip unchanged file .harness/tasks/generate-me/generated/context-manifest.yaml" in messages
    assert "skip existing manifest .harness/manifest.json" in messages
    assert _workset_path(project_tmp, slug).read_bytes() == workset_before
    assert _context_manifest_path(project_tmp, slug).read_bytes() == context_manifest_before
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_changed_workset_is_persisted_before_manifest_hash_is_recorded(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr(
        generate_module,
        "_timestamp",
        lambda: "2026-07-30T00:00:00+00:00",
    )
    assert run_generate(project_tmp, slug)[0] == 0
    first_hash = _manifest_entry(
        project_tmp,
        ".harness/tasks/generate-me/generated/agent-workset.md",
    )["sha256"]
    monkeypatch.setattr(
        generate_module,
        "_timestamp",
        lambda: "2026-07-30T00:00:01+00:00",
    )

    code, messages = run_generate(project_tmp, slug)

    second_entry = _manifest_entry(
        project_tmp,
        ".harness/tasks/generate-me/generated/agent-workset.md",
    )
    assert code == 0
    assert "refresh file .harness/tasks/generate-me/generated/agent-workset.md" in messages
    assert second_entry["sha256"] != first_hash
    assert verify_project(project_tmp) == (
        0,
        ["AI SDLC Harness verification passed"],
    )


def test_verify_passes_immediately_after_successful_generate(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0

    assert verify_project(project_tmp) == (
        0,
        ["AI SDLC Harness verification passed"],
    )


def test_verify_fails_when_manifest_managed_workset_is_missing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    _workset_path(project_tmp, slug).unlink()

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert (
        "- missing file "
        ".harness/tasks/generate-me/generated/agent-workset.md"
    ) in messages


def test_verify_fails_when_manifest_managed_workset_hash_drifts(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    _workset_path(project_tmp, slug).write_text(
        "changed outside generate\n",
        encoding="utf-8",
    )

    code, messages = verify_project(project_tmp)

    assert code == 1
    assert (
        "- hash drift detected for "
        ".harness/tasks/generate-me/generated/agent-workset.md"
    ) in messages


def test_verify_does_not_require_workset_for_initialized_task(project_tmp):
    slug = _start_sample_task(project_tmp)

    assert not _workset_path(project_tmp, slug).exists()
    assert verify_project(project_tmp) == (
        0,
        ["AI SDLC Harness verification passed"],
    )


def test_generate_refuses_managed_drift_without_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    workset = _workset_path(project_tmp, slug)
    workset.write_text("custom workset\n", encoding="utf-8")
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert "hash drift detected for .harness/tasks/generate-me/generated/agent-workset.md" in messages
    assert workset.read_text(encoding="utf-8") == "custom workset\n"
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_generate_force_overwrites_only_managed_workset_and_preserves_user_note(
    project_tmp,
):
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
    workset = _workset_path(project_tmp, slug)
    workset.parent.mkdir()
    workset.write_text("user owned\n", encoding="utf-8")
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug, force=True)

    assert code == 1
    assert (
        "unmanaged existing file "
        ".harness/tasks/blocked-workset/generated/agent-workset.md"
    ) in messages
    assert workset.read_text(encoding="utf-8") == "user owned\n"
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_unsafe_managed_output_path_fails_without_preparation_or_private_path(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    prepared = False

    def fail_resolution(*_):
        raise generate_module.PathSafetyError("private absolute path")

    def unexpected_preparation(*_):
        nonlocal prepared
        prepared = True
        raise AssertionError("preparation must not run")

    monkeypatch.setattr(
        generate_module,
        "resolve_managed_output_under_root",
        fail_resolution,
    )
    monkeypatch.setattr(
        generate_module,
        "_prepare_generation",
        unexpected_preparation,
    )

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert not prepared
    assert "safely inspect both generate-owned output paths" in messages[0]
    assert "private absolute path" not in "\n".join(messages)


def test_output_persistence_failure_is_bounded_and_does_not_refresh_manifest(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = _manifest_path(project_tmp).read_bytes()
    refreshed = False

    def fail_persistence(*_):
        raise OSError("private absolute path")

    def unexpected_manifest(*_, **__):
        nonlocal refreshed
        refreshed = True

    monkeypatch.setattr(generate_module, "persist_exact_bytes", fail_persistence)
    monkeypatch.setattr(generate_module, "persist_manifest_model", unexpected_manifest)

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert messages[0] == "Could not persist agent workset."
    assert "private absolute path" not in "\n".join(messages)
    assert not refreshed
    assert not _workset_path(project_tmp, slug).exists()
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_manifest_refresh_failure_is_bounded_after_confirmed_workset_persistence(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = _manifest_path(project_tmp).read_bytes()
    monkeypatch.setattr(
        generate_module,
        "persist_manifest_model",
        lambda *_, **__: (_ for _ in ()).throw(OSError("private absolute path")),
    )

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert messages[0] == (
        "Generated files were persisted, but the Harness manifest could not be refreshed."
    )
    assert "private absolute path" not in "\n".join(messages)
    assert _workset_path(project_tmp, slug).is_file()
    assert _context_manifest_path(project_tmp, slug).is_file()
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_status_counts_only_manifest_managed_worksets_and_remains_read_only(
    project_tmp,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    unmanaged_task = (
        project_tmp / ".harness" / "tasks" / "unmanaged-task" / "generated"
    )
    unmanaged_task.mkdir(parents=True)
    (unmanaged_task / "agent-workset.md").write_text(
        "not managed\n",
        encoding="utf-8",
    )
    tracked = [project_tmp / relative for relative in BASE_MANAGED_FILES]
    tracked.extend(_task_path(project_tmp, slug, filename) for filename in TASK_FILES)
    tracked.append(_workset_path(project_tmp, slug))
    tracked.append(_context_manifest_path(project_tmp, slug))
    before = {path: path.read_bytes() for path in tracked}

    code, messages = status_project(project_tmp)

    assert code == 0
    assert "manifest-managed generated worksets: 1" in messages
    assert {path: path.read_bytes() for path in tracked} == before


def test_context_manifest_is_managed_without_generate_provenance(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)

    assert run_generate(project_tmp, slug)[0] == 0

    manifest = _manifest(project_tmp)
    workset_entry = _manifest_entry(
        project_tmp,
        ".harness/tasks/generate-me/generated/agent-workset.md",
    )
    assert _context_manifest_path(project_tmp, slug).is_file()
    assert "provenance" not in workset_entry
    assert (
        ".harness/tasks/generate-me/generated/context-manifest.yaml"
        in {entry["path"] for entry in manifest["managed_files"]}
    )
    assert manifest.get("generated_artifact_provenance", []) == []


def test_context_manifest_is_built_from_selection_and_confirmed_workset_snapshot(
    project_tmp,
):
    slug, preparation = _prepared_project(project_tmp)
    workset_text = _render_workset(
        slug=slug,
        generated_at="2026-08-01T00:00:00+00:00",
        selection=preparation.selection,
        findings=preparation.findings,
    )
    workset_bytes = workset_text.encode("utf-8")
    workset_hash = sha256_bytes(workset_bytes)

    document = _build_context_manifest(
        preparation=preparation,
        workset_text=workset_text,
        workset_sha256=workset_hash,
    )
    rendered = render_context_manifest_yaml(document)
    parsed = parse_context_manifest_yaml(rendered)

    assert document.schema_version == 1
    assert document.artifact_path == (
        ".harness/tasks/generate-me/generated/context-manifest.yaml"
    )
    assert document.artifact_role == "context_selection_manifest"
    assert document.authority == "non_authoritative"
    assert document.edit_model == "generated_do_not_edit"
    assert document.task_slug == slug
    assert [item.path for item in document.source_artifacts] == [
        artifact.path for artifact in preparation.selection.source_artifacts
    ]
    assert [item.sha256 for item in document.source_artifacts] == [
        artifact.source_sha256
        for artifact in preparation.selection.source_artifacts
    ]
    assert len(document.source_artifacts) == 7
    assert len(document.generated_artifacts) == 1
    assert document.generated_artifacts[0].path.endswith("/agent-workset.md")
    assert document.generated_artifacts[0].sha256 == workset_hash
    assert document.artifact_path not in {
        item.path for item in document.generated_artifacts
    }
    workset_entry = next(
        item
        for item in document.context_entries
        if item.entry_id == "agent_workset_output"
    )
    assert workset_entry.existence.value == "present"
    assert workset_entry.inclusion_mode.value == "excluded"
    assert workset_entry.size_estimate.selected.bytes == 0
    assert workset_entry.size_estimate.source.bytes == len(workset_bytes)
    assert workset_entry.size_estimate.source.characters == len(workset_text)
    context_output_entry = next(
        item
        for item in document.context_entries
        if item.entry_id == "context_manifest_output"
    )
    original_context_output = next(
        item
        for item in preparation.selection.context_entries
        if item.entry_id == "context_manifest_output"
    )
    assert context_output_entry == original_context_output
    assert document.findings == preparation.findings
    assert document.budget == preparation.selection.budget
    assert parsed == document
    assert render_context_manifest_yaml(parsed) == rendered
    assert render_context_manifest_yaml(document) == rendered
    assert "self_hash" not in rendered


def test_context_manifest_construction_does_not_reread_task_sources(
    project_tmp,
    monkeypatch,
):
    slug, preparation = _prepared_project(project_tmp)
    expected_hashes = tuple(
        artifact.source_sha256 for artifact in preparation.selection.source_artifacts
    )
    workset_text = _render_workset(
        slug=slug,
        generated_at="2026-08-01T00:00:00+00:00",
        selection=preparation.selection,
        findings=preparation.findings,
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda *_: pytest.fail("context manifest construction reread a source"),
    )

    document = _build_context_manifest(
        preparation=preparation,
        workset_text=workset_text,
        workset_sha256=sha256_bytes(workset_text.encode("utf-8")),
    )

    assert tuple(item.sha256 for item in document.source_artifacts) == expected_hashes


def test_successful_generate_manages_exact_pair_and_preserves_lineage(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_preflight(project_tmp, slug)[0] == 0
    assert run_spec(project_tmp, slug)[0] == 0
    provenance_before = _manifest(project_tmp)["generated_artifact_provenance"]
    managed_before = {
        item["path"]: item for item in _manifest(project_tmp)["managed_files"]
    }

    assert run_generate(project_tmp, slug)[0] == 0

    manifest = _manifest(project_tmp)
    by_path = {item["path"]: item for item in manifest["managed_files"]}
    for target in (
        _workset_path(project_tmp, slug),
        _context_manifest_path(project_tmp, slug),
    ):
        relative = target.relative_to(project_tmp).as_posix()
        record = by_path[relative]
        assert record["protected"] is True
        assert record["hash_algorithm"] == "sha256"
        assert record["sha256"] == sha256_bytes(target.read_bytes())
    assert manifest["generated_artifact_provenance"] == provenance_before
    assert {
        path: record
        for path, record in by_path.items()
        if not path.endswith(("agent-workset.md", "context-manifest.yaml"))
    } == managed_before
    assert all(
        "agent-workset.md" not in record["output_path"]
        and "context-manifest.yaml" not in record["output_path"]
        for record in manifest["generated_artifact_provenance"]
    )
    assert verify_project(project_tmp)[0] == 0


def test_generate_preserves_legacy_v1_manifest_schema(project_tmp):
    slug = _start_sample_task(project_tmp)
    data = _manifest(project_tmp)
    data.pop("manifest_schema_version")
    data.pop("generated_artifact_provenance")
    _write_manifest_data(project_tmp, data)
    _fill_task_inputs(project_tmp, slug)

    assert run_generate(project_tmp, slug)[0] == 0

    persisted = _manifest(project_tmp)
    assert "manifest_schema_version" not in persisted
    assert "generated_artifact_provenance" not in persisted


@pytest.mark.parametrize("filename", ["agent-workset.md", "context-manifest.yaml"])
def test_verify_fails_for_missing_or_drifted_generate_output(project_tmp, filename):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    target = _workset_path(project_tmp, slug).parent / filename
    relative = target.relative_to(project_tmp).as_posix()

    target.unlink()
    code, messages = verify_project(project_tmp)
    assert code == 1
    assert any(f"missing file {relative}" in message for message in messages)

    assert run_generate(project_tmp, slug)[0] == 0
    target.write_bytes(b"drifted\r\n")
    code, messages = verify_project(project_tmp)
    assert code == 1
    assert any(f"hash drift detected for {relative}" in message for message in messages)


def test_legacy_managed_workset_only_migration_does_not_rewrite_workset(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    monkeypatch.setattr(
        generate_module,
        "_timestamp",
        lambda: "2026-08-01T00:00:00+00:00",
    )
    assert run_generate(project_tmp, slug)[0] == 0
    workset_before = _workset_path(project_tmp, slug).read_bytes()
    context_target = _context_manifest_path(project_tmp, slug)
    context_target.unlink()
    data = _manifest(project_tmp)
    data["managed_files"] = [
        record
        for record in data["managed_files"]
        if record["path"] != context_target.relative_to(project_tmp).as_posix()
    ]
    _write_manifest_data(project_tmp, data)

    code, messages = run_generate(project_tmp, slug)

    assert code == 0
    assert _workset_path(project_tmp, slug).read_bytes() == workset_before
    assert "skip unchanged file .harness/tasks/generate-me/generated/agent-workset.md" in messages
    assert "create file .harness/tasks/generate-me/generated/context-manifest.yaml" in messages
    assert context_target.is_file()


def test_clean_malformed_managed_context_manifest_is_regenerated(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    target = _context_manifest_path(project_tmp, slug)
    malformed = b"schema_version: [malformed\n"
    target.write_bytes(malformed)
    data = _manifest(project_tmp)
    record = next(
        item for item in data["managed_files"] if item["path"].endswith("context-manifest.yaml")
    )
    record["sha256"] = sha256_bytes(malformed)
    _write_manifest_data(project_tmp, data)

    code, _ = run_generate(project_tmp, slug)

    assert code == 0
    parsed = parse_context_manifest_yaml(target.read_text(encoding="utf-8"))
    assert parsed.task_slug == slug


def test_existing_unmanaged_context_manifest_blocks_even_with_force(project_tmp):
    slug = _start_sample_task(project_tmp)
    target = _context_manifest_path(project_tmp, slug)
    target.parent.mkdir()
    target.write_text("user owned\n", encoding="utf-8")
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug, force=True)

    assert code == 1
    assert f"unmanaged existing file {target.relative_to(project_tmp).as_posix()}" in messages
    assert target.read_text(encoding="utf-8") == "user owned\n"
    assert not _workset_path(project_tmp, slug).exists()
    assert _manifest_path(project_tmp).read_bytes() == manifest_before


def test_context_manifest_managed_drift_requires_force_and_can_recover(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    assert run_generate(project_tmp, slug)[0] == 0
    target = _context_manifest_path(project_tmp, slug)
    target.write_text("drifted\n", encoding="utf-8")
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug)
    assert code == 1
    assert any("hash drift detected" in message for message in messages)
    assert _manifest_path(project_tmp).read_bytes() == manifest_before

    code, _ = run_generate(project_tmp, slug, force=True)
    assert code == 0
    assert parse_context_manifest_yaml(target.read_text(encoding="utf-8")).task_slug == slug


@pytest.mark.parametrize(
    "private_detail",
    [
        "unsafe final output at C:/private/final",
        "symlinked parent at C:/private/parent",
    ],
)
def test_context_manifest_safety_is_checked_before_workset_persistence(
    project_tmp,
    monkeypatch,
    private_detail,
):
    slug = _start_sample_task(project_tmp)
    resolved: list[str] = []
    persisted = False
    original = generate_module.resolve_managed_output_under_root

    def resolve(root, path):
        resolved.append(str(path))
        if str(path).endswith("context-manifest.yaml"):
            raise generate_module.PathSafetyError(private_detail)
        return original(root, path)

    def unexpected_persist(*_):
        nonlocal persisted
        persisted = True
        raise AssertionError("persistence must not run")

    monkeypatch.setattr(generate_module, "resolve_managed_output_under_root", resolve)
    monkeypatch.setattr(generate_module, "persist_exact_bytes", unexpected_persist)

    code, messages = run_generate(project_tmp, slug, force=True)

    assert code == 1
    assert resolved == [
        ".harness/tasks/generate-me/generated/agent-workset.md",
        ".harness/tasks/generate-me/generated/context-manifest.yaml",
    ]
    assert not persisted
    assert private_detail not in "\n".join(messages)


def test_persistence_order_is_pair_then_single_manifest_update(project_tmp, monkeypatch):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    calls: list[str] = []
    real_persist = generate_module.persist_exact_bytes
    real_manifest_persist = generate_module.persist_manifest_model

    def persist(path, content):
        calls.append(path.name)
        return real_persist(path, content)

    def persist_manifest(root, document):
        calls.append("manifest.json")
        return real_manifest_persist(root, document)

    monkeypatch.setattr(generate_module, "persist_exact_bytes", persist)
    monkeypatch.setattr(generate_module, "persist_manifest_model", persist_manifest)

    assert run_generate(project_tmp, slug)[0] == 0
    assert calls == ["agent-workset.md", "context-manifest.yaml", "manifest.json"]


def test_context_manifest_persistence_failure_leaves_main_manifest_unchanged(
    project_tmp,
    monkeypatch,
):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = _manifest_path(project_tmp).read_bytes()
    real_persist = generate_module.persist_exact_bytes
    calls: list[str] = []

    def persist(path, content):
        calls.append(path.name)
        if path.name == "context-manifest.yaml":
            raise OSError("C:/private/context failure")
        return real_persist(path, content)

    monkeypatch.setattr(generate_module, "persist_exact_bytes", persist)

    code, messages = run_generate(project_tmp, slug)

    assert code == 1
    assert calls == ["agent-workset.md", "context-manifest.yaml"]
    assert _workset_path(project_tmp, slug).is_file()
    assert not _context_manifest_path(project_tmp, slug).exists()
    assert _manifest_path(project_tmp).read_bytes() == manifest_before
    assert "generated pair is incomplete" in "\n".join(messages)
    assert "C:/private" not in "\n".join(messages)


def test_force_dry_run_reports_both_outputs_and_writes_nothing(project_tmp):
    slug = _start_sample_task(project_tmp)
    _fill_task_inputs(project_tmp, slug)
    manifest_before = _manifest_path(project_tmp).read_bytes()

    code, messages = run_generate(project_tmp, slug, dry_run=True, force=True)

    assert code == 0
    assert any("agent-workset.md" in message and "would" in message for message in messages)
    assert any("context-manifest.yaml" in message and "would" in message for message in messages)
    assert messages[-1] == "dry run; no files written"
    assert not _workset_path(project_tmp, slug).exists()
    assert not _context_manifest_path(project_tmp, slug).exists()
    assert _manifest_path(project_tmp).read_bytes() == manifest_before
