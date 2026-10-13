"""``python -m backend.cli deploy-key create|show|list``: per-repository GitHub deploy keys.

Only public keys are printed; the private key stays in the keys directory (mode 0600).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from backend.adapters.deploy_keys import DeployKeyStore
from backend.domain.actor import Actor
from backend.domain.audit import AuditTarget
from backend.domain.errors import DomainError
from backend.services.audit import AuditService

CLI_ACTOR = Actor.system("cli")
AuditRecorder = Callable[[str, str], None]  # (action, repository)


def add_deploy_key_parser(groups: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``deploy-key`` group on the top-level parser."""
    commands = groups.add_parser(
        "deploy-key", help="per-repository GitHub deploy keys"
    ).add_subparsers(dest="deploy_key_command", required=True)
    create = commands.add_parser("create", help="generate a key and print its public key")
    create.add_argument("repository", help="owner/name")
    create.add_argument(
        "--rotate", action="store_true", help="replace an existing key (update GitHub too)"
    )
    show = commands.add_parser("show", help="print the public key of a repository")
    show.add_argument("repository", help="owner/name")
    commands.add_parser("list", help="list the repositories that have a deploy key")


def audit_recorder(service: AuditService) -> AuditRecorder:
    """Records key events; the details never hold key material."""

    def record(action: str, repository: str) -> None:
        service.record(
            CLI_ACTOR, action, AuditTarget("repository", repository), {"repository": repository}
        )

    return record


def run_deploy_key(
    args: argparse.Namespace, store: DeployKeyStore, record: AuditRecorder | None = None
) -> int:
    """Execute one deploy-key command; stdout carries public keys and names only."""
    try:
        if args.deploy_key_command == "create":
            created = store.create(args.repository, rotate=args.rotate)
            if record is not None:
                record(
                    "deploy_key.rotate" if created.rotated else "deploy_key.create", args.repository
                )
            print(created.public_key)
        elif args.deploy_key_command == "show":
            print(store.public_key(args.repository))
        else:
            for repository in store.repositories():
                print(repository)
    except DomainError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
