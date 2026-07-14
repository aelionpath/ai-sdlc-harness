"""Implementation of ``ai-sdlc status``."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .constants import (
    AGENT_WORKSET_FILENAME,
    EVIDENCE_REPORT_FILENAME,
    HARNESS_DIR,
    PREFLIGHT_FILENAME,
    REQUIREMENTS_FILENAME,
    SPEC_FILENAME,
    TASK_GENERATED_DIRNAME,
    TEST_CONTRACT_REVIEW_FILENAME,
    VALIDATION_REPORT_FILENAME,
)
from .detect import detect_project_signals
from .files import relative_display_path
from .manifest import load_manifest
from .redact import redact_value


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return yaml.safe_load(file_obj)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def status_project(root: Path) -> tuple[int, list[str]]:
    harness_root = root / HARNESS_DIR
    config_path = harness_root / "config.yaml"
    state_path = harness_root / "state.json"
    manifest_path = harness_root / "manifest.json"
    selected_path = harness_root / "packs" / "selected.yaml"
    generated_path = harness_root / "generated" / "agent-instructions.md"
    tasks_path = harness_root / "tasks"

    messages = ["AI SDLC Harness status"]
    messages.append(f"initialized: {'yes' if harness_root.is_dir() else 'no'}")
    messages.append(f"config: {'present' if config_path.is_file() else 'missing'}")
    messages.append(f"state: {'present' if state_path.is_file() else 'missing'}")
    messages.append(f"manifest: {'present' if manifest_path.is_file() else 'missing'}")
    messages.append(f"generated instructions: {'present' if generated_path.is_file() else 'missing'}")
    if harness_root.is_dir():
        task_count = sum(1 for path in tasks_path.iterdir() if path.is_dir()) if tasks_path.is_dir() else 0
        messages.append(f"task folders: {task_count}")

    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = _load_yaml(config_path)
            if isinstance(loaded, dict):
                config = redact_value(loaded)
            if config.get("adoption_scope"):
                messages.append(f"adoption scope: {config['adoption_scope']}")
            messages.append(f"selected packs: {', '.join(config.get('selected_packs', [])) or 'none'}")
            adapters = config.get("adapters", {})
            if isinstance(adapters, dict):
                adapter_text = ", ".join(f"{key}={value}" for key, value in adapters.items())
                messages.append(f"adapters: {adapter_text or 'none'}")
            project = config.get("project", {})
            if isinstance(project, dict):
                languages = project.get("detected_languages", [])
                tests = project.get("detected_test_frameworks", [])
                ci = project.get("detected_ci", [])
                messages.append(f"detected languages: {', '.join(languages) or 'none'}")
                messages.append(f"detected tests: {', '.join(tests) or 'none'}")
                messages.append(f"detected ci: {', '.join(ci) or 'none'}")
        except Exception as exc:  # pragma: no cover - exercised by verify in detail
            messages.append(f"config read error: {exc}")

    if selected_path.is_file():
        messages.append(f"selected packs file: {relative_display_path(selected_path, root)}")

    if manifest_path.is_file():
        try:
            manifest = load_manifest(manifest_path)
            entries = manifest.get("managed_files", [])
            preflight_count = 0
            spec_count = 0
            requirements_count = 0
            test_contract_review_count = 0
            generated_workset_count = 0
            evidence_report_count = 0
            validation_report_count = 0
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    path = PurePosixPath(str(entry.get("path", "")).replace("\\", "/"))
                    is_preflight = (
                        len(path.parts) == 4
                        and path.parts[0] == ".harness"
                        and path.parts[1] == "tasks"
                        and path.name == PREFLIGHT_FILENAME
                    )
                    if is_preflight:
                        preflight_count += 1
                    is_spec = (
                        len(path.parts) == 4
                        and path.parts[0] == ".harness"
                        and path.parts[1] == "tasks"
                        and path.name == SPEC_FILENAME
                    )
                    if is_spec:
                        spec_count += 1
                    is_requirements = (
                        len(path.parts) == 4
                        and path.parts[0] == ".harness"
                        and path.parts[1] == "tasks"
                        and path.name == REQUIREMENTS_FILENAME
                    )
                    if is_requirements:
                        requirements_count += 1
                    is_test_contract_review = (
                        len(path.parts) == 4
                        and path.parts[0] == ".harness"
                        and path.parts[1] == "tasks"
                        and path.name == TEST_CONTRACT_REVIEW_FILENAME
                    )
                    if is_test_contract_review:
                        test_contract_review_count += 1
                    is_generated_workset = (
                        len(path.parts) == 5
                        and path.parts[0] == ".harness"
                        and path.parts[1] == "tasks"
                        and path.parts[3] == TASK_GENERATED_DIRNAME
                        and path.name == AGENT_WORKSET_FILENAME
                    )
                    if is_generated_workset:
                        generated_workset_count += 1
                    is_evidence_report = (
                        len(path.parts) == 4
                        and path.parts[0] == ".harness"
                        and path.parts[1] == "tasks"
                        and path.name == EVIDENCE_REPORT_FILENAME
                    )
                    if is_evidence_report:
                        evidence_report_count += 1
                    is_validation_report = (
                        len(path.parts) == 4
                        and path.parts[0] == ".harness"
                        and path.parts[1] == "tasks"
                        and path.name == VALIDATION_REPORT_FILENAME
                    )
                    if is_validation_report:
                        validation_report_count += 1
            messages.append(f"manifest-managed preflight reports: {preflight_count}")
            messages.append(f"manifest-managed spec reports: {spec_count}")
            messages.append(f"manifest-managed requirements files: {requirements_count}")
            messages.append(f"manifest-managed test-contract review reports: {test_contract_review_count}")
            messages.append(f"manifest-managed generated worksets: {generated_workset_count}")
            messages.append(f"manifest-managed evidence reports: {evidence_report_count}")
            messages.append(f"manifest-managed validation reports: {validation_report_count}")
        except Exception as exc:  # pragma: no cover - verify reports manifest detail
            messages.append(f"manifest read error: {exc}")

    current_signals = detect_project_signals(root)
    messages.append(f"AGENTS.md: {'present' if current_signals['files']['AGENTS.md'] else 'missing'}")
    messages.append(f"CLAUDE.md: {'present' if current_signals['files']['CLAUDE.md'] else 'missing'}")
    return 0, messages
