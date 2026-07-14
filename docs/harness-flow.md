# Harness Flow

AI SDLC Harness is a repo-native control scaffold for AI-assisted software delivery. It records task boundaries, expectations, workset context, evidence, and review readiness signals as files in the repository.

This guide describes the implemented workflow:

```text
init -> task start -> preflight -> spec -> test-contract -> generate -> implement -> evidence -> validate -> status -> verify
```

## Task Model

A task is the harness unit of work: a bounded AI-assisted software change that a human wants a coding agent, or a human using AI, to implement and verify.

Start with a rough task title. The harness creates normalized task artifacts under `.harness/tasks/<task-slug>/`. These artifacts are the editable source for task intent:

- `task.md`
- `acceptance.md`
- `architecture-notes.md`
- `coupling-notes.md`
- `test-contract.md`
- `verification.md`
- `evidence.md`

The harness can generate reports and worksets from those files, but the human-owned task artifacts remain the source for task intent and evidence notes.

## Source Of Truth

The source-of-truth model is intentionally simple:

- Human-owned task artifacts describe the task, expectations, and recorded evidence.
- Generated artifacts summarize, project, or package those inputs.
- Manifest-managed files are tracked for integrity.
- If generated output is stale or wrong, update the source task artifacts and rerun the relevant command.

`requirements.yaml` is an advisory structured requirements projection. It is generated from source task artifacts and is not the authoritative requirements file. Do not edit it as the source of truth; edit task artifacts and rerun `ai-sdlc spec`.

## Command Flow

### 1. Initialize

```bash
ai-sdlc init
```

Reads:

- current repository path

Writes:

- `.harness/config.yaml`
- `.harness/state.json`
- `.harness/manifest.json`
- `.harness/generated/agent-instructions.md`
- `.harness/packs/selected.yaml`
- `.harness/tasks/`

`init` is non-destructive by default. It creates the repo-local harness directory and records that no optional packs are selected by default. It does not install adapters and does not modify root `AGENTS.md` or `CLAUDE.md`.

### 2. Start A Task

```bash
ai-sdlc task start "Describe the task"
```

Reads:

- `.harness/` state and manifest
- the task title passed on the command line

Writes:

- task artifacts under `.harness/tasks/<task-slug>/`
- manifest entries for created task artifacts

The task title is converted into a safe slug. Existing unmanaged task files are not overwritten, including with `--force`.

### 3. Run Preflight

```bash
ai-sdlc preflight --task <task-slug>
```

Reads:

- task artifacts
- shallow repository signals
- selected optional pack records

Writes:

- `.harness/tasks/<task-slug>/preflight.md`
- manifest entry and hash for the report

Preflight is a task-readiness report before implementation. It checks basic repo markers and task artifact readiness. It does not ask interactive questions, deeply inspect target code, scan security, enforce packs, or perform AI review.

### 4. Generate Spec

```bash
ai-sdlc spec --task <task-slug>
```

Reads:

- task artifacts
- readable `preflight.md`, when present

Writes:

- `.harness/tasks/<task-slug>/spec.md`
- `.harness/tasks/<task-slug>/requirements.yaml`
- manifest entries and hashes for both files

`spec.md` is a deterministic implementation-intent summary. `requirements.yaml` is an advisory structured requirements projection generated from explicit list items in `acceptance.md`.

The command records blocker, warning, and info signals when task sections are missing, empty, unreadable, or TODO-only. It does not import requirements, call AI models, run tests, inspect target code deeply, scan security, validate compliance, prove correctness, determine approval, or determine release readiness.

### 5. Review Test Contract

```bash
ai-sdlc test-contract --task <task-slug>
```

Reads:

- `acceptance.md`
- `test-contract.md`
- `verification.md`
- basic repository test signals

Writes:

- `.harness/tasks/<task-slug>/test-contract-review.md`
- manifest entry and hash for the report

This report checks whether existing task artifacts contain usable acceptance, test, and verification intent. It does not generate tests or run tests.

### 6. Generate The Workset

```bash
ai-sdlc generate --task <task-slug>
```

Reads:

- required task artifacts
- optional `preflight.md`
- optional `spec.md`
- optional `requirements.yaml`
- optional `test-contract-review.md`
- optional selected pack records
- repository signals
- coding-agent instructions

Writes:

- `.harness/tasks/<task-slug>/generated/agent-workset.md`
- manifest entry and hash for the workset

`agent-workset.md` is the task-scoped pre-implementation context package. Give it to a human implementer or coding agent before code changes begin. It compiles bounded excerpts, warnings, and completion evidence expectations. It does not generate code, generate tests, call AI models, run tests, install adapters, or enforce packs.

### 7. Implement

Implementation happens outside the harness command flow. A human or coding agent changes the repository, uses the workset as context, and updates task artifacts as needed.

Record what happened in:

- `evidence.md`
- `verification.md`

Useful evidence includes what changed, why it changed, files touched, requirements addressed, checks run, results, commands not run and why, known gaps, and references.

### 8. Summarize Evidence

```bash
ai-sdlc evidence --task <task-slug>
```

Reads:

- task artifacts
- readiness reports, when present
- `evidence.md`
- `verification.md`
- manifest-managed task artifact signals

Writes:

- `.harness/tasks/<task-slug>/evidence-report.md`
- manifest entry and hash for the report

The evidence report summarizes recorded evidence as review readiness signals. It does not run tests, prove correctness, validate compliance, scan security, perform AI review, execute packs, enforce packs, generate code, or generate tests.

### 9. Validate Workflow Signals

```bash
ai-sdlc validate --task <task-slug>
```

Reads:

- task artifacts
- generated reports
- manifest-managed task artifact signals
- blocker, warning, and info lines from generated reports

Writes:

- `.harness/tasks/<task-slug>/validation-report.md`
- manifest entry and hash for the report

`validate` reports workflow consistency and review readiness signals for human review. It can report blockers or warnings, but it does not approve the work, run tests, prove correctness, validate compliance, scan security, call AI models, or enforce packs.

### 10. Inspect Status

```bash
ai-sdlc status
```

Reads:

- `.harness/` structure
- manifest records
- selected optional pack records
- task/report/workset counts

Writes:

- nothing

`status` is read-only.

### 11. Verify Integrity

```bash
ai-sdlc verify
```

Reads:

- `.harness/` structure
- parseable harness files
- selected optional pack records
- manifest-managed file hashes

Writes:

- nothing

`verify` checks harness structure and manifest integrity. It does not evaluate whether implementation is correct, secure, compliant, complete, or ready to release.

## Manifest-Managed Artifacts

The harness records hashes for protected managed files and generated task artifacts. If a manifest-managed file changes unexpectedly, commands may report hash drift. A command with `--force` can refresh only the manifest-managed output that command owns.

Unmanaged existing files are not adopted or overwritten automatically. This protects user-authored task files and reports from accidental replacement.

## Limitations

The current harness does not:

- call AI models
- generate application code
- generate tests
- run tests
- deeply inspect application code
- scan for security issues
- validate compliance
- prove correctness
- approve work
- determine release readiness
- enforce packs
- install adapters
- modify root `AGENTS.md` or `CLAUDE.md`

Use the harness as a structured record and review aid. Human review, appropriate tests, security review, and release judgment remain outside the harness.
