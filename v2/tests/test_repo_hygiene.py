"""No merge conflict markers may be left in the tree (a leftover once broke the worker image)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIPPED_DIRS = {
    ".venv",
    "node_modules",
    ".git",
    "__pycache__",
    "dist",
    ".mypy_cache",
    ".ruff_cache",
}
MARKERS = ("<" * 7 + " ", ">" * 7 + " ")
MAX_BYTES = 2 * 1024 * 1024


def _text_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if any(part in SKIPPED_DIRS or part.endswith(".egg-info") for part in path.parts):
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
