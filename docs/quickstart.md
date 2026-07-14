# Quickstart

This walkthrough uses the current source-checkout / local editable install path. It is intentionally short; see [Harness Flow](harness-flow.md) for the full operational model.

## Install From A Source Checkout

```bash
python -m pip install -e ".[test]"
```

Package-registry publication is not part of the current implemented release surface.

## Run The Current Flow

Initialize the harness in the current repository:

```bash
ai-sdlc init
```

Start a task from a rough title:

```bash
ai-sdlc task start "Describe the task"
```

This creates user-editable task artifacts under `.harness/tasks/describe-the-task/`. Fill or refine those artifacts before relying on the generated workset.

Create a task-readiness report:

```bash
ai-sdlc preflight --task describe-the-task
```

Create the deterministic implementation-intent spec and advisory structured requirements projection:

```bash
ai-sdlc spec --task describe-the-task
```

`spec` writes both:

- `.harness/tasks/describe-the-task/spec.md`
- `.harness/tasks/describe-the-task/requirements.yaml`

`requirements.yaml` is not the authoritative requirements source. It is generated from source task artifacts. To change requirements, edit the source task artifacts, such as `acceptance.md`, and rerun `spec`.

Review test-contract readiness:

```bash
ai-sdlc test-contract --task describe-the-task
```

Generate task-scoped implementation context:

```bash
ai-sdlc generate --task describe-the-task
```

Use `.harness/tasks/describe-the-task/generated/agent-workset.md` as the pre-implementation workset for a human implementer or coding agent.

Then implement the task outside the harness command flow. Update task artifacts as needed and record evidence in `evidence.md` and `verification.md`.

Summarize recorded evidence:

```bash
ai-sdlc evidence --task describe-the-task
```

Generate workflow validation signals:

```bash
ai-sdlc validate --task describe-the-task
```

Check status and manifest integrity:

```bash
ai-sdlc status
ai-sdlc verify
```

`status` is read-only. `verify` checks `.harness/` structure and manifest-managed file hashes; it does not prove correctness, security, or compliance.

## Safety Notes

The current harness does not generate code, generate tests, run tests, call AI models, enforce packs, install adapters, or scan security. Root `AGENTS.md` and `CLAUDE.md` files are not modified.

Running `evidence` or `validate` immediately on untouched TODO templates is allowed, but the reports will intentionally produce blockers and warnings.
