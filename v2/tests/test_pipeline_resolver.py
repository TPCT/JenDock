"""Which pipeline a run executes: the repository's Jenkinsfile, the stored one, or none."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.sources import CheckoutRequest
from shiftcodes.domain.enums import GitTransport, PipelineSource, RunStatus
from shiftcodes.execution.context import RunJob
from shiftcodes.execution.pipeline_resolver import (
    JENKINSFILE_PATH,
    PipelineResolver,
    PipelineUnavailableError,
)
from shiftcodes.repositories.models import Run
from shiftcodes.services.pipelines import PipelineService
from shiftcodes.services.secret_cipher import SecretCipher
from tests.conftest import DEV
from tests.execution_support import SHA, FakeSource, Harness, make_harness
from tests.test_pipeline_runner import pipeline, stage

REPO_PIPELINE = pipeline(stage("From repo", "sh 'echo repo'"))
STORED_V1 = pipeline(stage("Stored one", "sh 'echo one'"))
STORED_V2 = pipeline(stage("Stored two", "sh 'echo two'"))


def _store(harness: Harness, text: str) -> None:
    PipelineService(harness.uow()).save(harness.project, DEV, text)


def _row(harness: Harness, run: Run) -> Run:
    with harness.uow() as uow:
        row = uow.runs.get(run.id)
        assert row is not None
        return row


def _queue_provisional(harness: Harness, text: str = "") -> Run:
    """Queue as a webhook or manual trigger does: the pipeline is chosen when the run starts."""
    return harness.queue(text)


# --- precedence ------------------------------------------------------------------------------


def test_the_repository_jenkinsfile_wins_over_the_stored_pipeline(harness: Harness) -> None:
    _store(harness, STORED_V1)
    harness.source.files["Jenkinsfile"] = REPO_PIPELINE
    run = _queue_provisional(harness, STORED_V1)
    outcome = harness.execute(run)
    assert outcome.status is RunStatus.SUCCEEDED
    assert harness.runtime.script_order == ["echo repo"]
    row = _row(harness, run)
    assert row.pipeline_source is PipelineSource.REPOSITORY
    assert row.pipeline_text == REPO_PIPELINE  # the exact text is snapshotted
    assert "From repo" in harness.stage_statuses(run)
    assert "pipeline source: repository" in harness.log_of(run, "Checkout")


def test_without_a_jenkinsfile_the_latest_stored_pipeline_is_used(harness: Harness) -> None:
    _store(harness, STORED_V1)
    run = _queue_provisional(harness, STORED_V1)
    _store(harness, STORED_V2)  # saved after the run was queued: the latest at start wins
    outcome = harness.execute(run)
    assert outcome.status is RunStatus.SUCCEEDED
    assert harness.runtime.script_order == ["echo two"]
    row = _row(harness, run)
    assert row.pipeline_source is PipelineSource.PLATFORM
    assert row.pipeline_text == STORED_V2
    assert "pipeline source: platform" in harness.log_of(run, "Checkout")


def test_the_jenkinsfile_is_looked_up_at_the_pinned_commit_and_root(harness: Harness) -> None:
    _store(harness, STORED_V1)
    run = _queue_provisional(harness, STORED_V1)
    harness.execute(run)
    assert harness.source.reads == [JENKINSFILE_PATH]
    assert harness.source.requests[0].commit_sha == SHA


def test_a_queued_snapshot_is_the_fallback_when_nothing_is_stored(harness: Harness) -> None:
    run = harness.queue(STORED_V1)  # e.g. created by a test or an older version of the platform
    outcome = harness.execute(run)
    assert outcome.status is RunStatus.SUCCEEDED
    assert _row(harness, run).pipeline_source is PipelineSource.PLATFORM


# --- nothing to run --------------------------------------------------------------------------


def test_with_neither_the_run_fails_with_a_clear_message(harness: Harness) -> None:
    run = _queue_provisional(harness, "")
    outcome = harness.execute(run)
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert "no Jenkinsfile in the repository" in outcome.message
    assert "no pipeline is stored for demo/development" in outcome.message
    assert harness.runtime.runs == []
    assert [d.stage.name for d in harness.detail(run).stages] == ["Pipeline"]
    assert "no pipeline is stored" in harness.log_of(run, "Pipeline")
    assert _row(harness, run).status is RunStatus.FAILED


def test_an_unreadable_repository_fails_the_run_instead_of_using_the_stored_pipeline(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str
) -> None:
    class Unreadable(FakeSource):
        def read_file(self, request: CheckoutRequest, path: str) -> str | None:
            raise SourceError("git fetch failed: could not read from remote")

    local = make_harness(tmp_path, engine, cipher, project, source=Unreadable())
    _store(local, STORED_V1)
    run = _queue_provisional(local, STORED_V1)
    outcome = local.execute(run)
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert "could not read the Jenkinsfile" in outcome.message
    assert "could not read from remote" in local.log_of(run, "Pipeline")
    assert local.runtime.runs == []


# --- validation ------------------------------------------------------------------------------


def test_a_syntax_error_in_the_repository_jenkinsfile_goes_to_the_run_log(harness: Harness) -> None:
    harness.source.files["Jenkinsfile"] = "pipeline { nonsense }"
    run = _queue_provisional(harness, STORED_V1)
    outcome = harness.execute(run)
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert "pipeline syntax error" in outcome.message
    assert "pipeline syntax error" in harness.log_of(run, "Pipeline")
    row = _row(harness, run)
    assert row.pipeline_source is PipelineSource.REPOSITORY  # what was attempted is on record
    assert row.pipeline_text == "pipeline { nonsense }"
    assert harness.runtime.runs == []


def test_every_diagnostic_is_logged_with_its_position(harness: Harness) -> None:
    text = pipeline(stage("A", "sh 'x'\nunknownStep 'y'") + stage("B", 'echo "hi ${NOPE}"'))
    harness.source.files["Jenkinsfile"] = text
    run = _queue_provisional(harness, "")
    outcome = harness.execute(run)
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert outcome.message.startswith("pipeline is invalid (line ")
    log = harness.log_of(run, "Pipeline")
    lines = [line for line in log.splitlines() if line]
    assert len(lines) >= 2
    assert all(line.startswith(("error: line ", "warning: line ")) for line in lines)
    assert any("unknownStep" in line or "unknown step" in line for line in lines)
    assert any("NOPE" in line for line in lines)


def test_a_valid_repository_pipeline_may_use_the_environments_secrets(harness: Harness) -> None:
    harness.add_secret("TOKEN", "secret-value-123")
    harness.source.files["Jenkinsfile"] = pipeline(
        stage("A", "sh 'echo $T'"), head="environment {\nT = credentials('TOKEN')\n}"
    )
    run = _queue_provisional(harness, "")
    assert harness.execute(run).status is RunStatus.SUCCEEDED


# --- the resolver alone ----------------------------------------------------------------------


def _job(text: str = "") -> RunJob:
    return RunJob(
        run_id=1,
        number=1,
        project="demo",
        environment="development",
        environment_id=7,
        branch="development",
        commit_sha=SHA,
        repository="octo/demo",
        compose_project=None,
        compose_files=(),
        services=(),
        pipeline_text=text,
        git_transport=GitTransport.SSH,
    )


def test_the_resolver_passes_the_projects_transport_to_the_source() -> None:
    source = FakeSource({"Jenkinsfile": "text"})
    requests: list[CheckoutRequest] = []
    original = source.read_file

    def spy(request: CheckoutRequest, path: str) -> str | None:
        requests.append(request)
        return original(request, path)

    source.read_file = spy  # type: ignore[method-assign]
    resolved = PipelineResolver(source, lambda _id: None).resolve(_job())
    assert (resolved.source, resolved.text) == (PipelineSource.REPOSITORY, "text")
    assert requests == [CheckoutRequest("octo/demo", SHA, GitTransport.SSH)]


def test_the_resolver_asks_for_the_stored_pipeline_of_the_environment() -> None:
    asked: list[int] = []

    def stored(environment_id: int) -> str | None:
        asked.append(environment_id)
        return "stored text"

    resolved = PipelineResolver(FakeSource({}), stored).resolve(_job())
    assert (resolved.source, resolved.text) == (PipelineSource.PLATFORM, "stored text")
    assert asked == [7]


def test_the_resolver_raises_when_nothing_is_available() -> None:
    try:
        PipelineResolver(FakeSource({}), lambda _id: None).resolve(_job("   \n"))
    except PipelineUnavailableError as error:
        assert "add a Jenkinsfile to the repository" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected PipelineUnavailableError")
