from __future__ import annotations

import yaml

from ai_sdlc_harness.requirements import (
    parse_requirements_text,
    read_requirements,
)


def _document(slug: str) -> dict:
    return {
        "schema_version": 1,
        "artifact_role": "advisory_projection",
        "authority": "non_authoritative",
        "edit_model": "edit_source_task_artifacts_and_rerun_spec",
        "task_slug": slug,
        "source_model": "deterministic_acceptance_markdown_projection",
        "source_artifacts": [
            {
                "path": "acceptance.md",
                "section": "Requirements And Acceptance Criteria",
            }
        ],
        "requirements": [],
        "findings": [],
    }


def test_parse_requirements_text_matches_file_reader(project_tmp):
    slug = "sample-task"
    target = (
        project_tmp / ".harness" / "tasks" / slug / "requirements.yaml"
    )
    target.parent.mkdir(parents=True)
    text = yaml.safe_dump(_document(slug), sort_keys=False)
    target.write_text(text, encoding="utf-8")

    assert parse_requirements_text(text, expected_slug=slug) == read_requirements(
        project_tmp, slug
    )


def test_parse_requirements_text_reports_malformed_yaml_without_filesystem():
    result = parse_requirements_text("not: [valid\n", expected_slug="sample-task")

    assert result.present is True
    assert result.readable is True
    assert result.schema_valid is False
    assert result.problems
    assert result.problems[0].startswith("requirements.yaml is malformed YAML:")
