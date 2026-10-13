"""Commit status reporting: the GitHub request, the state mapping and "never fails a run"."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace

import pytest

from backend.adapters.commit_status import (
    CommitState,
    CommitStatus,
    GithubStatusPublisher,
    UrllibPoster,
)
from backend.adapters.errors import AdapterError
from backend.domain.enums import RunStatus
from backend.domain.errors import NotFoundError, ValidationError
from backend.execution.context import RunJob
from backend.execution.status_reporter import CommitStatusReporter, NoStatusReporter
from backend.services.audit import AuditService
from backend.services.secrets import SecretService
from tests.execution_support import SHA, Harness
from tests.test_pipeline_runner import pipeline, stage

TOKEN = "ghp_super_secret_token_value"
SECRET_NAME = "GITHUB_STATUS_TOKEN"


class FakePoster:
    """Records the requests; answers with a scripted status or raises."""

    def __init__(self, status: int = 201, error: AdapterError | None = None) -> None:
        self.status = status
        self.error = error
        self.requests: list[tuple[str, Mapping[str, str], dict[str, str]]] = []

    def post_json(self, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> int:
        self.requests.append((url, dict(headers), json.loads(body)))
        if self.error:
            raise self.error
        return self.status


def _status(**changes: object) -> CommitStatus:
    base = CommitStatus("octo/demo", SHA, CommitState.SUCCESS, "Run #1 succeeded", "jendock/dev")
    return replace(base, **changes)  # type: ignore[arg-type]


def _job(**changes: object) -> RunJob:
    base = RunJob(
        run_id=42,
        number=3,
        project="demo",
        environment="development",
        environment_id=1,
        branch="development",
        commit_sha=SHA,
        repository="octo/demo",
        compose_project=None,
        compose_files=(),
        services=(),
        pipeline_text="",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# --- the request -----------------------------------------------------------------------------


def test_publisher_posts_to_the_statuses_endpoint() -> None:
    poster = FakePoster()
    GithubStatusPublisher(poster).publish(_status(target_url="https://ci.example/runs/9"), TOKEN)
    [(url, headers, body)] = poster.requests
    assert url == f"https://api.github.com/repos/octo/demo/statuses/{SHA}"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["Content-Type"] == "application/json"
    assert body == {
        "state": "success",
        "description": "Run #1 succeeded",
        "context": "jendock/dev",
        "target_url": "https://ci.example/runs/9",
    }
    assert TOKEN not in url
    assert TOKEN not in json.dumps(body)


def test_publisher_clips_the_description_and_omits_an_empty_target() -> None:
    poster = FakePoster()
    GithubStatusPublisher(poster).publish(_status(description="x" * 500), TOKEN)
    body = poster.requests[0][2]
    assert len(body["description"]) == 140
    assert "target_url" not in body


@pytest.mark.parametrize("code", [200, 401, 403, 404, 422, 500])
def test_anything_but_201_is_an_error_without_the_token(code: int) -> None:
    with pytest.raises(AdapterError) as caught:
        GithubStatusPublisher(FakePoster(code)).publish(_status(), TOKEN)
    assert f"HTTP {code}" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_publisher_validates_what_goes_into_the_url() -> None:
    poster = FakePoster()
    with pytest.raises(ValidationError):
        GithubStatusPublisher(poster).publish(_status(repository="../../evil"), TOKEN)
    with pytest.raises(ValidationError):
        GithubStatusPublisher(poster).publish(_status(commit_sha="abc"), TOKEN)
    assert poster.requests == []


def test_the_real_poster_refuses_plain_http() -> None:
    with pytest.raises(AdapterError, match="only sent over https"):
        UrllibPoster().post_json("http://api.github.com/x", {}, b"{}", 1.0)


# --- the reporter ----------------------------------------------------------------------------


class _Secrets:
    """A ``SecretService`` stand-in that resolves from a dict (like the real one, it raises
    ``NotFoundError`` for a name that is not stored)."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve_for_run(
        self, environment_id: int, names: list[str], run_id: int | None = None
    ) -> dict[str, str]:
        missing = [n for n in names if n not in self.values]
        if missing:
            raise NotFoundError(f"secrets not found: {', '.join(missing)}")
        return {n: self.values[n] for n in names}


def _reporter(poster: FakePoster, values: dict[str, str] | None = None) -> CommitStatusReporter:
    secrets = _Secrets({SECRET_NAME: TOKEN} if values is None else values)
    factory: Callable[[], SecretService] = lambda: secrets  # type: ignore[assignment,return-value]  # noqa: E731
    return CommitStatusReporter(
        GithubStatusPublisher(poster), factory, SECRET_NAME, "https://ci.example.test/"
    )


def test_started_publishes_pending_with_a_link_to_the_run() -> None:
    poster = FakePoster()
    _reporter(poster).started(_job())
    [(url, headers, body)] = poster.requests
    assert url.endswith(f"/statuses/{SHA}")
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert body["state"] == "pending"
    assert body["context"] == "jendock/development"
    assert body["target_url"] == "https://ci.example.test/runs/42"
    assert body["description"] == "Run #3 is running"


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (RunStatus.SUCCEEDED, "success"),
        (RunStatus.FAILED, "failure"),
        (RunStatus.CANCELLED, "error"),
        (RunStatus.INTERRUPTED, "error"),
    ],
)
def test_final_states(status: RunStatus, state: str) -> None:
    poster = FakePoster()
    _reporter(poster).finished(_job(), status, "stage Build failed")
    body = poster.requests[0][2]
    assert body["state"] == state
    assert body["description"].startswith(f"Run #3 {status.value}")
    assert ("stage Build failed" in body["description"]) is (status is not RunStatus.SUCCEEDED)


def test_a_missing_token_secret_skips_quietly(caplog: pytest.LogCaptureFixture) -> None:
    poster = FakePoster()
    with caplog.at_level(logging.INFO):
        _reporter(poster, {}).started(_job())
    assert poster.requests == []
    assert "skipped" in caplog.text


@pytest.mark.parametrize(
    "poster",
    [
        FakePoster(403),
        FakePoster(error=AdapterError(f"boom with {TOKEN} inside")),
    ],
)
def test_failures_are_logged_without_the_token_and_never_raised(
    poster: FakePoster, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _reporter(poster).started(_job())
        _reporter(poster).finished(_job(), RunStatus.FAILED, "x")
    assert "commit status failed" in caplog.text
    assert TOKEN not in caplog.text
    assert {getattr(r, "error", None) for r in caplog.records} == {"AdapterError"}


def test_the_null_reporter_does_nothing() -> None:
    reporter = NoStatusReporter()
    reporter.started(_job())
    reporter.finished(_job(), RunStatus.FAILED, "x")


# --- inside a run ----------------------------------------------------------------------------


def _with_status(harness: Harness, poster: FakePoster) -> Harness:
    reporter = CommitStatusReporter(
        GithubStatusPublisher(poster),
        harness.env.secret_service,
        SECRET_NAME,
        "https://ci.example.test",
    )
    return replace(harness, env=replace(harness.env, status=reporter))


def test_a_run_reports_pending_then_the_final_state(harness: Harness) -> None:
    harness.add_secret(SECRET_NAME, TOKEN)
    poster = FakePoster()
    local = _with_status(harness, poster)
    run, outcome = local.run(pipeline(stage("A", "sh 'echo hi'")))
    assert outcome.status is RunStatus.SUCCEEDED
    assert [body["state"] for _, _, body in poster.requests] == ["pending", "success"]
    assert all(url.endswith(f"/statuses/{SHA}") for url, _, _ in poster.requests)
    assert poster.requests[1][2]["target_url"] == f"https://ci.example.test/runs/{run.id}"
    assert TOKEN not in local.all_logs(run)


def test_a_failing_run_reports_failure(harness: Harness) -> None:
    harness.add_secret(SECRET_NAME, TOKEN)
    harness.runtime.when("boom", exit_code=2)
    poster = FakePoster()
    local = _with_status(harness, poster)
    _, outcome = local.run(pipeline(stage("A", "sh 'boom'")))
    assert outcome.status is RunStatus.FAILED
    assert [body["state"] for _, _, body in poster.requests] == ["pending", "failure"]


def test_github_failures_never_fail_the_run(harness: Harness) -> None:
    harness.add_secret(SECRET_NAME, TOKEN)
    poster = FakePoster(error=AdapterError("no response from GitHub: URLError"))
    local = _with_status(harness, poster)
    run, outcome = local.run(pipeline(stage("A", "sh 'echo hi'")))
    assert outcome.status is RunStatus.SUCCEEDED
    assert local.detail(run).run.status is RunStatus.SUCCEEDED
    assert len(poster.requests) == 2  # both were attempted


def test_a_crashing_reporter_cannot_fail_the_run(harness: Harness) -> None:
    class Exploding:
        def started(self, job: RunJob) -> None:
            raise RuntimeError("started")

        def finished(self, job: RunJob, status: RunStatus, message: str | None) -> None:
            raise RuntimeError("finished")

    local = replace(harness, env=replace(harness.env, status=Exploding()))
    _, outcome = local.run(pipeline(stage("A", "sh 'echo hi'")))
    assert outcome.status is RunStatus.SUCCEEDED


def test_reporting_is_off_by_default_and_the_token_is_audited_when_resolved(
    harness: Harness,
) -> None:
    _, outcome = harness.run(pipeline(stage("A", "sh 'echo hi'")))
    assert outcome.status is RunStatus.SUCCEEDED
    assert AuditService(harness.uow()).list(action="secret.resolve") == []
    harness.add_secret(SECRET_NAME, TOKEN)
    _with_status(harness, FakePoster()).run(pipeline(stage("A", "sh 'echo hi'")))
    resolves = AuditService(harness.uow()).list(action="secret.resolve")
    assert resolves
    assert TOKEN not in repr([e.details for e in resolves])
