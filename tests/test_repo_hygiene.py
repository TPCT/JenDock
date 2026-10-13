"""No merge conflict markers may be left in the tree (a leftover once broke the worker image)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIPPED_DIRS = {
    ".claude",
    ".venv",
    "node_modules",
    ".git",
    "__pycache__",
    "dist",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "coverage",
}
MARKERS = ("<" * 7 + " ", ">" * 7 + " ")
MAX_BYTES = 2 * 1024 * 1024


def _candidate_paths() -> list[Path]:
    """Files git would see (tracked, or untracked but not ignored); a plain walk outside git."""
    git = shutil.which("git")
    if git is None:
        return list(ROOT.rglob("*"))
    try:
        listed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return list(ROOT.rglob("*"))
    return [ROOT / name for name in listed.split("\0") if name]


def _text_files() -> list[Path]:
    found = []
    for path in _candidate_paths():
        relative = path.relative_to(ROOT)
        if any(part in SKIPPED_DIRS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_file() and path.stat().st_size <= MAX_BYTES:
            found.append(path)
    return found


def test_no_conflict_markers_in_the_tree() -> None:
    offenders = []
    for path in _text_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        offenders += [
            f"{path.relative_to(ROOT)}:{number}"
            for number, line in enumerate(lines, start=1)
            if line.startswith(MARKERS)
        ]
    assert offenders == []


# The product is called Jendock; its former working name must not reappear anywhere (built from
# parts so that this file does not contain it either).

FORMER_NAME = "shift" + "codes"
# Nor may its two-letter abbreviation followed by a dash, slash or underscore start a name (an
# image, container or token prefix); a letter, digit or underscore before it makes it part of
# another word.
SOURCE_DIRS = {"backend", "tests", "frontend"}
FORMER_PREFIX = re.compile(r"(?<![A-Za-z0-9_])" + "s" + "c" + r"[-/_]")


def test_the_former_abbreviation_is_not_used_as_a_name_prefix() -> None:
    offenders = []
    for path in _text_files():
        relative = path.relative_to(ROOT)
        if relative.parts[0] not in SOURCE_DIRS or "fixtures" in relative.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        offenders += [
            f"{relative}:{number}"
            for number, line in enumerate(text.splitlines(), start=1)
            if FORMER_PREFIX.search(line)
        ]
    assert offenders == []


def test_the_former_product_name_appears_nowhere() -> None:
    offenders = []
    for path in _text_files():
        relative = path.relative_to(ROOT)
        if FORMER_NAME in str(relative).lower():
            offenders.append(f"{relative} (path)")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if FORMER_NAME in text.lower():
            offenders.append(str(relative))
    assert offenders == []
