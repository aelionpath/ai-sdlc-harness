"""Native repository Agent Skill adapters for supported coding agents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .constants import ADAPTER_ARTIFACT_PATHS
from .files import (
    PathSafetyError,
    persist_exact_bytes,
    resolve_managed_output_under_root,
    sha256_bytes,
)
from .manifest import (
    MainManifest,
    ManagedFileRecord,
    build_managed_file_record,
    load_manifest_model,
    persist_manifest_model,
    replace_managed_file_records,
)


ADAPTER_SKILL_MAX_BYTES = 4 * 1024


@dataclass(frozen=True)
class AdapterDefinition:
    adapter_id: str
    display_name: str
    skill_path: PurePosixPath
    root_instruction_file: str


ADAPTER_DEFINITIONS = (
    AdapterDefinition("codex", "Codex", ADAPTER_ARTIFACT_PATHS[0], "AGENTS.md"),
    AdapterDefinition(
        "claude-code",
        "Claude Code",
        ADAPTER_ARTIFACT_PATHS[1],
        "CLAUDE.md",
    ),
    AdapterDefinition(
        "gemini-cli",
        "Gemini CLI",
        ADAPTER_ARTIFACT_PATHS[2],
        "GEMINI.md",
    ),
)
ADAPTER_IDS = tuple(item.adapter_id for item in ADAPTER_DEFINITIONS)
_ADAPTERS_BY_ID = {item.adapter_id: item for item in ADAPTER_DEFINITIONS}


_SKILL_TEXT = """---
name: ai-sdlc-harness
description: Use this skill for coding, implementation, refactoring, debugging, testing, or code-review work in a repository initialized with AI SDLC Harness (.harness). It reads deterministic workflow status, follows the task-scoped agent workset, preserves Harness-managed outputs, and records factual evidence without bypassing human review.
---

# AI SDLC Harness

Use this thin bridge only for Harness-governed work in an initialized repository.

You execute the engineering work. Harness is the deterministic control layer and workflow record; the user retains authority. Harness does not invoke or orchestrate you.

## Start from structured status

- Run `ai-sdlc status --json` at the beginning. If the task slug is already known, run `ai-sdlc status --task <slug> --json`.
- Do not infer the workflow phase from file existence or parse the human status output when structured status is available.
- If multiple tasks leave `selected_task` empty, do not guess or persist a hidden choice. Ask the user which task to use, then rerun status with `--task <slug> --json`.
- Use `phase`, `outcome`, `next_actions`, the four-key `lineage` state, and validation state as the workflow navigator. For `NEXT`, you may run a deterministic Harness producer command in `next_actions` for the selected task unless that contradicts the user's instructions. After a state-changing Harness command, rerun structured status. Continue through routine safe, bounded work rather than stopping after every Harness command. Never fabricate a successful next state.
- Stop and ask the user when a real authority or approval decision is required, required information is unavailable, scope or intent is consequentially ambiguous, Harness reports `BLOCKED`, or progress would require changing authoritative intent merely to unblock yourself. Do not bypass `REVIEW_REQUIRED`; you may prepare supporting material, but never accept or approve for the user.

## Use the implementation handoff

When status reaches implementation, read `.harness/tasks/<slug>/generated/agent-workset.md` as the bounded workflow handoff. Load repository code and files as needed, but do not recursively load the whole `.harness` tree.

Human-owned task source artifacts are the authoritative workflow inputs. You may assist with them within the user's stated intent; human-owned does not mean human-authored-only. Do not invent missing requirements, redefine intent, or accept human review for the user. Follow the baseline secure-engineering guardrails in the workset. Generated Harness outputs are not direct-edit surfaces. Never directly edit `preflight.md`, `spec.md`, `requirements.yaml`, `test-contract-review.md`, `generated/agent-workset.md`, `generated/context-manifest.yaml`, `evidence-report.md`, or `validation-report.md`; use the corresponding `ai-sdlc` producer command.

Do not directly edit Harness control-plane files such as `.harness/config.yaml`, `.harness/state.json`, `.harness/manifest.json`, `.harness/generated/agent-instructions.md`, or `.harness/packs/selected.yaml`.

## Record evidence and stop at review

After code, test, or configuration changes, record only factual implementation evidence in `evidence.md`. Record actual verification commands and observed results in `verification.md`. Never claim a command ran when it did not, and do not treat clean Harness validation as proof of correctness, security, or compliance.

Rerun structured status and continue the bounded loop until a stop condition above applies. If status reports `COMPLETE`, stop Harness workflow automation, report that the Harness workflow record is complete, and preserve explicit human review and normal delivery boundaries. `COMPLETE` is not proof of correctness, security, compliance, approval, or release readiness.
"""


@dataclass(frozen=True)
class _InstallPlan:
    definition: AdapterDefinition
    target: Path
    existed: bool
    expected_bytes: bytes
    record: ManagedFileRecord | None
    needs_file_write: bool
    needs_manifest_update: bool


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def render_adapter_skill(adapter_id: str) -> bytes:
    """Render deterministic, semantically common skill bytes for one adapter."""

    if adapter_id not in _ADAPTERS_BY_ID:
        raise ValueError(f"unsupported adapter ID: {adapter_id}")
    content = _SKILL_TEXT.encode("utf-8")
    if len(content) > ADAPTER_SKILL_MAX_BYTES:
        raise ValueError("rendered adapter skill exceeds the 4 KiB limit")
    return content


def _selected_definitions(adapter_id: str) -> tuple[AdapterDefinition, ...]:
    if adapter_id == "all":
        return ADAPTER_DEFINITIONS
    definition = _ADAPTERS_BY_ID.get(adapter_id)
    if definition is None:
        raise ValueError(f"unsupported adapter ID: {adapter_id}")
    return (definition,)


def _valid_owned_record(record: ManagedFileRecord) -> bool:
    return (
        record.protected
        and record.hash_algorithm == "sha256"
        and record.sha256 is not None
    )


def _plan_adapter(
    root: Path,
    definition: AdapterDefinition,
    manifest: MainManifest,
    *,
    force: bool,
) -> _InstallPlan:
    path_text = definition.skill_path.as_posix()
    target = resolve_managed_output_under_root(root, definition.skill_path)
    expected_bytes = render_adapter_skill(definition.adapter_id)
    expected_hash = sha256_bytes(expected_bytes)
    records = {record.path: record for record in manifest.managed_files}
    record = records.get(path_text)

    try:
        exists = target.exists()
    except OSError as exc:
        raise OSError(f"could not inspect adapter path {path_text}") from exc

    if exists and (target.is_symlink() or not target.is_file()):
        raise ValueError(f"adapter path is not a safe regular file: {path_text}")
    if exists and record is None:
        raise ValueError(f"unmanaged existing adapter file: {path_text}")
    if record is not None and not _valid_owned_record(record):
        raise ValueError(f"invalid adapter ownership record: {path_text}")

    if not exists:
        if record is not None and not force:
            raise ValueError(
                f"managed adapter file is missing: {path_text}; rerun with --force to restore it"
            )
        return _InstallPlan(
            definition,
            target,
            False,
            expected_bytes,
            record,
            True,
            True,
        )

    try:
        current_bytes = target.read_bytes()
    except OSError as exc:
        raise OSError(f"could not read adapter file {path_text}") from exc
    current_hash = sha256_bytes(current_bytes)
    assert record is not None
    if current_hash != record.sha256 and not force:
        raise ValueError(
            f"hash drift detected for {path_text}; rerun with --force to restore it"
        )
    if current_bytes != expected_bytes and not force:
        raise ValueError(
            f"managed adapter content differs from the current template: {path_text}; "
            "rerun with --force to refresh it"
        )

    return _InstallPlan(
        definition,
        target,
        True,
        expected_bytes,
        record,
        current_bytes != expected_bytes,
        record.sha256 != expected_hash,
    )


def _result_messages(plans: tuple[_InstallPlan, ...], *, dry_run: bool) -> list[str]:
    messages: list[str] = []
    for plan in plans:
        path_text = plan.definition.skill_path.as_posix()
        if dry_run:
            action = "planned" if plan.needs_file_write or plan.needs_manifest_update else "skipped"
        else:
            action = "installed" if plan.needs_file_write or plan.needs_manifest_update else "skipped"
        messages.extend(
            (
                f"{action} {plan.definition.display_name} adapter: {path_text}",
                f"agent ID: {plan.definition.adapter_id}",
                f"root {plan.definition.root_instruction_file} was not modified",
            )
        )
    if dry_run:
        messages.append("dry run; no files written")
    return messages


def install_adapters(
    root: Path,
    adapter_id: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[int, list[str]]:
    """Install one or all native repository adapter skills safely."""

    try:
        definitions = _selected_definitions(adapter_id)
    except ValueError as exc:
        return 2, [str(exc)]

    try:
        manifest_path = resolve_managed_output_under_root(
            root, PurePosixPath(".harness/manifest.json")
        )
        if not (root / ".harness").is_dir() or not manifest_path.is_file():
            return 1, [
                "AI SDLC Harness is not initialized. Run `ai-sdlc init` from the project root."
            ]
        manifest = load_manifest_model(manifest_path)
    except (OSError, RuntimeError, ValueError, PathSafetyError):
        return 1, ["Could not load a safe, valid Harness manifest for adapter installation."]

    plans: list[_InstallPlan] = []
    blockers: list[str] = []
    for definition in definitions:
        try:
            plans.append(_plan_adapter(root, definition, manifest, force=force))
        except PathSafetyError:
            blockers.append(
                f"could not safely inspect adapter path: {definition.skill_path.as_posix()}"
            )
        except ValueError as exc:
            blockers.append(str(exc))
        except (OSError, RuntimeError):
            blockers.append(
                f"could not safely inspect adapter path: {definition.skill_path.as_posix()}"
            )
    if blockers:
        return 1, ["Adapter installation blocked.", *(f"- {item}" for item in blockers)]

    planned = tuple(plans)
    if dry_run:
        return 0, _result_messages(planned, dry_run=True)

    persisted_paths: list[str] = []
    try:
        for plan in planned:
            if plan.needs_file_write:
                persist_exact_bytes(plan.target, plan.expected_bytes)
                persisted_paths.append(plan.definition.skill_path.as_posix())
    except Exception:
        return 1, [
            "Could not persist all planned adapter skill files.",
            "The Harness manifest was not refreshed.",
        ]

    try:
        replacements = tuple(
            build_managed_file_record(root, plan.definition.skill_path)
            for plan in planned
        )
        current_by_path = {record.path: record for record in manifest.managed_files}
        manifest_changed = any(
            current_by_path.get(record.path) != record for record in replacements
        )
        if manifest_changed:
            updated = replace_managed_file_records(
                manifest,
                replacements,
                generated_at=_timestamp(),
            )
            persist_manifest_model(root, updated)
    except Exception:
        paths = ", ".join(persisted_paths) or "none"
        return 1, [
            "Adapter skill files were persisted, but the Harness manifest could not be refreshed.",
            f"persisted adapter paths: {paths}",
            "The manifest does not claim the current adapter state.",
        ]

    return 0, _result_messages(planned, dry_run=False)
