"""Admin command line: ``python -m shiftcodes.cli users create|reset-password <username>`` and
``import-v1`` (see ``shiftcodes.importer``).

Passwords are read from a hidden prompt, or from stdin with ``--password-stdin``. They are
never accepted as arguments or environment variables, so they stay out of shell history
and process listings.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from shiftcodes.config import get_settings
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.errors import DomainError, ValidationError
from shiftcodes.importer.apply import ImportServices
from shiftcodes.importer.cli import add_arguments as add_import_arguments
from shiftcodes.importer.cli import run_import
from shiftcodes.process import apply_shared_umask
from shiftcodes.repositories.database import UnitOfWork, create_db_engine, create_session_factory
from shiftcodes.services.environments import EnvironmentService
from shiftcodes.services.passwords import PasswordHasher
from shiftcodes.services.pipelines import PipelineService
from shiftcodes.services.projects import ProjectService
from shiftcodes.services.users import UserService

CLI_ACTOR = Actor.system("cli")
PasswordReader = Callable[[], str]


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser (no password option exists by design)."""
    parser = argparse.ArgumentParser(prog="python -m shiftcodes.cli")
    groups = parser.add_subparsers(dest="group", required=True)
    users = groups.add_parser("users", help="manage admin users").add_subparsers(
        dest="command", required=True
    )
    for name, help_text in (("create", "create a user"), ("reset-password", "set a new password")):
        command = users.add_parser(name, help=help_text)
        command.add_argument("username")
        command.add_argument(
            "--password-stdin",
            action="store_true",
            help="read the password from the first line of stdin instead of prompting",
        )
    add_import_arguments(
        groups.add_parser(
            "import-v1", help="import v1 projects and convert recipes to Jenkinsfiles"
        )
    )
    return parser


def read_password(from_stdin: bool, stdin: TextIO, prompt: Callable[[str], str]) -> str:
    """Read a password from stdin or prompt twice and require both entries to match."""
    if from_stdin:
        return stdin.readline().rstrip("\r\n")
    first = prompt("Password: ")
    if prompt("Repeat password: ") != first:
        raise ValidationError("passwords do not match")
    return first


def run(
    argv: Sequence[str],
    service: UserService,
    stdin: TextIO | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    """Execute one command; returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        password = read_password(args.password_stdin, stdin or sys.stdin, prompt)
        if args.command == "create":
            service.create(args.username, password, CLI_ACTOR)
            print(f"created user {args.username}")
        else:
            service.reset_password(args.username, password, CLI_ACTOR)
            print(f"password reset for {args.username}")
    except DomainError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _import_v1(args: argparse.Namespace) -> int:
    """Run the importer; a dry run never opens the database."""
    if args.dry_run:
        return run_import(args, _no_database)
    engine = create_db_engine(get_settings().database_url)
    try:
        factory = create_session_factory(engine)

        def services() -> ImportServices:
            return ImportServices(
                ProjectService(UnitOfWork(factory)),
                EnvironmentService(UnitOfWork(factory)),
                PipelineService(UnitOfWork(factory)),
            )

        return run_import(args, services)
    finally:
        engine.dispose()


def _no_database() -> ImportServices:
    raise RuntimeError("a dry run must not use the database")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point wiring the real database."""
    apply_shared_umask()
    parsed = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(parsed)
    if args.group == "import-v1":
        return _import_v1(args)
    engine = create_db_engine(get_settings().database_url)
    try:
        service = UserService(UnitOfWork(create_session_factory(engine)), PasswordHasher())
        return run(parsed, service)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
