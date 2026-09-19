# Quickstart

This walkthrough creates one task, follows the status-guided workflow to implementation, records evidence, and reaches the final human-review boundary.

## 1. Install The Released Package

AI SDLC Harness requires Python 3.11 or newer. Install it as an isolated command-line tool with `uv`:

```bash
uv tool install ai-sdlc-harness
ai-sdlc --version
```

As a secondary option, use `pipx install ai-sdlc-harness`. If neither tool is available, `python -m pip install ai-sdlc-harness` is an appropriate fallback inside an explicitly activated virtual environment.

Use these commands when you need to manage the `uv` installation:

```bash
uv tool upgrade ai-sdlc-harness
uv tool install ai-sdlc-harness==1.0.0
uv tool uninstall ai-sdlc-harness
```

## 2. Initialize A Repository

Change to the repository where the work will happen, then initialize its repo-local Harness state:

```bash
cd path/to/your-project
ai-sdlc init
```

Initialization creates `.harness/`. It does not modify root `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md`, and it does not install an agent adapter implicitly.

## 3. Start A Task

Start with a short human-readable title:

```bash
ai-sdlc task start "Add request timeout handling"
```

The command prints the safe task slug and creates human-owned source files under:

```text
.harness/tasks/add-request-timeout-handling/
```

Fill in the generated templates. At minimum, make the task scope, acceptance criteria, and planned test and verification intent concrete in `task.md`, `acceptance.md`, and `test-contract.md`. Use the architecture and coupling notes when they are material.

The complete human-owned source set is:

- `task.md`
- `acceptance.md`
- `architecture-notes.md`
- `coupling-notes.md`
- `test-contract.md`
- `verification.md`
- `evidence.md`

These files remain the authoritative workflow inputs. Do not edit generated reports to change task intent.

## 4. Let Status Route The Work

Ask the read-only workflow resolver what should happen next:

```bash
ai-sdlc status --task add-request-timeout-handling
```

Follow the command or human action shown under `next:`, then rerun the same status command. Depending on repository state, the command sequence will route through:

```text
preflight -> spec -> test-contract -> generate -> implementation
           -> evidence -> validate -> review -> complete
```

The producer commands are deterministic and task-scoped:

```bash
ai-sdlc preflight --task add-request-timeout-handling
ai-sdlc spec --task add-request-timeout-handling
ai-sdlc test-contract --task add-request-timeout-handling
ai-sdlc generate --task add-request-timeout-handling
```

Do not assume every command should be run immediately. `status` can stop at a human-review boundary. If a producer refuses to replace a drifted managed output, inspect the mismatch before deciding whether that producer's explicit `--force` option is appropriate.

If task source files change later, Lineage can mark dependent outputs stale and route you back to the correct producer. Update the human-owned source, not the generated output.

## 5. Implement From The Bounded Handoff

When `status` reports the `implementation` phase, give this file to the human or coding agent implementing the change:

```text
.harness/tasks/add-request-timeout-handling/generated/agent-workset.md
```

The workset packages current task intent, relevant repository context, warnings, secure-engineering guardrails, and evidence expectations. It is a bounded starting point, not permission to ignore repository code that must be inspected for the task.

`generate` also manages `generated/context-manifest.yaml`, the machine-readable record of the same context-selection snapshot. Do not edit either generated file directly.

Implementation happens outside the Harness command flow. Change the application, tests, or configuration normally.

## 6. Record Evidence And Verification

After implementation, update the human-owned files:

- `evidence.md` — what changed, why, files affected, requirements addressed, and known gaps.
- `verification.md` — commands actually run, observed results, and checks not run with the reason.

Do not claim a command ran when it did not. Then rerun status and follow its next actions, which normally include:

```bash
ai-sdlc evidence --task add-request-timeout-handling
ai-sdlc validate --task add-request-timeout-handling
```

Validation reports workflow consistency and review-readiness signals. It does not run project tests or inspect the implementation deeply.

## 7. Stop At Human Review

Continue rerunning:

```bash
ai-sdlc status --task add-request-timeout-handling
```

Resolve any reported blocker. When validation warnings remain, status stops at `REVIEW_REQUIRED`: resolve them and rerun validation if you want to reach `COMPLETE`, or carry their documented acceptance into the normal human delivery decision. When the outcome becomes `COMPLETE`, Harness automation stops; the implementation still needs the repository's normal review, approval, and delivery process.

You can check protected Harness state independently at any time:

```bash
ai-sdlc verify
```

`verify` checks structure and manifest-managed hashes. It does not prove correctness, security, compliance, or release readiness.

## Optional: Install A Repository Agent Skill

After initialization, install a thin skill for one supported coding agent:

```bash
ai-sdlc adapter install codex
ai-sdlc adapter install claude-code
ai-sdlc adapter install gemini-cli
```

Or preflight and install all three together:

```bash
ai-sdlc adapter install all --dry-run
ai-sdlc adapter install all
```

The managed skill paths are:

- Codex: `.agents/skills/ai-sdlc-harness/SKILL.md`
- Claude Code: `.claude/skills/ai-sdlc-harness/SKILL.md`
- Gemini CLI: `.gemini/skills/ai-sdlc-harness/SKILL.md`

Adapter installation never adopts an existing unmanaged file. `--force` is only for restoring or refreshing an adapter already managed by the Harness.

## Optional: Machine-Readable Status

Coding-agent adapters and other tooling should use structured status:

```bash
ai-sdlc status --task add-request-timeout-handling --json
```

The JSON document exposes the selected task, phase, outcome, next actions, four Lineage states, generated-handoff state, validation currentness, integrity state, and bounded findings. It is read-only and derived from current repository artifacts.
