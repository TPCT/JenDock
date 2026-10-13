"""The terminal bridge on a local shell: open, input, resize, limits, detach and reattach."""

from __future__ import annotations

from pathlib import Path

from backend.terminal.session import TerminalLimits
from tests.terminal_support import Client, terminal_server, wait_for


def test_echo_hi_and_exit(socket_dir: Path) -> None:
    with terminal_server(socket_dir) as server:
        client = Client(server.path)
        client.open()
        client.pump(until=None, timeout=0.3)
        assert client.terminal_id is not None
        client.send("echo hi\n")
        client.pump(until=b"\r\nhi\r\n")
        assert b"\r\nhi\r\n" in client.output
        client.send("exit\n")
        client.pump()
        assert client.exit == {"code": 0, "reason": "exited"}
        assert wait_for(lambda: server.manager.count() == 0)
        assert wait_for(lambda: server.recorder.events[-1:] == [("close", "exited")])
        assert [kind for kind, _ in server.recorder.events] == ["open", "close"]


def test_resize_reaches_the_terminal(socket_dir: Path) -> None:
    with terminal_server(socket_dir) as server:
        client = Client(server.path)
        client.open(rows=24, cols=80)
        client.pump(timeout=0.3)
        client.resize(30, 100)
        client.send("stty size\n")
        client.pump(until=b"30 100")
        assert b"30 100" in client.output
        client.close_terminal()
        client.pump()
        assert client.exit is not None and client.exit["reason"] == "closed"


def test_input_out_of_sequence_ends_the_connection_and_detaches(socket_dir: Path) -> None:
    with terminal_server(socket_dir) as server:
        client = Client(server.path)
        client.open()
        client.pump(timeout=0.3)
        client.send_raw_sequence(7, b"x")
        client.pump()
        assert client.error is not None and "sequence" in client.error
        # the terminal itself lives on for the detach grace period, then is cleaned up
        assert server.manager.count() == 1
        server.manager.shutdown()
        assert server.manager.count() == 0


def test_idle_timeout_after_disconnect(socket_dir: Path) -> None:
    limits = TerminalLimits(idle_seconds=0.6, max_seconds=60)
    with terminal_server(socket_dir, limits) as server:
        client = Client(server.path)
        client.open()
        client.pump(timeout=0.3)
        client.drop()
        assert wait_for(lambda: server.manager.count() == 0, timeout=5)
        assert server.recorder.events[-1] == ("close", "idle_timeout")


def test_reattach_within_the_grace_period(socket_dir: Path) -> None:
    limits = TerminalLimits(idle_seconds=5, max_seconds=60)
    with terminal_server(socket_dir, limits) as server:
        first = Client(server.path)
        first.open()
        first.pump(timeout=0.3)
        terminal_id = first.terminal_id
        first.send("MARK=kept\n")
        first.drop()
        second = Client(server.path)
        second.open(attach=terminal_id)
        second.pump(timeout=0.3)
        assert second.terminal_id == terminal_id
        second.send("echo $MARK\n")
        second.pump(until=b"\r\nkept\r\n")
        assert b"\r\nkept\r\n" in second.output
        second.close_terminal()
        second.pump()
        assert wait_for(lambda: server.recorder.events[-1] == ("close", "closed"))


def test_reattach_by_someone_else_is_refused(socket_dir: Path) -> None:
    with terminal_server(socket_dir) as server:
        first = Client(server.path)
        first.open()
        first.pump(timeout=0.3)
        intruder = Client(server.path)
        intruder.open(attach=first.terminal_id, actor={"type": "user", "id": "99"})
        intruder.pump()
        assert intruder.error is not None and "belongs to someone else" in intruder.error
        first.send("echo still-mine\n")
        first.pump(until=b"still-mine\r\n")
        assert b"still-mine" in first.output


def test_reattach_replaces_the_previous_connection(socket_dir: Path) -> None:
    with terminal_server(socket_dir) as server:
        first = Client(server.path)
        first.open()
        first.pump(timeout=0.3)
        second = Client(server.path)
        second.open(attach=first.terminal_id)
        second.pump(timeout=0.3)
        first.pump(timeout=1)  # its socket was closed by the worker
        second.send("echo second\n")
        second.pump(until=b"\r\nsecond\r\n")
        assert b"\r\nsecond\r\n" in second.output


def test_absolute_timeout_ends_an_attached_terminal(socket_dir: Path) -> None:
    limits = TerminalLimits(idle_seconds=60, max_seconds=0.8)
    with terminal_server(socket_dir, limits) as server:
        client = Client(server.path)
        client.open()
        client.pump(timeout=5)
        assert client.exit is not None and client.exit["reason"] == "absolute_timeout"
        assert wait_for(lambda: server.manager.count() == 0)


def test_session_limits(socket_dir: Path) -> None:
    limits = TerminalLimits(max_sessions=2, max_per_actor=1)
    with terminal_server(socket_dir, limits) as server:
        first = Client(server.path)
        first.open()
        first.pump(timeout=0.3)
        second = Client(server.path)
        second.open()
        second.pump()
        assert second.error is not None and "too many" in second.error
        other_actor = Client(server.path)
        other_actor.open(actor={"type": "user", "id": "2"})
        other_actor.pump(timeout=0.3)
        assert other_actor.terminal_id is not None


def test_unknown_container_is_reported_and_nothing_starts(socket_dir: Path) -> None:
    with terminal_server(socket_dir) as server:
        server.opener.missing = True
        client = Client(server.path)
        client.open()
        client.pump()
        assert client.error == "container not found"
        assert server.manager.count() == 0 and server.recorder.events == []


def test_garbage_and_invalid_open_are_rejected(socket_dir: Path) -> None:
    with terminal_server(socket_dir) as server:
        garbage = Client(server.path)
        garbage.socket.sendall(b"\xff\xff\xff\xff\xff")
        garbage.pump()
        assert garbage.error is not None
        bad = Client(server.path)
        bad.open(container_id="short")
        bad.pump()
        assert bad.error == "invalid request"
        assert server.manager.count() == 0


def test_the_worker_never_keeps_input_or_output(socket_dir: Path) -> None:
    """Metadata events carry no terminal content."""
    with terminal_server(socket_dir) as server:
        client = Client(server.path)
        client.open()
        client.pump(timeout=0.3)
        client.send("echo super-private-command\n")
        client.pump(until=b"\r\nsuper-private-command\r\n")
        client.send("exit\n")
        client.pump()
        wait_for(lambda: server.manager.count() == 0)
        assert "super-private-command" not in repr(server.recorder.events)
