# Security Policy

AI SDLC Harness is a local, artifact-driven workflow for recording task boundaries, generated worksets, evidence notes, review readiness signals, and manifest-managed file integrity.

Please report suspected vulnerabilities through [GitHub Private Vulnerability Reporting](https://github.com/aelionpath/ai-sdlc-harness/security/advisories/new). Do not include secrets, customer data, vulnerability details, or exploit details in public issues.

Current security posture:

- Workflow artifacts are managed under `.harness/`; explicit adapter installation writes only the supported repository-scoped `SKILL.md` paths.
- Absolute managed-output paths and traversal paths are rejected.
- Existing root `AGENTS.md`, `CLAUDE.md`, and `GEMINI.md` files are not modified.
- Existing unmanaged adapter files are not adopted or overwritten.
- CLI output redacts obvious secret-like values.
- The CLI does not run project commands, scanners, tests, or CI automatically.

The harness does not call AI models, run tests, scan for security issues, validate compliance, prove correctness, or approve release readiness.

Release integrity checks are handled through the private release process before public publication.
