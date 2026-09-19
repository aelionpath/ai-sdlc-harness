# Minimal Python Project Example

This directory is a minimal example target for trying the AI SDLC Harness command flow.

It is intentionally small and does not include generated `.harness/` content. The harness creates `.harness/` locally when you run the commands.

This example is useful for validating the artifact workflow, but it does not currently demonstrate a complete before/after code change, executable application behavior, or a full project test suite.

## Try The Harness Flow

From this example directory, run:

```bash
ai-sdlc init
ai-sdlc task start "Add validation for negative numbers"
ai-sdlc status --task add-validation-for-negative-numbers
```

Fill in the human-owned task sources, rerun the same status command, and follow each displayed command or human action through preflight, specification, test-contract review, and generation. Then implement the task, or use this README-only example to record manual evidence in the task source files. Keep planned checks in `test-contract.md`; record only checks actually run and their observed results in `verification.md`.

After implementation or evidence updates, run these commands from this example directory:

```bash
ai-sdlc evidence --task add-validation-for-negative-numbers
ai-sdlc validate --task add-validation-for-negative-numbers
ai-sdlc status --task add-validation-for-negative-numbers
ai-sdlc verify
```

## Current Scope

This example is intentionally minimal. It is meant to show the harness workflow and generated artifacts, not to prove application correctness.

The harness does not call AI models, generate application code, generate tests, run tests, scan security, validate compliance, approve work, or determine release readiness.

A fuller executable example can be added later.
