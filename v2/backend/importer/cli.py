"""``python -m backend.cli import-v1 --projects-json <path> [--v1-db <path>] [--dry-run]``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from backend.domain.actor import Actor
from backend.domain.enums import GitTransport
from backend.domain.errors import DomainError
from backend.importer.apply import ImportServices, apply_plan
from backend.importer.plan import build_plan
from backend.importer.report import render_outcomes, render_plan
from backend.importer.v1_model import V1Registry, load_projects, load_registry

IMPORT_ACTOR = Actor.system("import-v1")
ServicesFactory = Callable[[], ImportServices]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``import-v1`` options."""
    parser.add_argument("--projects-json", type=Path, required=True, help="v1 projects.json")
    parser.add_argument("--v1-db", type=Path, help="v1 SQLite database (optional, read-only)")
    parser.add_argument(
        "--compose-root",
        type=Path,
        help="directory holding a copy of /opt/docker-engine/projects, to read compose files",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="clone over https (public repositories only) instead of ssh with the deploy key; "
        "v1 used ssh, so ssh is the default",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan and write nothing (no database use)"
    )


def run_import(
    args: argparse.Namespace, services: ServicesFactory, out: TextIO | None = None
) -> int:
    """Execute the import; exit code 0 on success, 1 when anything failed or was rejected."""
    stream = out or sys.stdout
    try:
        projects = load_projects(args.projects_json)
        registry: V1Registry | None = load_registry(args.v1_db) if args.v1_db else None
        transport = GitTransport.HTTPS if args.https else GitTransport.SSH
        plan = build_plan(projects, registry, args.compose_root, transport)
        if args.dry_run:
            stream.write(render_plan(plan))
            stream.write("dry run: nothing was written\n")
            return 1 if plan.has_errors else 0
        stream.write(render_plan(plan, full=False))
        outcomes = apply_plan(plan, services(), IMPORT_ACTOR)
    except DomainError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    stream.write(render_outcomes(outcomes))
    return 1 if any(outcome.errors for outcome in outcomes) else 0
