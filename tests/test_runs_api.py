"""Run endpoints: detail, step logs, event stream and cancellation."""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.api.container import AppContainer
from backend.domain.actor import Actor
from backend.domain.enums import RunStatus
from backend.repositories.models import Run
from backend.services.audit import AuditService
from tests.api_support import FakeClock, create_user, login, make_client, mutation_headers
from tests.execution_support import Harness
from tests.test_pipeline_runner import pipeline, stage

LOG_LINE = "hello from the container"


@pytest.fixture
def fast_container(container: AppContainer) -> AppContainer:
    settings = container.settings.model_copy(
        update={"sse_poll_interval": 0.01, "sse_heartbeat_interval": 0.05}
    )
    return replace(container, settings=settings)


@pytest.fixture
def session(fast_container: AppContainer, admin: int) -> tuple[TestClient, str]:
    client = make_client(fast_container)
    return client, login(client)


@pytest.fixture
def finished(harness: Harness) -> Run:
    harness.runtime.when("greet", lines=(LOG_LINE, "second line"))
    run, outcome = harness.run(pipeline(stage("Build", "sh 'greet'\necho 'done'")))
    assert outcome.status is RunStatus.SUCCEEDED
    return run


def _bearer(client: TestClient, csrf: str, scopes: list[str]) -> dict[str, str]:
    body = {"name": "agent", "scopes": scopes}
    created = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
    return {"Authorization": f"Bearer {created.json()['token']}"}


def _events(client: TestClient, path: str) -> list[tuple[str, str]]:
    """Collect ``(event, data)`` pairs until the stream ends."""
    events: list[tuple[str, str]] = []
    name = ""
    with client.stream("GET", path) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, line.removeprefix("data: ")))
            elif line.startswith(": "):
                events.append(("comment", line.removeprefix(": ")))
    return events


# --- detail ----------------------------------------------------------------------------------


def test_run_detail_has_stages_and_steps(session: tuple[TestClient, str], finished: Run) -> None:
    client, _ = session
    body = client.get(f"/api/runs/{finished.id}").json()
    assert body["id"] == finished.id
    assert body["number"] == 1
    assert body["project"] == "demo"
    assert body["environment"] == "development"
    assert body["status"] == "succeeded"
    assert body["message"] is None
    assert [s["name"] for s in body["stages"]] == ["Checkout", "Build"]
    build = body["stages"][1]
    assert build["status"] == "succeeded"
    assert [(s["step_type"], s["status"], s["label"]) for s in build["steps"]] == [
        ("sh", "succeeded", "sh: greet"),
        ("echo", "succeeded", "echo: done"),
    ]
    assert build["steps"][0]["exit_code"] == 0


def test_unknown_run_is_404(session: tuple[TestClient, str]) -> None:
    client, _ = session
    assert client.get("/api/runs/999").status_code == 404
    assert client.get("/api/runs/999/events").status_code == 404
    assert client.get("/api/runs/999/steps/1/log").status_code == 404


def test_requires_authentication_and_read_scope(
    session: tuple[TestClient, str], finished: Run
) -> None:
    client, csrf = session
    bare = TestClient(client.app, base_url=str(client.base_url))
    assert bare.get(f"/api/runs/{finished.id}").status_code == 401
    assert bare.get(f"/api/runs/{finished.id}/events").status_code == 401
    only_run = _bearer(client, csrf, ["run"])
    assert bare.get(f"/api/runs/{finished.id}", headers=only_run).status_code == 403
    reader = _bearer(client, csrf, ["read"])
    assert bare.get(f"/api/runs/{finished.id}", headers=reader).status_code == 200


# --- log chunks ------------------------------------------------------------------------------


def _first_step(harness: Harness, run: Run) -> int:
    return harness.detail(run).stages[1].steps[0].id


def test_log_chunk_is_bounded_and_resumable(
    session: tuple[TestClient, str], harness: Harness, finished: Run
) -> None:
    client, _ = session
    step = _first_step(harness, finished)
    path = f"/api/runs/{finished.id}/steps/{step}/log"
    first = client.get(path).json()
    assert first["offset"] == 0
    assert first["text"] == (
        f"pulling image alpine:3.20\nsha256:fake\nrunning in alpine:3.20\n{LOG_LINE}\nsecond line\n"
    )
    assert first["complete"] is True
    assert first["next_offset"] == first["size"]
    again = client.get(path, params={"offset": first["next_offset"]}).json()
    assert again["text"] == ""
    assert again["complete"] is True
    middle = client.get(path, params={"offset": 5}).json()
    assert middle["offset"] == 5
    assert middle["text"] == first["text"][5:]


def test_log_of_another_runs_step_is_404(
    session: tuple[TestClient, str], harness: Harness, finished: Run
) -> None:
    client, _ = session
    other, _ = harness.run(pipeline(stage("Build", "sh 'x'")))
    step = _first_step(harness, finished)
    assert client.get(f"/api/runs/{other.id}/steps/{step}/log").status_code == 404
    assert client.get(f"/api/runs/{finished.id}/steps/{step}/log?offset=-1").status_code == 422


# --- events (SSE) ----------------------------------------------------------------------------


def test_event_stream_replays_a_finished_run_and_ends(
    session: tuple[TestClient, str], harness: Harness, finished: Run
) -> None:
    client, _ = session
    events = _events(client, f"/api/runs/{finished.id}/events")
    names = [name for name, _ in events]
    assert names[0] == "status"
    assert names[-1] == "end"
    assert json.loads(events[-1][1]) == {"status": "succeeded"}
    status = json.loads(events[0][1])
    assert status["status"] == "succeeded"
    assert [s["name"] for s in status["stages"]] == ["Checkout", "Build"]
    logs = [json.loads(data) for name, data in events if name == "log"]
    step = _first_step(harness, finished)
    text = "".join(item["text"] for item in logs if item["step_id"] == step)
    assert LOG_LINE in text
    assert "second line" in text


def test_event_stream_follows_a_live_run_with_heartbeats(
    session: tuple[TestClient, str], fast_container: AppContainer, harness: Harness
) -> None:
    # The test client returns a response only when the stream ends, so the run is cancelled
    # from a timer while the stream is open.
    client, _ = session
    run = harness.queue(pipeline(stage("S", "sh 'x'")))
    cancel = threading.Timer(
        0.4, lambda: fast_container.run_lifecycle().request_cancel(run.id, Actor.system("test"))
    )
    cancel.start()
    with client.stream("GET", f"/api/runs/{run.id}/events") as response:
        lines = list(response.iter_lines())
    cancel.join()
    text = "\n".join(lines)
    assert text.startswith("event: status")
    assert '"status":"queued"' in text
    assert ": heartbeat" in text
    assert text.index('"status":"queued"') < text.index(": heartbeat")
    assert '"status":"cancelled"' in text  # the status change was pushed
    assert lines[-3:] == ["event: end", 'data: {"status": "cancelled"}', ""]


def test_event_stream_does_not_extend_the_session(
    session: tuple[TestClient, str],
    fast_container: AppContainer,
    clock: FakeClock,
    finished: Run,
) -> None:
    client, _ = session
    clock.advance(minutes=40)
    assert _events(client, f"/api/runs/{finished.id}/events")[-1][0] == "end"
    clock.advance(minutes=30)  # 70 minutes after login: idle timeout (60) not extended
    assert client.get(f"/api/runs/{finished.id}").status_code == 401


def test_a_normal_request_does_extend_the_session(
    session: tuple[TestClient, str], clock: FakeClock, finished: Run
) -> None:
    client, _ = session
    clock.advance(minutes=40)
    assert client.get(f"/api/runs/{finished.id}").status_code == 200
    clock.advance(minutes=30)
    assert client.get(f"/api/runs/{finished.id}").status_code == 200


def test_stream_ends_when_the_session_expires(
    session: tuple[TestClient, str], clock: FakeClock, harness: Harness
) -> None:
    client, _ = session
    run = harness.queue(pipeline(stage("S", "sh 'x'")))
    expire = threading.Timer(0.3, lambda: clock.advance(hours=2))
    expire.start()
    with client.stream("GET", f"/api/runs/{run.id}/events") as response:
        lines = list(response.iter_lines())
    expire.join()
    assert any(line.startswith("event: status") for line in lines)
    assert "event: end" not in lines  # closed at a heartbeat, the run had not finished


# --- cancel ----------------------------------------------------------------------------------


def test_cancel_queued_run(
    session: tuple[TestClient, str], fast_container: AppContainer, harness: Harness
) -> None:
    client, csrf = session
    run = harness.queue(pipeline(stage("S", "sh 'x'")))
    response = client.post(f"/api/runs/{run.id}/cancel", headers=mutation_headers(client, csrf))
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "cancelled"
    assert body["run"]["status"] == "cancelled"
    assert body["run"]["message"] == "cancelled before it started"
    events = AuditService(fast_container.uow()).list(action="run.cancel")
    assert [(e.actor_type.value, e.target_id) for e in events] == [("user", str(run.id))]


def test_cancel_running_run_only_sets_the_request(
    session: tuple[TestClient, str], harness: Harness
) -> None:
    client, csrf = session
    run = harness.queue(pipeline(stage("S", "sh 'x'")))
    assert harness.env.lifecycle().claim_next() is not None
    body = client.post(f"/api/runs/{run.id}/cancel", headers=mutation_headers(client, csrf)).json()
    assert body["outcome"] == "requested"
    assert body["run"]["status"] == "running"  # the worker finishes the cancellation
    assert body["run"]["cancel_requested_at"] is not None


def test_cancel_finished_run_conflicts(session: tuple[TestClient, str], finished: Run) -> None:
    client, csrf = session
    response = client.post(
        f"/api/runs/{finished.id}/cancel", headers=mutation_headers(client, csrf)
    )
    assert response.status_code == 409
    assert (
        client.post("/api/runs/999/cancel", headers=mutation_headers(client, csrf)).status_code
        == 404
    )


def test_cancel_needs_csrf_origin_and_the_run_scope(
    session: tuple[TestClient, str], harness: Harness
) -> None:
    client, csrf = session
    run = harness.queue(pipeline(stage("S", "sh 'x'")))
    url = f"/api/runs/{run.id}/cancel"
    assert client.post(url).status_code == 403  # no Origin, no CSRF token
    wrong_token = {"Origin": mutation_headers(client, csrf)["Origin"], "X-CSRF-Token": "nope"}
    assert client.post(url, headers=wrong_token).status_code == 403
    assert (
        client.post(url, headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf}).status_code
        == 403
    )
    bare = TestClient(client.app, base_url=str(client.base_url))
    reader = _bearer(client, csrf, ["read"])
    assert bare.post(url, headers=reader).status_code == 403  # read scope is not enough
    assert harness.detail(run).run.status is RunStatus.QUEUED
    runner = _bearer(client, csrf, ["run"])
    assert bare.post(url, headers=runner).status_code == 200  # tokens need no CSRF
    audit = AuditService(harness.uow()).list(action="run.cancel")
    assert audit[0].actor_type.value == "token"


def test_create_user_helper_still_works(container: AppContainer) -> None:
    assert create_user(container, "other") > 0
