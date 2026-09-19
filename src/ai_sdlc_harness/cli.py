"""Argparse CLI for AI SDLC Harness."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .adapters import ADAPTER_IDS, install_adapters
from .constants import AGENT_CHOICES, HARNESS_VERSION, PRODUCT_NAME
from .evidence import run_evidence
from .generate import run_generate
from .init import init_project
from .preflight import run_preflight
from .spec import run_spec
from .status import status_project
from .task import start_task
from .test_contract import run_test_contract_review
from .validate import run_validate
from .verify import verify_project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-sdlc",
        description=f"{PRODUCT_NAME}: deterministic repo-local control for bounded, reviewable AI-assisted software changes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {HARNESS_VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize .harness/ in the current project.")
    init_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    init_parser.add_argument("--force", action="store_true", help="Rewrite known managed .harness files.")
    init_parser.add_argument(
        "--agent",
        choices=AGENT_CHOICES,
        default="none",
        help="Record a requested agent adapter. Init records the request but does not install adapters.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show read-only workflow status.",
        description="Show read-only workflow status.",
    )
    status_parser.add_argument(
        "--task", help="Explicit existing safe task slug to inspect."
    )
    adapter_parser = subparsers.add_parser(
        "adapter",
        help="Manage explicit repository-scoped agent adapters.",
    )
    adapter_subparsers = adapter_parser.add_subparsers(dest="adapter_command")
    adapter_install_parser = adapter_subparsers.add_parser(
        "install",
        help="Install a native repository Agent Skill.",
    )
    adapter_install_parser.add_argument(
        "adapter_id",
        choices=(*ADAPTER_IDS, "all"),
        help="Adapter ID to install, or all.",
    )
    adapter_install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned adapter changes without writing files.",
    )
    adapter_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Restore or refresh only an existing Harness-managed adapter skill.",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit deterministic machine-readable workflow status.",
    )
    subparsers.add_parser("verify", help="Verify harness structure and protected file hashes.")
    preflight_parser = subparsers.add_parser("preflight", help="Create a task-readiness preflight report.")
    preflight_parser.add_argument("--task", required=True, help="Existing safe task slug.")
    preflight_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    preflight_parser.add_argument("--force", action="store_true", help="Rewrite manifest-managed preflight.md only.")
    spec_parser = subparsers.add_parser("spec", help="Create a deterministic task implementation-intent spec.")
    spec_parser.add_argument("--task", required=True, help="Existing safe task slug.")
    spec_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    spec_parser.add_argument("--force", action="store_true", help="Rewrite manifest-managed spec.md only.")
    test_contract_parser = subparsers.add_parser(
        "test-contract",
        help="Create a test-contract readiness review report.",
    )
    test_contract_parser.add_argument("--task", required=True, help="Existing safe task slug.")
    test_contract_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    test_contract_parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite manifest-managed test-contract-review.md only.",
    )
    generate_parser = subparsers.add_parser("generate", help="Create a deterministic task-scoped agent workset.")
    generate_parser.add_argument("--task", required=True, help="Existing safe task slug.")
    generate_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    generate_parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite manifest-managed generate-owned outputs for the selected task only.",
    )
    evidence_parser = subparsers.add_parser("evidence", help="Create a deterministic task-scoped evidence report.")
    evidence_parser.add_argument("--task", required=True, help="Existing safe task slug.")
    evidence_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    evidence_parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite manifest-managed evidence-report.md only.",
    )
    validate_parser = subparsers.add_parser("validate", help="Create a deterministic task workflow validation report.")
    validate_parser.add_argument("--task", required=True, help="Existing safe task slug.")
    validate_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    validate_parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite manifest-managed validation-report.md only.",
    )
    task_parser = subparsers.add_parser("task", help="Manage task-level harness artifacts.")
    task_subparsers = task_parser.add_subparsers(dest="task_command")
    task_start_parser = task_subparsers.add_parser("start", help="Create task-level harness artifacts.")
    task_start_parser.add_argument("title", help="Human-readable task title.")
    task_start_parser.add_argument("--dry-run", action="store_true", help="Show planned changes without writing files.")
    task_start_parser.add_argument("--force", action="store_true", help="Rewrite manifest-managed task files only.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path.cwd()

    if args.command == "init":
        code, messages = init_project(root, agent=args.agent, dry_run=args.dry_run, force=args.force)
    elif args.command == "status":
        code, messages = status_project(
            root,
            task_slug=args.task,
            json_output=args.json,
        )
    elif args.command == "adapter" and args.adapter_command == "install":
        code, messages = install_adapters(
            root,
            args.adapter_id,
            dry_run=args.dry_run,
            force=args.force,
        )
    elif args.command == "adapter":
        parser.print_help()
        return 0
    elif args.command == "verify":
        code, messages = verify_project(root)
    elif args.command == "preflight":
        code, messages = run_preflight(root, args.task, dry_run=args.dry_run, force=args.force)
    elif args.command == "spec":
        code, messages = run_spec(root, args.task, dry_run=args.dry_run, force=args.force)
    elif args.command == "test-contract":
        code, messages = run_test_contract_review(root, args.task, dry_run=args.dry_run, force=args.force)
    elif args.command == "generate":
        code, messages = run_generate(root, args.task, dry_run=args.dry_run, force=args.force)
    elif args.command == "evidence":
        code, messages = run_evidence(root, args.task, dry_run=args.dry_run, force=args.force)
    elif args.command == "validate":
        code, messages = run_validate(root, args.task, dry_run=args.dry_run, force=args.force)
    elif args.command == "task" and args.task_command == "start":
        code, messages = start_task(root, args.title, dry_run=args.dry_run, force=args.force)
    elif args.command == "task":
        parser.print_help()
        return 0
    else:
        parser.print_help()
        return 0

    for message in messages:
        print(message)
    return code
