"""Pure units of container management: parsers, masking, overrides, tickets, protocol, telemetry."""

from __future__ import annotations

import io
import json
import stat
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest

from backend.adapters.compose_overrides import (
    FILE_NAME,
    ComposeOverrideStore,
    OverrideFileError,
    OverrideSet,
    ServiceOverride,
)
from backend.adapters.container_admin import parse_tar_member
from backend.adapters.docker_inspect import parse_container, parse_docker_time, parse_env
from backend.adapters.docker_stats import parse_percent, parse_size, parse_stats
from backend.adapters.errors import ContainerRuntimeError
from backend.api.terminal_tickets import PrincipalKind, TicketGrant, TicketStore
from backend.containers.confirmations import ConfirmationStore
from backend.containers.masking import LogMasker
from backend.containers.telemetry import (
    HostSampler,
    RingBuffer,
    parse_cpu_times,
    parse_meminfo,
)
from backend.domain.actor import Actor
from backend.domain.containers import EnvironmentKey, validate_container_path
from backend.domain.enums import ActorType, EnvironmentName
from backend.domain.errors import RateLimitedError, ValidationError
from backend.terminal.protocol import (
    MAX_DATA_BYTES,
    Frame,
    FrameDecoder,
    FrameType,
    ProtocolError,
    SequenceCounter,
    decode_data,
    decode_json,
    encode_data,
    encode_frame,
    encode_json,
)
from tests.api_support import FakeClock
from tests.container_support import make_record

KEY = EnvironmentKey("demo", EnvironmentName.DEVELOPMENT)
ACTOR = Actor(ActorType.USER, "1")

# --- docker inspect ---------------------------------------------------------------


def test_parse_container_reads_labels_and_state() -> None:
    record = make_record(
        labels={
            "org.opencontainers.image.revision": "abc123",
            "jendock.commit": "def456",
        },
        health="healthy",
        restart_count=3,
    )
    info = parse_container(record)
    assert (info.revision, info.commit, info.health, info.restart_count) == (
        "abc123",
        "def456",
        "healthy",
        3,
    )
    assert info.started_at is not None and info.started_at.year == 2026
    assert info.image_id.startswith("sha256:")
    assert info.running and info.service == "web"


def test_parse_env_keeps_equals_signs_in_values() -> None:
    record = make_record(env={"A": "x=y", "B": ""})
    assert parse_env(record) == {"A": "x=y", "B": ""}


def test_parse_docker_time() -> None:
    parsed = parse_docker_time("2026-10-13T12:00:00.123456789Z")
    assert parsed is not None and parsed.microsecond == 123456
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    assert parse_docker_time("garbage") is None
    assert parse_docker_time(None) is None


# --- stats ------------------------------------------------------------------------


def test_stats_parsing() -> None:
    line = json.dumps(
        {
            "ID": "a" * 64,
            "Name": "web",
            "CPUPerc": "12.50%",
            "MemUsage": "10.5MiB / 1.5GiB",
            "MemPerc": "0.68%",
            "NetIO": "1.2kB / 3MB",
            "BlockIO": "0B / 4kB",
            "PIDs": "7",
        }
    )
    [stats] = parse_stats(line + "\nnot json\n")
    assert stats.cpu_percent == 12.5 and stats.pids == 7
    assert stats.memory_used == int(10.5 * 1024**2) and stats.memory_limit == int(1.5 * 1024**3)
    assert (stats.net_rx, stats.net_tx) == (1200, 3_000_000)
    assert parse_size("garbage") == 0 and parse_percent("--") == 0.0


# --- tar --------------------------------------------------------------------------


def _tar(name: str, data: bytes = b"", kind: bytes = tarfile.REGTYPE, link: str = "") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = len(data)
        info.linkname = link
        archive.addfile(info, io.BytesIO(data) if data else None)
    return buffer.getvalue()


def test_tar_member_kinds_and_cap() -> None:
    assert parse_tar_member(_tar("f", b"hello"), 100).data == b"hello"
    cut = parse_tar_member(_tar("f", b"0123456789"), 4)
    assert (cut.data, cut.truncated, cut.size) == (b"0123", True, 10)
    assert parse_tar_member(_tar("d", kind=tarfile.DIRTYPE), 10).kind == "directory"
    assert (
        parse_tar_member(_tar("l", kind=tarfile.SYMTYPE, link="/etc/passwd"), 10).kind == "symlink"
    )
    long_name = "x" * 300
    assert parse_tar_member(_tar(long_name, b"ok"), 10).data == b"ok"
    with pytest.raises(ContainerRuntimeError):
        parse_tar_member(b"short", 10)
    with pytest.raises(ContainerRuntimeError):
        parse_tar_member(_tar("f", b"hello")[:514], 100)


# --- masking ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("login with s3cr3t-value ok", "s3cr3t-value"),
        ("DB_PASSWORD=plain123 started", "plain123"),
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ('{"api_key": "k-123456"}', "k-123456"),
        ("-----BEGIN RSA PRIVATE KEY-----MIIE...", "MIIE"),
        ("token=abc&other=1", "abc"),
    ],
)
def test_masker_hides_secrets(line: str, secret: str) -> None:
    masker = LogMasker({"API_TOKEN": "s3cr3t-value", "PATH": "/usr/bin"})
    masked = masker.mask(line)
    assert secret not in masked and "[REDACTED]" in masked


def test_masker_leaves_ordinary_lines_and_short_values_alone() -> None:
    masker = LogMasker({"API_TOKEN": "ab"})
    assert masker.mask("GET /health 200 in 3ms") == "GET /health 200 in 3ms"
    assert masker.mask("ab cd") == "ab cd"


# --- override file ----------------------------------------------------------------


def test_override_roundtrip_escaping_and_permissions(tmp_path: Path) -> None:
    store = ComposeOverrideStore(tmp_path)
    overrides = OverrideSet().with_service(
        "web", ServiceOverride({"PRICE": "$5", "Q": 'a"b\nc'}, cpus=1.5, memory_bytes=1024**3)
    )
    store.save(KEY, overrides)
    path = store.path(KEY)
    assert path.name == FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    document = json.loads(path.read_text())
    assert document["services"]["web"]["environment"]["PRICE"] == "$$5"
    assert document["services"]["web"]["mem_limit"] == 1024**3
    assert store.load(KEY) == overrides
    assert store.compose_file_args(KEY) == ["-f", str(path)]


def test_override_store_snapshot_restore_and_removal(tmp_path: Path) -> None:
    store = ComposeOverrideStore(tmp_path)
    assert store.snapshot(KEY) is None and store.compose_file_args(KEY) == []
    store.save(KEY, OverrideSet().with_service("web", ServiceOverride({"A": "1"})))
    snapshot = store.snapshot(KEY)
    store.save(KEY, OverrideSet().with_service("web", ServiceOverride({"A": "2"})))
    store.restore(KEY, snapshot)
    assert store.load(KEY).for_service("web").env == {"A": "1"}
    store.save(KEY, OverrideSet())
    assert store.snapshot(KEY) is None
    store.restore(KEY, None)  # idempotent


def test_override_store_rejects_foreign_and_broken_files(tmp_path: Path) -> None:
    store = ComposeOverrideStore(tmp_path)
    store.restore(KEY, b'{"services": {}}')
    with pytest.raises(OverrideFileError):
        store.load(KEY)
    store.restore(KEY, b"not json")
    with pytest.raises(OverrideFileError):
        store.load(KEY)
    with pytest.raises(OverrideFileError):
        store.path(EnvironmentKey("../evil", EnvironmentName.DEVELOPMENT))


def test_with_service_drops_empty_entries() -> None:
    overrides = OverrideSet().with_service("web", ServiceOverride({"A": "1"}))
    assert overrides.with_service("web", ServiceOverride()).is_empty


# --- confirmations ----------------------------------------------------------------


def test_confirmation_expiry_and_prefix_binding() -> None:
    clock = FakeClock()
    store = ConfirmationStore(clock)
    token, _ = store.issue(ACTOR, ("p/e", "id", "stop"))
    assert store.consume(token, ACTOR, ("p/e", "id")) == ("p/e", "id", "stop")
    token, _ = store.issue(ACTOR, ("p/e", "id", "stop"))
    clock.advance(seconds=61)
    with pytest.raises(ValidationError):
        store.consume(token, ACTOR, ("p/e", "id"))
    token, _ = store.issue(ACTOR, ("p/e", "id", "stop"))
    with pytest.raises(ValidationError):
        store.consume(token, ACTOR, ("p/e", "other-id"))


# --- paths ------------------------------------------------------------------------


def test_path_validation_accepts_clean_paths() -> None:
    assert validate_container_path("/") == "/"
    assert validate_container_path("/app/src/main.py") == "/app/src/main.py"
    with pytest.raises(ValidationError):
        validate_container_path("")


# --- terminal tickets -------------------------------------------------------------


def _grant(clock: FakeClock, principal_id: int = 1) -> TicketGrant:
    return TicketGrant(
        PrincipalKind.SESSION,
        principal_id,
        ACTOR,
        "demo",
        EnvironmentName.DEVELOPMENT,
        "a" * 64,
        None,
        clock() + timedelta(seconds=30),
    )


def test_tickets_are_single_use_and_expire() -> None:
    clock = FakeClock()
    store = TicketStore(clock)
    ticket = store.issue(_grant(clock))
    assert store.redeem(ticket) is not None
    assert store.redeem(ticket) is None
    ticket = store.issue(_grant(clock))
    clock.advance(seconds=31)
    assert store.redeem(ticket) is None
    assert store.redeem("never issued") is None


def test_ticket_issuance_is_bounded_per_principal() -> None:
    clock = FakeClock()
    store = TicketStore(clock)
    for _ in range(10):
        store.issue(_grant(clock))
    with pytest.raises(RateLimitedError):
        store.issue(_grant(clock))
    store.issue(_grant(clock, principal_id=2))


# --- frame protocol ---------------------------------------------------------------


def test_frames_roundtrip_across_arbitrary_chunking() -> None:
    wire = (
        encode_json(FrameType.OPEN, {"a": 1})
        + encode_data(0, b"hello")
        + encode_frame(FrameType.CLOSE)
    )
    decoder = FrameDecoder()
    frames: list[Frame] = []
    for index in range(len(wire)):
        frames += decoder.feed(wire[index : index + 1])
    assert [f.type for f in frames] == [FrameType.OPEN, FrameType.DATA, FrameType.CLOSE]
    assert decode_json(frames[0]) == {"a": 1}
    assert decode_data(frames[1]) == (0, b"hello")


def test_decoder_rejects_violations() -> None:
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(bytes([99, 0, 0, 0, 0]))
    with pytest.raises(ProtocolError):
        FrameDecoder().feed(bytes([FrameType.OPEN, 0, 0, 0x20, 0]))
    with pytest.raises(ProtocolError):
        encode_data(0, b"x" * (MAX_DATA_BYTES + 1))
    with pytest.raises(ProtocolError):
        decode_json(Frame(FrameType.OPEN, b"[1]"))


def test_sequence_counter_detects_gaps_and_repeats() -> None:
    counter = SequenceCounter()
    counter.expect(0)
    counter.expect(1)
    with pytest.raises(ProtocolError):
        counter.expect(1)
    with pytest.raises(ProtocolError):
        counter.expect(5)
    assert [SequenceCounter().take()] == [0]


# --- telemetry --------------------------------------------------------------------


def test_proc_parsers() -> None:
    times = parse_cpu_times("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3 4 5\n")
    assert times == (850, 1000)
    memory = parse_meminfo("MemTotal:       2048 kB\nMemAvailable:    512 kB\nSwapTotal: 0 kB\n")
    assert memory["MemTotal"] == 2048 * 1024 and memory["MemAvailable"] == 512 * 1024


def test_host_sampler_reads_a_proc_tree(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    (proc / "loadavg").write_text("0.50 0.25 0.10 1/100 123\n")
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0\n")
    sampler = HostSampler(tmp_path, proc)
    first = sampler.sample()
    assert first.cpu_percent is None and first.memory_used == 600 * 1024 and first.load_1 == 0.5
    assert first.disk_total > 0 and first.disk_used >= 0
    (proc / "stat").write_text("cpu  200 0 200 1000 0 0 0 0\n")  # +200 busy, +200 idle
    assert sampler.sample().cpu_percent == 50.0


def test_ring_buffer_is_bounded() -> None:
    buffer: RingBuffer[int] = RingBuffer(3)
    for value in range(10):
        buffer.add(value)
    assert buffer.items() == [7, 8, 9] and buffer.last() == 9
