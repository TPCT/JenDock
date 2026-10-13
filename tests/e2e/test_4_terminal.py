"""Step 6: a terminal in the web container over a WebSocket; audit keeps open and close only."""

from __future__ import annotations

import json
import time

import pytest
from websockets.exceptions import ConnectionClosed

from tests.e2e.client import Api, Terminal
from tests.e2e.scenario import ENV, Scenario
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e
QUERY = (
    "import json, sqlite3; c = sqlite3.connect('file:/var/lib/jendock/jendock.db?mode=ro', "
    'uri=True); print(json.dumps(c.execute("SELECT action, target_type, details FROM audit_events '
    "WHERE action LIKE 'terminal.%' ORDER BY id\").fetchall()))"
)


def _audit_rows(stack: Stack, expected: int) -> list[list[str]]:
    """The terminal audit rows (the close event is written when the shell has ended)."""
    for _ in range(20):
        rows: list[list[str]] = json.loads(stack.exec_api("python", "-c", QUERY).stdout)
        if len(rows) >= expected:
            return rows
        time.sleep(0.5)
    return rows


def test_11_terminal_runs_a_command_and_audits_open_and_close_only(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    web_id = scenario.data["web_id"]
    ticket = api.post(f"{ENV}/containers/{web_id}/terminal/tickets", {})
    assert ticket["websocket_path"] == "/api/terminal/ws"
    assert ticket["container_id"] == web_id
    terminal = Terminal(api, ticket["ticket"], ticket["websocket_path"])
    terminal.send("echo e2e-ok\n")
    output = terminal.read_until("\r\ne2e-ok\r\n")  # the output line, not the echoed command
    assert "e2e-ok" in output
    exit_message = terminal.close()
    assert exit_message is not None
    assert exit_message["reason"] in {"closed", "exited"}
    # a ticket is single use
    with pytest.raises((AssertionError, ConnectionClosed)):
        Terminal(api, ticket["ticket"], ticket["websocket_path"])
    rows = _audit_rows(stack, expected=2)
    assert [r[0] for r in rows] == ["terminal.open", "terminal.close"]
    for _action, _target, details in rows:
        assert "e2e-ok" not in details
        assert "echo" not in details
    assert "e2e-ok" not in stack.logs("api", 200) + stack.logs("worker", 200)
