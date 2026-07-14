"""Shared constants for the CLI scaffold."""

from __future__ import annotations

from pathlib import PurePosixPath

PRODUCT_NAME = "AI SDLC Harness"
PACKAGE_NAME = "ai-sdlc-harness"
IMPORT_NAME = "ai_sdlc_harness"
CLI_COMMAND = "ai-sdlc"
HARNESS_DIR = ".harness"
HARNESS_VERSION = "1.0.0"
TEMPLATE_VERSION = "1.0.0"
DEFAULT_ADOPTION_SCOPE = "task-local"

AGENT_CHOICES = ("none", "codex", "claude-code", "both")

DEFAULT_SELECTED_PACKS: tuple[str, ...] = ()

LEGACY_BASELINE_PACKS = (
    "architecture-generic",
    "coupling-baseline",
    "observability-baseline",
    "security-baseline",
)

KNOWN_PACK_IDS = (*DEFAULT_SELECTED_PACKS, *LEGACY_BASELINE_PACKS)

EXPECTED_DIRECTORIES = (
    PurePosixPath(".harness"),
    PurePosixPath(".harness/generated"),
    PurePosixPath(".harness/packs"),
    PurePosixPath(".harness/tasks"),
)

TASK_ARTIFACT_FILENAMES = (
    "task.md",
    "acceptance.md",
    "architecture-notes.md",
    "coupling-notes.md",
    "test-contract.md",
    "verification.md",
    "evidence.md",
)

PREFLIGHT_FILENAME = "preflight.md"
SPEC_FILENAME = "spec.md"
REQUIREMENTS_FILENAME = "requirements.yaml"
TEST_CONTRACT_REVIEW_FILENAME = "test-contract-review.md"
EVIDENCE_REPORT_FILENAME = "evidence-report.md"
VALIDATION_REPORT_FILENAME = "validation-report.md"
TASK_GENERATED_DIRNAME = "generated"
AGENT_WORKSET_FILENAME = "agent-workset.md"

MANIFEST_MANAGED_TASK_FILENAMES = (
    *TASK_ARTIFACT_FILENAMES,
    PREFLIGHT_FILENAME,
    SPEC_FILENAME,
    REQUIREMENTS_FILENAME,
    TEST_CONTRACT_REVIEW_FILENAME,
    EVIDENCE_REPORT_FILENAME,
    VALIDATION_REPORT_FILENAME,
)

TASK_SLUG_MAX_LENGTH = 80
TASK_SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$"

MANAGED_FILE_PATHS = (
    PurePosixPath(".harness/config.yaml"),
    PurePosixPath(".harness/state.json"),
    PurePosixPath(".harness/manifest.json"),
    PurePosixPath(".harness/generated/agent-instructions.md"),
    PurePosixPath(".harness/packs/selected.yaml"),
)

HASHED_MANAGED_FILE_PATHS = tuple(
    path for path in MANAGED_FILE_PATHS if path != PurePosixPath(".harness/manifest.json")
)

PROTECTED_MANAGED_FILE_PATHS = MANAGED_FILE_PATHS

FUTURE_INTEGRITY_NOTE = (
    "Future releases should add signed release metadata and downloadable "
    "checksum verification."
)
