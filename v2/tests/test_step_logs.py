"""Step log files: bounds, masking and chunked reads."""

from __future__ import annotations

from pathlib import Path

from backend.execution.logs import TRUNCATION_MARKER, LogStore, RunRedactor


def _store(tmp_path: Path) -> LogStore:
    return LogStore(tmp_path / "logs")


def test_lines_are_masked_before_they_reach_the_file(tmp_path: Path) -> None:
    redactor = RunRedactor()
    redactor.register("hunter2hunter2")
    store = _store(tmp_path)
    writer = store.open_writer(5, 9, redactor, 1000)
    writer.write_line("password=hunter2hunter2")
    writer.close()
    assert store.path(5, 9).read_text() == "password=[REDACTED]\n"


def test_secret_registered_later_is_masked_from_then_on(tmp_path: Path) -> None:
    redactor = RunRedactor()
    writer = _store(tmp_path).open_writer(1, 1, redactor, 1000)
    writer.write_line("before late-secret-1")
    redactor.register("late-secret-1")
    writer.write_line("after late-secret-1")
    writer.close()
    assert (tmp_path / "logs" / "1" / "1.log").read_text() == (
        "before late-secret-1\nafter [REDACTED]\n"
    )


def test_one_truncation_marker_then_silence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    writer = store.open_writer(1, 1, RunRedactor(), 50)
    for number in range(20):
        writer.write_line(f"line {number:02d} of output")
    writer.close()
    text = store.path(1, 1).read_text()
    assert text.count(TRUNCATION_MARKER) == 1
    assert text.splitlines()[-1] == TRUNCATION_MARKER
    assert "line 19" not in text


def test_permissions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.open_writer(3, 4, RunRedactor(), 100).close()
    assert store.path(3, 4).stat().st_mode & 0o777 == 0o640
    assert store.path(3, 4).parent.stat().st_mode & 0o777 == 0o750


def test_chunked_reading_follows_offsets_on_line_boundaries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    writer = store.open_writer(1, 1, RunRedactor(), 10_000)
    for number in range(10):
        writer.write_line(f"entry {number}")
    writer.close()
    collected = []
    offset = 0
    while True:
        chunk = store.read_chunk(1, 1, offset, limit=25)
        assert chunk.text.endswith("\n")
        collected.append(chunk.text)
        offset = chunk.next_offset
        if chunk.at_end:
            break
    assert "".join(collected) == "".join(f"entry {n}\n" for n in range(10))
    assert len(collected) > 2


def test_chunk_never_splits_a_multibyte_character(tmp_path: Path) -> None:
    store = _store(tmp_path)
    writer = store.open_writer(1, 1, RunRedactor(), 10_000)
    writer.write_line("é" * 30)
    writer.close()
    first = store.read_chunk(1, 1, 0, limit=7)
    second = store.read_chunk(1, 1, first.next_offset, limit=1000)
    assert "�" not in first.text + second.text
    assert first.text + second.text == "é" * 30 + "\n"


def test_missing_log_and_out_of_range_offset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.read_chunk(1, 2, 0).text == ""
    store.open_writer(1, 2, RunRedactor(), 100).close()
    chunk = store.read_chunk(1, 2, 999)
    assert (chunk.offset, chunk.next_offset, chunk.text) == (0, 0, "")


def test_multiline_secret_lines_are_masked_individually() -> None:
    redactor = RunRedactor()
    redactor.register("-----BEGIN-----\nsecretbody123\n-----END-----")
    assert redactor.redact("x secretbody123 y") == "x [REDACTED] y"
