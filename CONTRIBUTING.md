# Contributing

Thanks for considering a contribution to AI SDLC Harness.

Keep changes focused on the implemented CLI scaffold, safe file handling, documentation, and tests unless a broader capability or behavior change has been explicitly accepted.

## Branch And PR Expectations

Contributors should branch from `integration` and open pull requests against `integration`. `main` is release-stable; maintainers merge to `main` only after release validation.

Issues may be opened normally. Keep proposed changes concrete and scoped.

## Test Suite Overview

The `/tests` directory tests the harness itself. It does not test user projects, and it does not run user project tests. User project checks remain in the user's repository and can be recorded in harness task artifacts as evidence.

The regression suite is pytest-based and includes:

- unit and small integration tests for helpers and command behavior
- CLI functional tests
- artifact-generation tests for task files, reports, worksets, and specs
- workflow tests across init, task, preflight, spec, generate, evidence, validate, status, and verify
- public-content checks for unsupported wording and public-surface expectations
- release-boundary and hygiene tests for export safety and private/public separation

## Running Checks

Run a focused test while developing:

```bash
py -m pytest tests/test_spec.py -p no:cacheprovider
```

Run the full harness regression suite:

```bash
py -m pytest -p no:cacheprovider
```

Run the maintainer public inventory check from the private development checkout before release or release-candidate review. This confirms the selected public files do not include private-only material.

Export the public surface into a separate review checkout before release or release-candidate review. Use the private maintainer export helper from the development checkout; do not manually edit the generated public checkout as the source of truth.

Run tests from the exported public repo before release or release-candidate review:

```bash
cd ..\ai-sdlc-harness-public-test
py -m pytest -p no:cacheprovider
```

Check whitespace and patch hygiene from the source repo:

```bash
git diff --check
```

## When To Add Or Update Tests

Add or update tests when changing:

- CLI behavior, arguments, exit codes, or messages
- generated artifact wording or structure
- manifest, verify, or status behavior
- safe-write, ownership, hash drift, or unmanaged-file behavior
- public export allowlist or denylist behavior
- docs or public wording covered by public-content tests

Docs-only changes may not need new tests, but public-facing wording must still avoid unsupported capability claims.

## Public Boundary

Do not add private planning files, prompt transcripts, scratch notes, release tooling, local artifacts, local machine paths, secrets, or implementation discussions to public package data, docs, examples, templates, or generated output.

Public docs should describe implemented behavior only. Do not claim that the harness calls AI models, generates code, generates tests, runs tests, scans security, validates compliance, proves correctness, approves work, enforces packs, installs adapters, or modifies root `AGENTS.md` / `CLAUDE.md`.
