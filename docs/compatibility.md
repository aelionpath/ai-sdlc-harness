# Compatibility

## Runtime / platform compatibility

AI SDLC Harness requires Python 3.11 or newer.

Core logic is designed to be cross-platform through Python standard-library file handling. Local maintainer validation has been performed on Windows. macOS and Linux are designed for OS-neutral compatibility but have not yet been manually validated by the maintainer.

The CLI avoids shell-specific assumptions, platform-specific dependencies, and automatic execution of project commands.

The v1 package is distributed through PyPI for isolated tool installation with `uv` or `pipx`; installation with `pip` is supported inside an explicit virtual environment.

## Coding-agent compatibility

Harness is agent-neutral: it does not invoke, launch, or orchestrate a coding agent. Its first-party adapters install repository-scoped Agent Skills that guide an already-running agent. The coding agent executes the engineering work; Harness supplies deterministic task state, bounded context, next actions, currentness and integrity checks, evidence expectations, and authority boundaries.

```text
Coding agent = executor
Harness = control layer
Developer = authority
```

In this document, compatibility claims use three distinct terms:

- **SUPPORTED** — Harness ships a first-party integration for the agent, covered by automated adapter regression and generated-skill validation. Support does not by itself claim that a real-agent E2E run has been performed.
- **VALIDATED** — additional real-agent testing has been performed and is maintained as project validation. This may include the project's reference or manual E2E validation.
- **COMMUNITY VALIDATED** — real-agent testing has been performed by an external user or contributor, with enough reproducible evidence for the project to document the compatibility claim. Community evidence should identify the tested agent and version where available, Harness version, relevant environment, validation scope, and known limitations, and should link to the corresponding GitHub issue, pull request, discussion, or other project record when one exists.

Community validation is a legitimate compatibility signal, but it is distinct from project-maintained **VALIDATED** status. All validation statuses describe observed behavior for the tested versions and environment; they do not guarantee future compatibility because external agent tooling and discovery conventions can change independently of Harness.

### Current v1 matrix

| Coding agent | Support | Repository Agent Skill | Automated coverage | Additional real-agent validation |
| --- | --- | --- | --- | --- |
| Codex | **SUPPORTED** | `.agents/skills/ai-sdlc-harness/SKILL.md` | Adapter regression and generated-skill validation | **VALIDATED** — manual full E2E is the v1 reference validation path |
| Claude Code | **SUPPORTED** | `.claude/skills/ai-sdlc-harness/SKILL.md` | Adapter regression and generated-skill validation | No additional real-agent validation is claimed yet |
| Gemini CLI | **SUPPORTED** | `.gemini/skills/ai-sdlc-harness/SKILL.md` | Adapter regression and generated-skill validation | No additional real-agent validation is claimed yet |

A validation result for one agent or execution surface does not imply validation of another. In particular, the current matrix does not claim real discovery or activation validation for Claude Code or Gemini CLI.

Future matrix entries may include **COMMUNITY VALIDATED** after the project accepts sufficient external real-agent evidence. No current v1 integration has that status yet.

Adapter installation does not modify root `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md`. Routine bounded work can continue through Harness-directed next actions without stopping after every Harness command; the developer retains authority over intent, consequential decisions, and acceptance. Human-owned artifacts express that authority but may be drafted with agent assistance. A `COMPLETE` status means the workflow record is complete, not that the change is correct, secure, compliant, approved, or ready for release.
