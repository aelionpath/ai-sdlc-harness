"""Implementation of ``ai-sdlc generate``."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

from .constants import (
    AGENT_WORKSET_FILENAME,
    CONTEXT_MANIFEST_FILENAME,
    TASK_GENERATED_DIRNAME,
    TASK_SLUG_PATTERN,
)
from .context import (
    ARTIFACT_ROLE,
    AUTHORITY,
    EDIT_MODEL,
    SCHEMA_VERSION,
    WORKSET_ARTIFACT_ROLE,
    AuthorityLevel,
    BudgetConfiguration,
    ContextFinding,
    ContextManifest,
    ContextSelectionResult,
    ExistenceState,
    FindingLevel,
    FreshnessState,
    GeneratedArtifact,
    RepositoryObservations,
    SelectedContextEntry,
    SizeEstimate,
    SourceArtifact,
    context_manifest_path,
    measure_text,
    render_context_manifest_yaml,
    resolve_budget_configuration,
    select_context,
    validate_context_manifest,
)
from .detect import detect_project_signals
from .files import (
    PathSafetyError,
    persist_exact_bytes,
    read_text,
    resolve_managed_output_under_root,
    resolve_under_root,
    sha256_bytes,
)
from .lineage import resolve_derived_freshness
from .manifest import (
    MainManifest,
    build_managed_file_record,
    load_manifest_model,
    persist_manifest_model,
    replace_managed_file_records,
    sha256_file,
)


TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)

DISCLAIMER = (
    "This workset compiles task-scoped context for implementation. It does not call AI models, "
    "generate code, generate tests, run tests, inspect target code deeply, scan for security issues or vulnerabilities, "
    "query external security or reputation services, validate compliance, prove correctness, security, or release "
    "readiness, approve releases, enforce packs, or install adapters."
)

_CONFIGURATION_PATH = ".harness/config.yaml"
_CONFIGURATION_UNAVAILABLE = ContextFinding(
    code="generation_configuration_unavailable",
    level=FindingLevel.BLOCKER,
    path=_CONFIGURATION_PATH,
    message=(
        "Harness configuration could not be read for context selection: "
        ".harness/config.yaml."
    ),
)


@dataclass(frozen=True)
class GenerationPreparation:
    """Successful in-memory inputs for blocker evaluation and workset rendering."""

    selection: ContextSelectionResult
    findings: tuple[ContextFinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.selection, ContextSelectionResult):
            raise TypeError("selection must use ContextSelectionResult")
        if not isinstance(self.findings, tuple):
            raise TypeError("findings must be a tuple")
        if any(not isinstance(finding, ContextFinding) for finding in self.findings):
            raise TypeError("findings items must use ContextFinding")
        if self.findings != _canonical_findings(self.findings):
            raise ValueError("findings must be canonical and deduplicated")

    @property
    def blockers(self) -> tuple[ContextFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.level == FindingLevel.BLOCKER
        )


@dataclass(frozen=True)
class _GenerateOutput:
    path: PurePosixPath
    target: Path
    existed: bool


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _task_dir_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug


def _workset_path(slug: str) -> PurePosixPath:
    return _task_dir_path(slug) / TASK_GENERATED_DIRNAME / AGENT_WORKSET_FILENAME


def _context_manifest_path(slug: str) -> PurePosixPath:
    return (
        _task_dir_path(slug)
        / TASK_GENERATED_DIRNAME
        / CONTEXT_MANIFEST_FILENAME
    )


def _validate_task_slug(slug: str) -> None:
    if not slug or not slug.strip():
        raise ValueError("Task slug must not be empty.")
    if "\x00" in slug:
        raise ValueError("Task slug contains an unsafe NUL character.")
    if TASK_SLUG_RE.fullmatch(slug) is None:
        raise ValueError("Task slug is unsafe. Use the safe slug created by `ai-sdlc task start`.")


def _finding_sort_key(finding: ContextFinding) -> tuple[int, str, str, str]:
    severity = {
        FindingLevel.BLOCKER: 0,
        FindingLevel.WARNING: 1,
        FindingLevel.INFO: 2,
    }
    return (
        severity[finding.level],
        finding.code,
        finding.path or "",
        finding.message,
    )


def _canonical_findings(
    findings: tuple[ContextFinding, ...] | list[ContextFinding],
) -> tuple[ContextFinding, ...]:
    return tuple(sorted(set(findings), key=_finding_sort_key))


def _load_budget_configuration(
    root: Path,
) -> tuple[BudgetConfiguration, tuple[ContextFinding, ...]]:
    try:
        configuration_path = resolve_under_root(root, _CONFIGURATION_PATH)
        if not configuration_path.is_file():
            raise OSError("configuration is not a regular file")
        loaded = yaml.safe_load(read_text(configuration_path))
        if not isinstance(loaded, Mapping):
            raise ValueError("configuration top level is not a mapping")
        configuration_data: Mapping[str, Any] = loaded
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, PathSafetyError):
        default_configuration, _ = resolve_budget_configuration({})
        return default_configuration, (_CONFIGURATION_UNAVAILABLE,)

    budget_configuration, budget_findings = resolve_budget_configuration(
        configuration_data
    )
    return (
        budget_configuration,
        _canonical_findings(list(budget_findings)),
    )


def _signal_values(signals: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = signals.get(key, ())
    if isinstance(value, (str, bytes, Mapping)) or value is None:
        return ()
    try:
        return tuple(sorted({str(item) for item in value if str(item)}))
    except TypeError:
        return ()


def _repository_observations(
    signals: Mapping[str, Any],
) -> RepositoryObservations:
    return RepositoryObservations(
        available=True,
        git_repo=bool(signals.get("git_repo", False)),
        detected_languages=_signal_values(signals, "detected_languages"),
        detected_package_managers=_signal_values(
            signals,
            "detected_package_managers",
        ),
        detected_test_frameworks=_signal_values(
            signals,
            "detected_test_frameworks",
        ),
        detected_ci=_signal_values(signals, "detected_ci"),
        existing_agent_files=_signal_values(signals, "existing_agent_files"),
    )


def _prepare_generation(root: Path, task_slug: str) -> GenerationPreparation:
    lineage_result = resolve_derived_freshness(root, task_slug)
    budget_configuration, configuration_findings = _load_budget_configuration(root)
    observations = _repository_observations(detect_project_signals(root))
    selection = select_context(
        root,
        task_slug,
        derived_freshness=lineage_result.derived_freshness,
        budget_configuration=budget_configuration,
        repository_observations=observations,
    )
    findings = _canonical_findings(
        [
            *lineage_result.findings,
            *configuration_findings,
            *selection.findings,
        ]
    )
    return GenerationPreparation(selection=selection, findings=findings)


def _render_selected_entry(entry: SelectedContextEntry) -> list[str]:
    lines = [
        f"### `{entry.entry_id}`",
        "",
        f"- Path: `{entry.path}`" if entry.path is not None else "- Path: none (current shallow observation)",
        f"- Classification: `{entry.classification.value}`",
        f"- Authority: `{entry.authority_level.value}`",
        f"- Inclusion: `{entry.inclusion_mode.value}`",
        f"- Freshness: `{entry.freshness_state.value}`",
        "",
        "Selected content:",
        "",
    ]
    lines.extend(
        f"    {line}" if line else ""
        for line in entry.selected_content.splitlines()
    )
    if entry.report_findings:
        lines.extend(["", "Selected report findings:", ""])
        lines.extend(f"- {finding}" for finding in entry.report_findings)
    lines.append("")
    return lines


def _render_workset(
    *,
    slug: str,
    generated_at: str,
    selection: ContextSelectionResult,
    findings: tuple[ContextFinding, ...],
) -> str:
    budget = selection.budget
    configuration = budget.configuration
    result = budget.result
    lines = [
        "# Agent Workset",
        "",
        f"Task slug: `{slug}`",
        f"Generated: {generated_at}",
        "",
        "## Summary",
        "",
        "- Purpose: pre-implementation context for a human or coding agent before changing code.",
        f"- Selected context entries: {len(selection.selected_entries)}",
        f"- Advisory context-budget status: `{result.status.value}`",
        "- Context is authority ordered, bounded, redacted, and selected from the current repository state.",
        "- Post-implementation reports and generated handoff outputs are not implementation inputs.",
        "",
        "## Implementation Contract",
        "",
        "- This is the task-scoped implementation contract compiled from current task artifacts.",
        "- Treat authoritative task sources as controlling when they differ from advisory projections or reports.",
        "- Implement only after blocker findings are resolved or explicitly accepted by the human.",
        "- Treat this as bounded implementation guidance for the selected task only.",
        "- Stay within task scope and preserve documented assumptions.",
        "- Preserve protected areas and non-goals unless the human explicitly changes them.",
        "- Do not silently change public behavior outside task scope.",
        "- Treat interface impact, security/privacy risk-surface notes, and unresolved questions as review prompts.",
        "- Preserve compatibility unless the task artifacts explicitly change it.",
        "- Document implementation deviations and unresolved risks in `evidence.md`.",
        "",
        "## Baseline Secure-Engineering Guardrails",
        "",
        "Apply when relevant to the task:",
        "",
        "- Do not introduce hard-coded secrets, credentials, tokens, private keys, or sensitive production data, or expose sensitive values in logs, errors, evidence, or generated artifacts.",
        "- Validate untrusted input at trust boundaries and reject malformed or unsupported input safely; avoid unsafe interpolation, evaluation, or deserialization patterns when relevant.",
        "- Preserve authorization checks and least privilege; do not broaden access, capability, or authority implicitly.",
        "- Fail safely without exposing credentials, sensitive data, implementation secrets, or excessive internals; preserve useful diagnostics for the intended audience.",
        "- For security-sensitive, trust-boundary, authorization, parsing, or configuration changes, include appropriate negative or abuse-path verification; when automation is impractical, record the bounded manual approach and why.",
        "- Minimize new dependencies and treat dependency or security-relevant configuration changes, including privilege or capability expansion, as explicit review and evidence items.",
        "- Record factual evidence only. A clean Harness workflow is not proof of correctness, security, or compliance.",
        "",
        "## Selected Context",
        "",
    ]
    for entry in selection.selected_entries:
        lines.extend(_render_selected_entry(entry))

    lines.extend(
        [
            "## Advisory Context Budget",
            "",
            f"- Status: `{result.status.value}`",
            f"- Selected entry count: {result.selected_entry_count}",
            f"- Selected bytes: {result.selected_bytes}",
            f"- Selected characters: {result.selected_characters}",
            f"- Selected lines: {result.selected_lines}",
            f"- Selected approximate tokens: {result.selected_approximate_tokens}",
            (
                "- Per-file warning approximate-token threshold: "
                f"{configuration.per_file_warning_approximate_tokens}"
            ),
            (
                "- Per-file high-risk approximate-token threshold: "
                f"{configuration.per_file_high_risk_approximate_tokens}"
            ),
            (
                "- Total warning approximate-token threshold: "
                f"{configuration.total_warning_approximate_tokens}"
            ),
            (
                "- Total high-risk approximate-token threshold: "
                f"{configuration.total_high_risk_approximate_tokens}"
            ),
            "- Budget findings are advisory and do not certify context sufficiency.",
            "",
            "## Findings",
            "",
        ]
    )
    if findings:
        for finding in findings:
            path = f" [`{finding.path}`]" if finding.path is not None else ""
            lines.append(
                f"- {finding.level.value}: `{finding.code}`: {finding.message}{path}"
            )
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Implementation Instructions",
            "",
            "- Implement only the task described by the task artifacts.",
            "- Stay within the task boundary.",
            "- Preserve documented assumptions unless the human explicitly changes them.",
            "- Preserve protected areas, non-goals, and existing interfaces unless explicitly changed in the task artifacts.",
            "- Do not silently change public behavior outside task scope.",
            "- Do not silently expand scope.",
            "- Preserve compatibility unless explicitly changed in the task artifacts.",
            "- Treat interface impact, security/privacy risk-surface notes, and unresolved questions as review prompts.",
            "- Implement only after blocker findings are resolved or explicitly accepted by the human.",
            "- Update tests according to the planned test and verification intent in `test-contract.md`.",
            "- Record deviations, commands run, results, not-run rationale, gaps, and references in `evidence.md` and `verification.md`.",
            "",
            "## Human Review Notes",
            "",
            "- [ ] Implementation summary recorded in `evidence.md`.",
            "- [ ] Protected areas, interface impact, and unresolved questions reviewed.",
            "- [ ] Tests updated according to `test-contract.md` intent.",
            "- [ ] Security/privacy risk-surface notes reviewed where applicable.",
            "- [ ] Verification commands and results recorded in `verification.md` or `evidence.md`.",
            "- [ ] Deviations from this workset recorded in `evidence.md`.",
            "- [ ] Unresolved risks or follow-up work recorded in `evidence.md`.",
            "- [ ] Any blocker findings were resolved or explicitly accepted by the human.",
            "",
            "## Disclaimer",
            "",
            DISCLAIMER,
        ]
    )
    return "\n".join(lines).rstrip("\n") + "\n"


def _build_context_manifest(
    *,
    preparation: GenerationPreparation,
    workset_text: str,
    workset_sha256: str,
) -> ContextManifest:
    """Build the machine record from one selection and confirmed workset hash."""

    selection = preparation.selection
    source_artifacts = tuple(
        SourceArtifact(
            path=artifact.path or "",
            authority_level=artifact.authority_level,
            existence=artifact.existence,
            sha256=artifact.source_sha256,
        )
        for artifact in selection.source_artifacts
    )
    workset_size = measure_text(workset_text)
    context_entries = tuple(
        replace(
            entry,
            existence=ExistenceState.PRESENT,
            freshness_state=FreshnessState.NOT_APPLICABLE,
            size_estimate=SizeEstimate(
                source=workset_size,
                selected=entry.size_estimate.selected,
            ),
        )
        if entry.entry_id == "agent_workset_output"
        else entry
        for entry in selection.context_entries
    )
    return validate_context_manifest(
        ContextManifest(
            schema_version=SCHEMA_VERSION,
            artifact_path=context_manifest_path(selection.task_slug),
            artifact_role=ARTIFACT_ROLE,
            authority=AUTHORITY,
            edit_model=EDIT_MODEL,
            task_slug=selection.task_slug,
            source_artifacts=source_artifacts,
            generated_artifacts=(
                GeneratedArtifact(
                    path=_workset_path(selection.task_slug).as_posix(),
                    artifact_role=WORKSET_ARTIFACT_ROLE,
                    authority_level=AuthorityLevel.HANDOFF_OUTPUT,
                    sha256=workset_sha256,
                ),
            ),
            context_entries=context_entries,
            findings=preparation.findings,
            budget=selection.budget,
        )
    )


def _output_action(output: _GenerateOutput, *, changed: bool) -> str:
    if not changed:
        return "skip unchanged"
    return "refresh" if output.existed else "create"


def _preflight_generate_outputs(
    root: Path,
    task_slug: str,
    *,
    force: bool,
) -> tuple[MainManifest, tuple[_GenerateOutput, _GenerateOutput]]:
    paths = (_workset_path(task_slug), _context_manifest_path(task_slug))
    targets = tuple(resolve_managed_output_under_root(root, path) for path in paths)
    manifest = load_manifest_model(resolve_under_root(root, ".harness/manifest.json"))
    records = {record.path: record for record in manifest.managed_files}
    outputs: list[_GenerateOutput] = []

    for path, target in zip(paths, targets):
        path_text = path.as_posix()
        record = records.get(path_text)
        try:
            exists = target.exists()
        except OSError as exc:
            raise OSError(f"could not inspect generate output {path_text}") from exc
        if exists and record is None:
            raise ValueError(f"unmanaged existing file {path_text}")
        if exists and record is not None:
            try:
                clean = (
                    record.hash_algorithm == "sha256"
                    and record.sha256 is not None
                    and sha256_file(target) == record.sha256
                )
            except OSError as exc:
                raise OSError(
                    f"could not inspect generate output bytes {path_text}"
                ) from exc
            if not clean and not force:
                raise ValueError(f"hash drift detected for {path_text}")
        outputs.append(
            _GenerateOutput(
                path=path,
                target=target,
                existed=exists,
            )
        )
    return manifest, (outputs[0], outputs[1])


def _blocker_messages(
    blockers: tuple[ContextFinding, ...],
) -> list[str]:
    messages = ["Cannot generate agent workset."]
    for finding in blockers:
        path_suffix = ""
        if finding.path is not None and finding.path not in finding.message:
            path_suffix = f" [{finding.path}]"
        messages.append(f"- {finding.code}: {finding.message}{path_suffix}")
    return messages


def _summary(preparation: GenerationPreparation) -> str:
    result = preparation.selection.budget.result
    warnings = sum(
        finding.level == FindingLevel.WARNING
        for finding in preparation.findings
    )
    return (
        f"summary: selected {result.selected_entry_count} context entries; "
        f"budget {result.status.value}; {warnings} warning(s)"
    )


def run_generate(
    root: Path,
    task_slug: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[int, list[str]]:
    harness_root = root / ".harness"
    if (
        not harness_root.is_dir()
        or not (harness_root / "manifest.json").is_file()
    ):
        return 1, [
            "AI SDLC Harness is not initialized. "
            "Run `ai-sdlc init` from the project root."
        ]

    try:
        _validate_task_slug(task_slug)
    except ValueError as exc:
        return 2, [str(exc)]

    task_dir = _task_dir_path(task_slug)
    try:
        task_target = resolve_under_root(root, task_dir)
    except (OSError, RuntimeError, PathSafetyError):
        return 1, [
            f"Task folder is unavailable: {task_dir.as_posix()}. "
            "Run `ai-sdlc task start` first."
        ]
    if not task_target.is_dir():
        return 1, [
            f"Task folder does not exist: {task_dir.as_posix()}. "
            "Run `ai-sdlc task start` first."
        ]

    try:
        current_manifest, outputs = _preflight_generate_outputs(
            root,
            task_slug,
            force=force,
        )
    except ValueError as exc:
        detail = str(exc)
        if detail.startswith("unmanaged existing file "):
            return 1, [
                detail,
                "refusing to overwrite unmanaged generate-owned output",
            ]
        if detail.startswith("hash drift detected for "):
            return 1, [
                detail,
                "refusing to overwrite manifest-managed generate-owned output without --force",
            ]
        return 1, [
            "Could not read the Harness manifest or safely inspect both "
            "generate-owned output paths."
        ]
    except Exception:
        return 1, [
            "Could not read the Harness manifest or safely inspect both "
            "generate-owned output paths."
        ]

    workset_output, context_manifest_output = outputs
    workset_path_text = workset_output.path.as_posix()
    context_manifest_path_text = context_manifest_output.path.as_posix()

    try:
        preparation = _prepare_generation(root, task_slug)
    except Exception:
        return 1, [
            "Cannot generate agent workset.",
            "- generation_preparation_unavailable: Context selection could not "
            "be prepared from the current repository state.",
        ]

    if preparation.blockers:
        return 1, _blocker_messages(preparation.blockers)

    generated_at = _timestamp()
    workset_text = _render_workset(
        slug=task_slug,
        generated_at=generated_at,
        selection=preparation.selection,
        findings=preparation.findings,
    )
    workset_bytes = workset_text.encode("utf-8")
    planned_workset_sha256 = sha256_bytes(workset_bytes)
    messages = [
        f"agent workset: {workset_path_text}",
        f"context manifest: {context_manifest_path_text}",
        _summary(preparation),
    ]
    if dry_run:
        try:
            context_manifest = _build_context_manifest(
                preparation=preparation,
                workset_text=workset_text,
                workset_sha256=planned_workset_sha256,
            )
            context_manifest_bytes = render_context_manifest_yaml(
                context_manifest
            ).encode("utf-8")
            workset_changed = (
                not workset_output.existed
                or workset_output.target.read_bytes() != workset_bytes
            )
            context_manifest_changed = (
                not context_manifest_output.existed
                or context_manifest_output.target.read_bytes()
                != context_manifest_bytes
            )
        except OSError:
            return 1, ["Could not inspect current generate-owned output bytes."]
        except Exception:
            return 1, [
                "Cannot generate the output pair.",
                "- context_manifest_construction_failed: The context manifest could "
                "not be built from the current selection snapshot.",
            ]
        messages.append(
            f"would {_output_action(workset_output, changed=workset_changed)} "
            f"file {workset_path_text}"
        )
        messages.append(
            f"would {_output_action(context_manifest_output, changed=context_manifest_changed)} "
            f"file {context_manifest_path_text}"
        )
        messages.append("dry run; no files written")
        return 0, messages

    try:
        persisted_workset = persist_exact_bytes(
            workset_output.target,
            workset_bytes,
        )
    except Exception:
        return 1, [
            "Could not persist agent workset.",
            f"agent workset was not confirmed at {workset_path_text}.",
        ]

    try:
        context_manifest = _build_context_manifest(
            preparation=preparation,
            workset_text=workset_text,
            workset_sha256=persisted_workset.sha256,
        )
        context_manifest_bytes = render_context_manifest_yaml(
            context_manifest
        ).encode("utf-8")
        persisted_context_manifest = persist_exact_bytes(
            context_manifest_output.target,
            context_manifest_bytes,
        )
    except Exception:
        return 1, [
            "Agent workset was confirmed, but the context manifest could not be persisted.",
            "The generated pair is incomplete and .harness/manifest.json was not refreshed.",
        ]

    try:
        replacement_records = (
            build_managed_file_record(root, workset_output.path),
            build_managed_file_record(root, context_manifest_output.path),
        )
        confirmed_hashes = (
            persisted_workset.sha256,
            persisted_context_manifest.sha256,
        )
        if tuple(record.sha256 for record in replacement_records) != confirmed_hashes:
            raise ValueError("generated output confirmation hash mismatch")
        current_by_path = {
            record.path: record for record in current_manifest.managed_files
        }
        manifest_changed = any(
            current_by_path.get(record.path) != record
            for record in replacement_records
        )
        if manifest_changed:
            updated_manifest = replace_managed_file_records(
                current_manifest,
                replacement_records,
                generated_at=generated_at,
            )
            persist_manifest_model(root, updated_manifest)
    except Exception:
        return 1, [
            "Generated files were persisted, but the Harness manifest could not be refreshed.",
            "The manifest does not yet record the current generated pair.",
        ]

    messages.append(
        f"{_output_action(workset_output, changed=persisted_workset.changed)} "
        f"file {workset_path_text}"
    )
    messages.append(
        f"{_output_action(context_manifest_output, changed=persisted_context_manifest.changed)} "
        f"file {context_manifest_path_text}"
    )
    messages.append(
        "refreshed manifest .harness/manifest.json"
        if manifest_changed
        else "skip existing manifest .harness/manifest.json"
    )
    messages.append("root AGENTS.md, CLAUDE.md, and GEMINI.md were not modified")
    messages.append(".harness/generated/agent-instructions.md was not modified")
    return 0, messages
