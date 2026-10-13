"""``python -m backend.cli backup create|restore|prune``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from backend.backup.create import KEY_WARNING, create_backup
from backend.backup.manifest import BackupError
from backend.backup.restore import restore_backup
from backend.backup.retention import prune_archives
from backend.backup.schedule import source_of
from backend.config import Settings


def add_backup_parser(groups: Any) -> None:
    """Register the ``backup`` command group on the CLI's subparsers."""
    backup = groups.add_parser("backup", help="back up and restore the platform state")
    commands = backup.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="write a timestamped archive")
    create.add_argument("--to", required=True, type=Path, help="directory for the archive")
    create.add_argument("--include-logs", action="store_true", help="also archive the run logs")
    create.add_argument(
        "--include-key",
        action="store_true",
        help="ALSO write the secret key to a separate 0600 file next to the archive (dangerous)",
    )
    create.add_argument("--keep", type=int, help="afterwards keep only the newest N archives")
    restore = commands.add_parser("restore", help="restore an archive into an empty data dir")
    restore.add_argument("--from", dest="source", required=True, type=Path, help="archive")
    restore.add_argument("--key-file", type=Path, help="a saved key file to put in place too")
    prune = commands.add_parser("prune", help="delete all but the newest N archives")
    prune.add_argument("--dir", required=True, type=Path)
    prune.add_argument("--keep", required=True, type=int)


def run_backup(args: argparse.Namespace, settings: Settings) -> int:
    """Execute a backup command; returns the exit code."""
    try:
        if args.command == "create":
            return _create(args, settings)
        if args.command == "restore":
            return _restore(args, settings)
        removed = prune_archives(args.dir, args.keep)
        print(f"removed {len(removed)} old archive(s)")
    except (BackupError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _create(args: argparse.Namespace, settings: Settings) -> int:
    result = create_backup(
        source_of(settings), args.to, include_logs=args.include_logs, include_key=args.include_key
    )
    print(f"backup written: {result.archive}")
    print(
        f"alembic revision: {result.manifest.alembic_revision}; files: {len(result.manifest.files)}"
    )
    if result.key_copy is not None:
        print(KEY_WARNING.format(path=result.key_copy), file=sys.stderr)
    if args.keep is not None:
        removed = prune_archives(args.to, args.keep)
        print(f"removed {len(removed)} old archive(s)")
    return 0


def _restore(args: argparse.Namespace, settings: Settings) -> int:
    result = restore_backup(args.source, source_of(settings), key_file=args.key_file)
    print(f"restored {result.files} file(s) at alembic revision {result.manifest.alembic_revision}")
    print("next: run the migrations (make migrate / python -m backend.repositories.migrate)")
    if not result.key_restored:
        print(
            "the secret key was not restored: put the matching key file in place", file=sys.stderr
        )
    return 0
