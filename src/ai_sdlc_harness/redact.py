"""Redaction helpers for CLI output."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{8,}|"
    r"sk-[a-z0-9_-]{8,}|"
    r"gh[pousr]_[a-z0-9_]{8,}|"
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----)"
)


def redact_text(value: str) -> str:
    """Redact obvious secret-looking substrings from text."""

    return SECRET_VALUE_RE.sub("[REDACTED]", value)


def redact_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret-looking values for display."""

    if key and SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {item_key: redact_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_value(item) for item in value]
    return value
