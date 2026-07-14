from __future__ import annotations

import pytest

from ai_sdlc_harness.files import PathSafetyError, normalize_managed_path, resolve_under_root
from ai_sdlc_harness.redact import redact_text, redact_value


def test_posix_and_windows_style_relative_paths_are_normalized(project_tmp):
    assert normalize_managed_path(".harness/generated/agent-instructions.md").as_posix() == (
        ".harness/generated/agent-instructions.md"
    )
    assert normalize_managed_path(".harness\\packs\\selected.yaml").as_posix() == ".harness/packs/selected.yaml"
    assert resolve_under_root(project_tmp, ".harness\\state.json").parent == project_tmp.resolve() / ".harness"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        ".harness/../escape.txt",
        "/tmp/escape.txt",
        "C:/Temp/escape.txt",
        "C:\\Temp\\escape.txt",
    ],
)
def test_unsafe_managed_paths_are_rejected(project_tmp, path):
    with pytest.raises(PathSafetyError):
        resolve_under_root(project_tmp, path)


def test_secret_redaction_helper_redacts_keys_and_values():
    fake_openai_key = "sk-" + "abcdefghijklmnop"
    fake_authorization = "Authorization: " + "Bearer " + "abcdefghijklmnop"
    data = {
        "api_key": "abc123",
        "password": "do-not-display",
        "access_token": "token-" + "abcdefghijklmnop",
        "nested": {
            "message": fake_authorization,
            "safe": "visible",
        },
    }

    redacted = redact_value(data)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["nested"]["message"] == "Authorization: [REDACTED]"
    assert redacted["nested"]["safe"] == "visible"
    assert redact_text(fake_openai_key) == "[REDACTED]"
