# Security And Integrity

AI SDLC Harness implements a small safety baseline for repo-local harness artifacts.

## Safety Baseline

- Uses `pathlib` based path handling.
- Restricts managed writes to `.harness/`.
- Rejects absolute managed-output paths.
- Rejects path traversal.
- Never modifies existing root `AGENTS.md` or `CLAUDE.md`.
- Records adapter requests only; `init` does not install adapters.
- Does not execute arbitrary project commands.
- Redacts obvious secret-like values from CLI output.
- Records manifest hashes for protected managed files and generated task artifacts.

## Protected Repo-Level Files

These files are manifest-managed at the repo level:

```text
.harness/config.yaml
.harness/state.json
.harness/manifest.json
.harness/generated/agent-instructions.md
.harness/packs/selected.yaml
```

`manifest.json` is listed as protected, but the manifest does not hash itself.

## Task Artifacts And Generated Outputs

Task artifacts created by `ai-sdlc task start` are manifest-managed and hashed:

```text
.harness/tasks/<task-slug>/task.md
.harness/tasks/<task-slug>/acceptance.md
.harness/tasks/<task-slug>/architecture-notes.md
.harness/tasks/<task-slug>/coupling-notes.md
.harness/tasks/<task-slug>/test-contract.md
.harness/tasks/<task-slug>/verification.md
.harness/tasks/<task-slug>/evidence.md
```

Generated task reports and worksets are also manifest-managed and hashed when created:

```text
.harness/tasks/<task-slug>/preflight.md
.harness/tasks/<task-slug>/spec.md
.harness/tasks/<task-slug>/requirements.yaml
.harness/tasks/<task-slug>/test-contract-review.md
.harness/tasks/<task-slug>/generated/agent-workset.md
.harness/tasks/<task-slug>/evidence-report.md
.harness/tasks/<task-slug>/validation-report.md
```

## Non-Destructive Write Model

`init` is non-destructive by default. It creates missing managed files, skips existing managed files, and rewrites known managed `.harness/` files only when `--force` is passed.

`task start` creates task artifacts under `.harness/tasks/<safe-slug>/`. Existing task files are not automatically claimed as harness-managed. If an expected task artifact already exists without a manifest entry, the command reports it and refuses to overwrite it, including with `--force`.

Report and workset commands write only their own selected task output:

- `preflight` writes `preflight.md`
- `spec` writes `spec.md` and `requirements.yaml`
- `test-contract` writes `test-contract-review.md`
- `generate` writes `generated/agent-workset.md`
- `evidence` writes `evidence-report.md`
- `validate` writes `validation-report.md`

Existing unmanaged generated outputs are not claimed or overwritten, including with `--force`. A command with `--force` can rewrite only the manifest-managed output that command owns for the selected task.

## Hash Drift Behavior

Manifest-managed files have recorded SHA-256 hashes. If a command detects hash drift in the output it owns, it refuses to rewrite that output unless `--force` is used.

Commands may report missing or hash-drifted manifest-managed task artifacts that they do not own, but they do not repair unrelated files.

`verify` checks recorded hashes for protected managed files and generated task artifacts. It reports missing or modified managed files, but it does not decide whether task content is semantically correct.

## Root Agent Files

The current command surface does not modify root `AGENTS.md` or `CLAUDE.md`.

`ai-sdlc init --agent ...` records the requested adapter choice in harness state, but it does not install adapters and does not write root agent instruction files.

## Integrity Limits

`verify` checks harness structure, parseable harness files, selected optional pack records, and manifest-managed file integrity.

It does not:

- run tests
- inspect target code deeply
- scan for security issues
- validate compliance
- prove correctness
- approve work
- determine release readiness

Release integrity checks are handled through the private release process before public publication.
