"""Structured requirements v1 helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .constants import REQUIREMENTS_FILENAME, TASK_SLUG_PATTERN
from .files import read_text, resolve_under_root


SCHEMA_VERSION = 1
ARTIFACT_ROLE = "advisory_projection"
AUTHORITY = "non_authoritative"
EDIT_MODEL = "edit_source_task_artifacts_and_rerun_spec"
SOURCE_MODEL = "deterministic_acceptance_markdown_projection"
SOURCE_PATH = "acceptance.md"
SOURCE_SECTION = "Requirements And Acceptance Criteria"
ALLOWED_FINDING_LEVELS = {"blocker", "warning", "info"}
ALLOWED_STATUSES = {"draft"}
TODO_RE = re.compile(r"\b(?:todo|tbd|fixme)\b", re.IGNORECASE)
LIST_ITEM_RE = re.compile(r"^(?P<indent>\s*)(?:[-*]|\d+[.])\s+(?P<text>.+?)\s*$")
REQ_ID_RE = re.compile(r"^REQ-\d{3}$")
TASK_SLUG_RE = re.compile(TASK_SLUG_PATTERN)


@dataclass(frozen=True)
class RequirementsReadResult:
    present: bool
    readable: bool
    schema_valid: bool
    message: str
    requirement_count: int
    finding_count: int
    requirements: list[dict[str, Any]]
    findings: list[dict[str, str]]
    problems: list[str]


def requirements_path(slug: str) -> PurePosixPath:
    return PurePosixPath(".harness") / "tasks" / slug / REQUIREMENTS_FILENAME


def _is_placeholder_item(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith(("[ ]", "[x]", "[X]")):
        return True
    return TODO_RE.search(stripped) is not None


def _requirement_items(section_text: str) -> list[str]:
    items: list[str] = []
    for line in section_text.splitlines():
        match = LIST_ITEM_RE.match(line)
        if not match:
            continue
        if match.group("indent"):
            continue
        statement = match.group("text").strip()
        if _is_placeholder_item(statement):
            continue
        items.append(statement)
    return items


def build_requirements_document(slug: str, section_text: str) -> dict[str, Any]:
    requirements = []
    for index, statement in enumerate(_requirement_items(section_text), start=1):
        requirements.append(
            {
                "id": f"REQ-{index:03d}",
                "statement": statement,
                "status": "draft",
                "source": {
                    "path": SOURCE_PATH,
                    "section": SOURCE_SECTION,
                },
                "acceptance_criteria": [],
                "verification": [],
            }
        )

    findings: list[dict[str, str]] = []
    if not requirements:
        findings.append(
            {
                "level": "warning",
                "message": "acceptance.md contains no explicit requirement list items.",
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_role": ARTIFACT_ROLE,
        "authority": AUTHORITY,
        "edit_model": EDIT_MODEL,
        "task_slug": slug,
        "source_model": SOURCE_MODEL,
        "source_artifacts": [
            {
                "path": SOURCE_PATH,
                "section": SOURCE_SECTION,
            }
        ],
        "requirements": requirements,
        "findings": findings,
    }


def render_requirements_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=False, default_flow_style=False)


def _validate_source_artifacts(value: Any, problems: list[str]) -> None:
    if not isinstance(value, list) or len(value) != 1:
        problems.append("source_artifacts must contain the acceptance.md source section.")
        return
    item = value[0]
    if not isinstance(item, dict):
        problems.append("source_artifacts item must be an object.")
        return
    if item.get("path") != SOURCE_PATH or item.get("section") != SOURCE_SECTION:
        problems.append("source_artifacts must reference acceptance.md Requirements And Acceptance Criteria.")


def _validate_requirements(value: Any, problems: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        problems.append("requirements must be a list.")
        return []

    requirements: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        label = f"requirements[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{label} must be an object.")
            continue
        req_id = item.get("id")
        if not isinstance(req_id, str) or REQ_ID_RE.fullmatch(req_id) is None:
            problems.append(f"{label}.id must use REQ-001 format.")
        statement = item.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            problems.append(f"{label}.statement must be a non-empty string.")
        status = item.get("status")
        if status not in ALLOWED_STATUSES:
            problems.append(f"invalid requirement status: {req_id or label} uses {status}.")
        source = item.get("source")
        if not isinstance(source, dict):
            problems.append(f"{label}.source must be an object.")
        elif source.get("path") != SOURCE_PATH or source.get("section") != SOURCE_SECTION:
            problems.append(f"{label}.source must reference acceptance.md Requirements And Acceptance Criteria.")
        if not isinstance(item.get("acceptance_criteria"), list):
            problems.append(f"{label}.acceptance_criteria must be a list.")
        if not isinstance(item.get("verification"), list):
            problems.append(f"{label}.verification must be a list.")
        requirements.append(item)
    return requirements


def _validate_findings(value: Any, problems: list[str]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        problems.append("findings must be a list.")
        return []

    findings: list[dict[str, str]] = []
    for index, item in enumerate(value, start=1):
        label = f"findings[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{label} must be an object.")
            continue
        level = item.get("level")
        message = item.get("message")
        if level not in ALLOWED_FINDING_LEVELS:
            problems.append(f"{label}.level must be blocker, warning, or info.")
        if not isinstance(message, str) or not message.strip():
            problems.append(f"{label}.message must be a non-empty string.")
        if isinstance(level, str) and isinstance(message, str):
            findings.append({"level": level, "message": message})
    return findings


def validate_requirements_data(data: Any, expected_slug: str | None = None) -> RequirementsReadResult:
    problems: list[str] = []
    if not isinstance(data, dict):
        return RequirementsReadResult(True, True, False, "invalid schema", 0, 0, [], [], ["requirements.yaml must be a mapping."])

    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version must be 1.")
    if data.get("artifact_role") != ARTIFACT_ROLE:
        problems.append(f"artifact_role must be {ARTIFACT_ROLE}.")
    if data.get("authority") != AUTHORITY:
        problems.append(f"authority must be {AUTHORITY}.")
    if data.get("edit_model") != EDIT_MODEL:
        problems.append(f"edit_model must be {EDIT_MODEL}.")
    task_slug = data.get("task_slug")
    if not isinstance(task_slug, str) or TASK_SLUG_RE.fullmatch(task_slug) is None:
        problems.append("task_slug must be a safe task slug.")
    elif expected_slug is not None and task_slug != expected_slug:
        problems.append(f"task_slug must match selected task: {expected_slug}.")
    if data.get("source_model") != SOURCE_MODEL:
        problems.append(f"source_model must be {SOURCE_MODEL}.")

    _validate_source_artifacts(data.get("source_artifacts"), problems)
    requirements = _validate_requirements(data.get("requirements"), problems)
    findings = _validate_findings(data.get("findings"), problems)
    return RequirementsReadResult(
        present=True,
        readable=True,
        schema_valid=not problems,
        message="schema valid" if not problems else "invalid schema",
        requirement_count=len(requirements),
        finding_count=len(findings),
        requirements=requirements,
        findings=findings,
        problems=problems,
    )


def parse_requirements_text(text: str, expected_slug: str | None = None) -> RequirementsReadResult:
    """Parse and validate one already-captured requirements document."""

    if not isinstance(text, str):
        raise TypeError("requirements text must be a string")
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        return RequirementsReadResult(
            True,
            True,
            False,
            f"malformed YAML: {exc}",
            0,
            0,
            [],
            [],
            [f"requirements.yaml is malformed YAML: {exc}"],
        )
    return validate_requirements_data(data, expected_slug=expected_slug)


def read_requirements(root: Path, slug: str) -> RequirementsReadResult:
    target = resolve_under_root(root, requirements_path(slug))
    if not target.exists():
        return RequirementsReadResult(False, False, False, "missing", 0, 0, [], [], [])
    if not target.is_file():
        return RequirementsReadResult(True, False, False, "not a regular file", 0, 0, [], [], ["requirements.yaml is not a regular file."])
    try:
        text = read_text(target)
    except Exception as exc:
        return RequirementsReadResult(True, False, False, f"unreadable: {exc}", 0, 0, [], [], [f"requirements.yaml is unreadable: {exc}"])
    return parse_requirements_text(text, expected_slug=slug)
