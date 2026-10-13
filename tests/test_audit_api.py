"""The audit log API: filters, pagination, descriptions and CSV export."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.container import AppContainer
from backend.domain.actor import Actor
from backend.domain.audit import AuditTarget
from backend.domain.audit_text import describe_audit, known_actions
from backend.domain.enums import ActorType, EnvironmentName, RunStatus, RunTrigger
from backend.domain.errors import ValidationError
from backend.repositories.models import Domain, Run
from backend.services import audit_log
from backend.services.audit import AuditService
from backend.services.projects import ProjectService
from tests.api_support import FakeClock, login, mutation_headers


@pytest.fixture
def csrf(client: TestClient, admin: int) -> str:
    return login(client)


def _record(
    container: AppContainer,
    action: str,
    target: AuditTarget,
    details: dict[str, object] | None = None,
    actor: Actor | None = None,
) -> None:
    AuditService(container.uow(), container.clock).record(
        actor or Actor.system("test"), action, target, details
    )


def _search(client: TestClient, query: str = "") -> dict[str, Any]:
    response = client.get(f"/api/audit{query}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _actions(body: dict[str, Any]) -> list[str]:
    return [item["action"] for item in body["items"]]


def test_requires_a_browser_session(client: TestClient, csrf: str) -> None:
    anonymous = TestClient(client.app, base_url=str(client.base_url))
    assert anonymous.get("/api/audit").status_code == 401
    assert anonymous.get("/api/audit/export.csv").status_code == 401
    token = client.post(
        "/api/tokens",
        json={"name": "all", "scopes": ["admin"]},
        headers=mutation_headers(client, csrf),
    ).json()["token"]
    bearer = anonymous.get("/api/audit", headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 403


def test_newest_first_with_descriptions_and_actor_names(
    container: AppContainer, client: TestClient, csrf: str, admin: int, clock: FakeClock
) -> None:
    clock.advance(minutes=1)
    _record(
        container,
        "secret.reveal",
        AuditTarget("environment", "1"),
        {"name": "DB_PASSWORD_VALUE"},
        Actor(ActorType.USER, str(admin)),
    )
    body = _search(client)
    first = body["items"][0]
    assert first["action"] == "secret.reveal"
    assert first["actor"] == "admin"
    assert first["description"] == "Revealed the secret DB_PASSWORD_VALUE"
    assert first["details"] == {"name": "DB_PASSWORD_VALUE"}
    assert body["items"][-1]["action"] == "user.create"
    assert body["next_cursor"] is None


def test_token_and_deleted_actors_have_readable_names(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    created = client.post(
        "/api/tokens",
        json={"name": "ci", "scopes": ["read"]},
        headers=mutation_headers(client, csrf),
    ).json()
    token_id = str(created["info"]["id"])
    _record(
        container,
        "run.cancel",
        AuditTarget("run", "9"),
        {"number": 3},
        Actor(ActorType.TOKEN, token_id),
    )
    _record(
        container,
        "run.cancel",
        AuditTarget("run", "9"),
        {"number": 3},
        Actor(ActorType.USER, "999"),
    )
    items = _search(client)["items"]
    assert items[0]["actor"] == "user 999 (deleted)"
    assert items[1]["actor"] == "token ci"


def test_filter_by_actor_action_and_target(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    _record(container, "secret.create", AuditTarget("environment", "1"), {"name": "A"})
    _record(container, "secret.rotate", AuditTarget("environment", "2"), {"name": "B"})
    _record(container, "domain.create", AuditTarget("domain", "5"), {"hostname": "x.example.test"})
    assert _actions(_search(client, "?action=domain.create")) == ["domain.create"]
    assert sorted(_actions(_search(client, "?action=secret.*"))) == [
        "secret.create",
        "secret.rotate",
    ]
    only_env_two = _search(client, "?target_type=environment&target_id=2")
    assert _actions(only_env_two) == ["secret.rotate"]
    system = _search(client, "?actor_type=system")
    assert "auth.login" not in _actions(system)
    users = _search(client, "?actor_type=user")
    assert _actions(users) == ["auth.login"]
    assert _actions(
        _search(client, f"?actor_type=user&actor_id={users['items'][0]['actor_id']}")
    ) == ["auth.login"]
    assert client.get("/api/audit?actor_type=robot").status_code == 422


def test_text_search_covers_action_details_target_and_actor_name(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    _record(
        container, "domain.create", AuditTarget("domain", "5"), {"hostname": "shop.example.test"}
    )
    _record(container, "secret.create", AuditTarget("environment", "1"), {"name": "STRIPE_KEY"})
    assert _actions(_search(client, "?q=shop.example")) == ["domain.create"]
    assert _actions(_search(client, "?q=STRIPE")) == ["secret.create"]
    assert _actions(_search(client, "?q=secret.cr")) == ["secret.create"]
    assert _actions(_search(client, "?q=admin")) == [
        "auth.login",
        "user.create",
    ]  # the actor's username
    assert _actions(_search(client, "?q=%25")) == []  # wildcards are escaped, not interpreted
    assert client.get("/api/audit?q=" + "x" * 101).status_code == 422


def test_filter_by_date_range(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    start = clock.now
    clock.advance(days=2)
    _record(container, "secret.create", AuditTarget("environment", "1"), {"name": "LATE"})
    clock.now = start + timedelta(minutes=1)  # back inside the session before querying
    midpoint = (start + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    assert _actions(_search(client, f"?since={midpoint}")) == ["secret.create"]
    assert _actions(_search(client, f"?until={midpoint}")) == ["auth.login", "user.create"]
    inverted = client.get(
        f"/api/audit?since={midpoint}&until={start.isoformat().replace('+00:00', 'Z')}"
    )
    assert inverted.status_code == 422
    assert client.get("/api/audit?since=2026-01-01T00:00:00").status_code == 422  # needs a zone


def test_filter_by_project_covers_its_environments_runs_and_domains(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    projects = ProjectService(container.uow())
    alpha = projects.create("alpha", "Alpha", "octo/alpha")
    beta = projects.create("beta", "Beta", "octo/beta")
    env_alpha = alpha.environments[0].id
    env_beta = beta.environments[0].id
    uow = container.uow()
    with uow:
        run_alpha = Run(
            environment_id=env_alpha,
            number=1,
            trigger=RunTrigger.MANUAL,
            branch="development",
            commit_sha="a" * 40,
            pipeline_source="platform",
            pipeline_text="x",
            status=RunStatus.SUCCEEDED,
        )
        uow.session.add(run_alpha)
        domain = Domain(
            environment_id=env_alpha,
            hostname="a.example.test",
            service="web",
            container_port=80,
            published_port=8080,
        )
        uow.session.add(domain)
        uow.session.flush()
        run_id, domain_id = run_alpha.id, domain.id
    _record(container, "project.create", AuditTarget("project", "alpha"), {"slug": "alpha"})
    _record(container, "secret.create", AuditTarget("environment", str(env_alpha)), {"name": "A"})
    _record(container, "run.create", AuditTarget("run", str(run_id)), {"number": 1})
    _record(container, "domain.create", AuditTarget("domain", str(domain_id)), {"hostname": "a"})
    _record(container, "secret.create", AuditTarget("environment", str(env_beta)), {"name": "B"})
    _record(container, "run.create", AuditTarget("run", "99999"), {"number": 1})  # a stranger's id
    found = _search(client, "?project=alpha")
    assert sorted(_actions(found)) == [
        "domain.create",
        "project.create",
        "run.create",
        "secret.create",
    ]
    assert {item["target_id"] for item in found["items"]} >= {"alpha", str(env_alpha), str(run_id)}
    assert str(env_beta) not in {item["target_id"] for item in found["items"]}
    assert _search(client, "?project=nonexistent")["items"] == []


def test_cursor_pagination_walks_every_event_once(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    for number in range(120):
        clock.advance(seconds=1)
        _record(container, "run.cancel", AuditTarget("run", str(number)), {"number": number})
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        query = "?limit=50" + (f"&cursor={cursor}" if cursor else "")
        body = _search(client, query)
        seen.extend(item["id"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert len(seen) == 122  # user.create, the login and 120 cancellations
    assert len(set(seen)) == len(seen)
    assert seen == sorted(seen, reverse=True)


def test_new_events_do_not_shift_a_page_in_progress(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    for number in range(5):
        _record(container, "run.cancel", AuditTarget("run", str(number)), {"number": number})
    first = _search(client, "?limit=3")
    _record(container, "run.cancel", AuditTarget("run", "new"), {"number": 99})
    second = _search(client, f"?limit=3&cursor={first['next_cursor']}")
    ids = [item["id"] for item in first["items"] + second["items"]]
    assert len(set(ids)) == len(ids)


def test_invalid_cursor_and_limit(client: TestClient, csrf: str) -> None:
    assert client.get("/api/audit?cursor=garbage").status_code == 422
    assert client.get("/api/audit?limit=0").status_code == 422
    assert client.get("/api/audit?limit=201").status_code == 422


def test_actions_lists_known_actions(client: TestClient, csrf: str) -> None:
    actions = client.get("/api/audit/actions").json()["actions"]
    assert actions == known_actions()
    assert "auth.login" in actions
    assert "two_factor.enable" in actions


def test_every_known_action_describes_empty_details() -> None:
    for action in known_actions():
        assert describe_audit(action, {}), action
    assert describe_audit("unheard.of", {"x": 1}) == "unheard.of"


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_csv_export_is_streamed_with_header_and_safe_cells(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    _record(container, "domain.create", AuditTarget("domain", "5"), {"hostname": "=HYPERLINK(1)"})
    response = client.get("/api/audit/export.csv?action=domain.*")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="jendock-audit-' in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    rows = _parse_csv(response.text)
    assert tuple(rows[0]) == audit_log.CSV_HEADER
    assert len(rows) == 2
    assert rows[1][5] == "domain.create"
    assert rows[1][6] == "Added the domain =HYPERLINK(1)"
    assert '"hostname": "=HYPERLINK(1)"' in rows[1][9]
    assert all(not cell.startswith("=") for cell in rows[1])


def test_csv_export_applies_filters_and_validates_them(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    _record(container, "secret.create", AuditTarget("environment", "1"), {"name": "A"})
    rows = _parse_csv(client.get("/api/audit/export.csv?action=secret.create").text)
    assert [row[5] for row in rows[1:]] == ["secret.create"]
    bad = client.get("/api/audit/export.csv?since=2026-10-02T00:00:00Z&until=2026-10-01T00:00:00Z")
    assert bad.status_code == 422  # reported as a normal error, before any streaming


def test_csv_export_is_bounded_and_batched(
    container: AppContainer,
    client: TestClient,
    csrf: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for number in range(25):
        _record(container, "run.cancel", AuditTarget("run", str(number)), {"number": number})
    monkeypatch.setattr(audit_log, "EXPORT_MAX_ROWS", 12)
    monkeypatch.setattr(audit_log, "EXPORT_BATCH", 5)
    rows = _parse_csv(client.get("/api/audit/export.csv").text)
    assert len(rows) == 1 + 12
    ids = [int(row[0]) for row in rows[1:]]
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 12


def test_details_never_contain_secret_values(
    container: AppContainer, client: TestClient, csrf: str
) -> None:

    with pytest.raises(ValidationError):
        _record(
            container, "secret.create", AuditTarget("environment", "1"), {"password": "hunter2"}
        )
    _record(container, "secret.reveal", AuditTarget("environment", "1"), {"name": "DB_URL"})
    everything = client.get("/api/audit?limit=200").text + client.get("/api/audit/export.csv").text
    assert "hunter2" not in everything


def test_environment_name_enum_values_serialize(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    _record(
        container,
        "environment.update",
        AuditTarget("environment", "1"),
        {
            "project": "demo",
            "environment": EnvironmentName.PRODUCTION.value,
            "auto_deploy": {"from": False, "to": True},
        },
    )
    item = _search(client)["items"][0]
    assert item["description"] == "Changed the settings of demo / production; auto-deploy on"
