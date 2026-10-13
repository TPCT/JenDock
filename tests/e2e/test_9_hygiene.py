"""Step 11 (beyond the brief): no secret value ended up in a log or in the audit trail."""

from __future__ import annotations

import pytest

from tests.e2e.client import Api
from tests.e2e.scenario import Scenario
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e
DUMP_AUDIT = (
    "import sqlite3; c = sqlite3.connect('file:/var/lib/jendock/jendock.db?mode=ro', "
    "uri=True); print(chr(10).join(str(r) for r in c.execute("
    "'SELECT action, actor_type, target_type, target_id, details FROM audit_events')))"
)


def test_20_secret_values_never_appear_in_logs_or_the_audit_trail(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    secrets = {
        "admin password": stack.admin_password,
        "webhook secret": stack.webhook_secret,
        "database password": stack.db_password,
        "API token": scenario.token,
        "session cookie": api.cookie.split("=", 1)[1],
        "csrf token": api.csrf,
    }
    audit = stack.exec_api("python", "-c", DUMP_AUDIT).stdout
    assert "database.query" in audit  # the audit is really there, and is searched below
    texts = {
        "api log": stack.logs("api", 2000),
        "worker log": stack.logs("worker", 2000),
        "audit trail": audit,
    }
    for label, value in secrets.items():
        for where, text in texts.items():
            assert value not in text, f"the {label} leaked into the {where}"
    for action in ("run.create", "github.webhook", "deployment.redeploy", "container.env.update"):
        assert action in audit, f"{action} was not audited"
