# CLI Reference

## `ai-sdlc --help`

Shows the implemented command list.

## `ai-sdlc init`

Creates `.harness/` in the current project.

Options:

- `--dry-run`: show planned creates/skips without writing files.
- `--force`: rewrite known managed `.harness/` files only.
- `--agent none|codex|claude-code|both`: record a requested adapter. Init records the request but does not install adapters.

## `ai-sdlc status`

Read-only status report for the current project.

The command summarizes `.harness/` structure, selected optional pack records, task counts, manifest-managed task artifacts, and generated report/workset counts. It does not modify files.

## `ai-sdlc verify`

Read-only integrity check for `.harness/` structure, parseable files, selected optional pack records, and protected file hashes.

`verify` checks manifest and file integrity only. It does not inspect implementation semantics, run tests, scan for security issues, validate compliance, prove correctness, approve work, or determine release readiness.

## `ai-sdlc preflight --task <task-slug>`

Creates a task-readiness report at `.harness/tasks/<task-slug>/preflight.md`.

Options:

- `--task <task-slug>`: required existing task slug created by `task start`.
- `--dry-run`: print the report path and summary without writing files.
- `--force`: rewrite only manifest-managed `preflight.md` for the selected task.

The command requires `ai-sdlc init` first and an existing task folder. It checks shallow repository markers, selected pack records, and task artifact readiness. It does not perform semantic validation, security scanning, pack enforcement, AI review, or application code changes.

Existing unmanaged `preflight.md` files are not overwritten, even with `--force`.

## `ai-sdlc spec --task <task-slug>`

Creates a deterministic Markdown implementation-intent spec at `.harness/tasks/<task-slug>/spec.md` and an advisory structured requirements projection at `.harness/tasks/<task-slug>/requirements.yaml`.

Options:

- `--task <task-slug>`: required existing task slug created by `task start`.
- `--dry-run`: print the spec and requirements paths and summary without writing files.
- `--force`: rewrite only manifest-managed `spec.md` and `requirements.yaml` for the selected task.

The command requires `ai-sdlc init` first and an existing task folder. It consolidates existing task artifacts and readable `preflight.md` findings into blocker, warning, and info signals. Missing, unreadable, empty, or TODO-only task sections are recorded as findings in the generated Markdown instead of stopping report generation.

`requirements.yaml` is generated from explicit list items in `acceptance.md` and is advisory, not authoritative, in Phase 3C. To change requirements, edit source task artifacts and rerun `spec`.

Existing unmanaged `spec.md` or `requirements.yaml` files are not claimed or overwritten, even with `--force`. Hash-drifted manifest-managed spec artifacts are refused unless `--force` is used. The command never overwrites task input files, root `AGENTS.md`, root `CLAUDE.md`, `.harness/generated/agent-instructions.md`, generated worksets, evidence reports, validation reports, packs, or adapters.

The command does not ask interactive questions, call AI models, import requirements, run tests, inspect target code deeply, scan security, validate compliance, enforce packs, prove correctness, determine approval, or determine release-readiness.

## `ai-sdlc test-contract --task <task-slug>`

Creates a test-contract readiness review at `.harness/tasks/<task-slug>/test-contract-review.md`.

Options:

- `--task <task-slug>`: required existing task slug created by `task start`.
- `--dry-run`: print the report path and summary without writing files.
- `--force`: rewrite only manifest-managed `test-contract-review.md` for the selected task.

The command requires `ai-sdlc init` first and an existing task folder. It reviews existing acceptance, test-contract, and verification intent with shallow marker-based checks. It does not generate tests, run tests, enforce pack semantics, determine compliance, execute packs, call AI models, or change application code.

Existing unmanaged `test-contract-review.md` files are not claimed or overwritten, even with `--force`. The command never overwrites `test-contract.md`.

## `ai-sdlc generate --task <task-slug>`

Creates deterministic task-scoped agent workset context at `.harness/tasks/<task-slug>/generated/agent-workset.md`.

Options:

- `--task <task-slug>`: required existing task slug created by `task start`.
- `--dry-run`: print the workset path and summary without writing files.
- `--force`: rewrite only manifest-managed `generated/agent-workset.md` for the selected task.

The command requires `ai-sdlc init` first and an existing task folder. It compiles bounded excerpts from required task artifacts, optional readiness reports when present, optional `spec.md`, optional `requirements.yaml`, optional selected pack records, repository signals, coding-agent instructions, warnings, and a completion evidence checklist.

Required task inputs:

- `task.md`
- `acceptance.md`
- `architecture-notes.md`
- `coupling-notes.md`
- `test-contract.md`
- `verification.md`
- `evidence.md`

The command fails if a required input is missing or unreadable. It does not fail only because `preflight.md` or `test-contract-review.md` is missing; missing readiness reports are recorded as warnings in the workset.

Existing unmanaged `generated/agent-workset.md` files are not claimed or overwritten, even with `--force`. The command never overwrites task input files, root `AGENTS.md`, root `CLAUDE.md`, or `.harness/generated/agent-instructions.md`.

The command creates the task-scoped agent workset for pre-implementation context. It does not call AI models, generate code, generate tests, run tests, install adapters, enforce packs, determine compliance, execute packs, or perform semantic validation.

## `ai-sdlc evidence --task <task-slug>`

Creates a deterministic task-scoped evidence report at `.harness/tasks/<task-slug>/evidence-report.md`.

Options:

- `--task <task-slug>`: required existing task slug created by `task start`.
- `--dry-run`: print the report path and summary without writing files.
- `--force`: rewrite only manifest-managed `evidence-report.md` for the selected task.

The command requires `ai-sdlc init` first and an existing task folder. It summarizes recorded task artifacts, readiness reports when present, manifest-managed task artifact signals, `evidence.md`, and `verification.md`. Missing, unreadable, empty, or TODO-only evidence notes are reported as review readiness signals in the generated report.

Existing unmanaged `evidence-report.md` files are not claimed or overwritten, even with `--force`. The command never overwrites task input files, root `AGENTS.md`, root `CLAUDE.md`, `.harness/generated/agent-instructions.md`, or `generated/agent-workset.md`.

The command summarizes recorded evidence only. It does not run tests, prove correctness, validate compliance, scan for security issues, perform AI review, execute packs, enforce packs, call AI models, generate code, generate tests, or change application code.

## `ai-sdlc validate --task <task-slug>`

Creates a deterministic task workflow validation report at `.harness/tasks/<task-slug>/validation-report.md`.

Options:

- `--task <task-slug>`: required existing task slug created by `task start`.
- `--dry-run`: print the report path and summary without writing files.
- `--force`: rewrite only manifest-managed `validation-report.md` for the selected task.

The command requires `ai-sdlc init` first and an existing task folder. It summarizes task artifacts, generated reports, manifest-managed task artifact signals, open blocker and warning lines from generated reports, workflow consistency checks, and findings for human review. Missing, unreadable, empty, or TODO-only task artifacts are recorded as blocker findings in the generated report instead of stopping report generation.

Existing unmanaged `validation-report.md` files are not claimed or overwritten, even with `--force`. A hash-drifted manifest-managed `validation-report.md` is refused unless `--force` is used. The command may report missing or hash-drifted manifest-managed task artifacts, but it only creates or refreshes `validation-report.md`.

The command never overwrites task input files, root `AGENTS.md`, root `CLAUDE.md`, `.harness/generated/agent-instructions.md`, `generated/agent-workset.md`, `preflight.md`, `test-contract-review.md`, or `evidence-report.md`.

The command reports review readiness signals for human review. It does not approve work, run tests, prove correctness, validate compliance, scan for security issues, perform AI review, execute packs, enforce packs, call AI models, generate code, generate tests, or change application code.

## `ai-sdlc task start "<task title>"`

Creates task-level template artifacts under `.harness/tasks/<task-slug>/`.

Options:

- `--dry-run`: show planned creates/skips without writing files.
- `--force`: rewrite only task files already recorded as manifest-managed for that task.

The command requires `ai-sdlc init` first. It generates a safe slug from the human-readable title and never uses the raw title as a path. Normal titles may include `/` or `\`; clearly unsafe path-like values such as traversal, absolute paths, Windows drive paths, NUL-containing input, or titles that produce an empty unsafe slug are rejected.

Created files:

- `task.md`
- `acceptance.md`
- `architecture-notes.md`
- `coupling-notes.md`
- `test-contract.md`
- `verification.md`
- `evidence.md`

Existing unmanaged task files are not overwritten, even with `--force`.
