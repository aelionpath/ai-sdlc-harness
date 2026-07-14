"""Small helpers for task metadata extracted from task artifacts."""

from __future__ import annotations

import re


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
TASK_FIELD_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:task\s+title|task\s+request|request)\s*:\s*(.+?)\s*$", re.IGNORECASE)
GENERIC_TASK_HEADINGS = {
    "default workflow context",
    "implementation boundary",
    "requirements and acceptance criteria",
}


def title_from_slug(slug: str) -> str:
    words = " ".join(part for part in slug.strip("-").split("-") if part)
    if not words:
        return slug
    return words[:1].upper() + words[1:]


def extract_task_title(task_text: str, slug: str) -> str:
    for line in task_text.splitlines():
        match = HEADING_RE.match(line.strip())
        if not match:
            continue
        heading = match.group(2).strip()
        if heading.casefold().startswith("task:"):
            title = heading.split(":", 1)[1].strip()
            if title:
                return title
        break

    for line in task_text.splitlines():
        match = TASK_FIELD_RE.match(line)
        if match:
            title = match.group(1).strip().strip("`")
            if title and title.casefold() not in GENERIC_TASK_HEADINGS:
                return title

    return title_from_slug(slug)
