# Security Policy

AI SDLC Harness is a local, artifact-driven workflow for recording task boundaries, generated worksets, evidence notes, review readiness signals, and manifest-managed file integrity.

Please report suspected vulnerabilities privately to the project maintainer. Do not include secrets, customer data, or exploit details in public issues.

Current security posture:

- Managed writes are restricted to `.harness/`.
- Absolute managed-output paths and traversal paths are rejected.
- Existing `AGENTS.md` and `CLAUDE.md` files are not modified.
- CLI output redacts obvious secret-like values.
- The CLI does not run project commands, scanners, tests, or CI automatically.

The harness does not call AI models, run tests, scan for security issues, validate compliance, prove correctness, or approve release readiness.

Release integrity checks are handled through the private release process before public publication.
