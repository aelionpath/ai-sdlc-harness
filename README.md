# AI SDLC Harness

AI SDLC Harness provides repo-native guardrails for AI-assisted software delivery. It turns a rough task into explicit task artifacts, a task-scoped workset, recorded evidence, and review readiness signals that humans and coding agents can use consistently.

The harness is an artifact-driven workflow. It does not replace the coding agent or reviewer; it gives them bounded context, implementation expectations, and a visible evidence trail.

## What It Helps With

AI coding-agent work often starts from vague instructions, stale context, missing test expectations, or undocumented assumptions. AI SDLC Harness makes those inputs explicit before implementation starts:

- task boundaries and non-goals
- acceptance and test expectations
- architecture, coupling, and verification notes
- generated pre-implementation workset context
- recorded implementation evidence
- validation signals for human review
- manifest-managed file integrity checks
- task-scoped context to reduce unnecessary prompt and token load

You do not need a formal requirements document to start. Begin with a rough task title or copied notes, then refine the generated task artifacts before using the workset for implementation.

## Current Command Flow

```bash
ai-sdlc init
ai-sdlc task start "Describe the task"
ai-sdlc preflight --task describe-the-task
ai-sdlc spec --task describe-the-task
ai-sdlc test-contract --task describe-the-task
ai-sdlc generate --task describe-the-task
# human or coding agent implements the task, updates task artifacts, and records evidence
ai-sdlc evidence --task describe-the-task
ai-sdlc validate --task describe-the-task
ai-sdlc status
ai-sdlc verify
```

## Generated Artifacts

The harness stores repo-local state under `.harness/`.

`ai-sdlc task start` creates user-editable task artifacts under `.harness/tasks/<task-slug>/`, including task scope, acceptance criteria, architecture notes, coupling notes, test contract, verification notes, and evidence notes.

`ai-sdlc preflight` creates `preflight.md`, a task-readiness report based on shallow repository signals and task artifact readiness.

`ai-sdlc spec` creates `spec.md` and `requirements.yaml`. The YAML file is an advisory structured requirements projection generated from source task artifacts; it is not the authoritative source of requirements. To change requirements, edit the source task artifacts and rerun `spec`.

`ai-sdlc test-contract` creates `test-contract-review.md`, a readiness review of existing acceptance, test-contract, and verification intent.

`ai-sdlc generate` creates `generated/agent-workset.md`, the task-scoped context package intended for a human implementer or coding agent before implementation.

`ai-sdlc evidence` creates `evidence-report.md`, summarizing recorded implementation evidence and verification notes.

`ai-sdlc validate` creates `validation-report.md`, summarizing workflow consistency and review readiness signals.

`ai-sdlc verify` checks `.harness/` structure and manifest-managed file integrity. It does not evaluate application behavior.

## Context And Token Discipline

AI-assisted development can become expensive and noisy when every agent session starts by re-reading broad repository context, stale notes, or long prior conversations. AI SDLC Harness is designed to keep implementation context task-scoped.

Today, the harness supports context discipline by:

- keeping task intent in explicit task-local artifacts
- generating a focused `agent-workset.md` for the current task
- separating source task artifacts from generated reports
- projecting explicit acceptance items into advisory `requirements.yaml`
- using shallow repository signals instead of asking an agent to inspect the whole repo by default
- recording evidence and validation signals so later review does not depend only on chat history

This does not guarantee lower token usage in every agent session. It gives humans and coding agents a smaller, more structured starting point than ad hoc prompts or whole-repo context dumps.

Further context-management improvements are planned, but the current implementation should be treated as a scoped-context workflow, not an automatic token optimizer.

## Current Limits

AI SDLC Harness does not:

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

It supports human review, security review, testing, and CI/CD by making task context and recorded evidence easier to inspect.

## Learn More

- [Quickstart](docs/quickstart.md)
- [Harness Flow](docs/harness-flow.md)
- [CLI Reference](docs/cli-reference.md)
- [Security and Integrity](docs/security-and-integrity.md)
- [Concepts](docs/concepts.md)
- [Compatibility](docs/compatibility.md)
- [Contributing](CONTRIBUTING.md)

## Current Install Path

The current supported install path is a source checkout with a local editable install:

```bash
python -m pip install -e ".[test]"
```

Package-registry publication is not part of the current implemented release surface.
