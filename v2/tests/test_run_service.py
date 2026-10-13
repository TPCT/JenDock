import pytest
from sqlalchemy.exc import IntegrityError

from shiftcodes.domain.enums import EnvironmentName, PipelineSource, RunStatus, RunTrigger
from shiftcodes.domain.errors import NotFoundError, ValidationError
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Run, StageRun, StepRun
from shiftcodes.services.environments import EnvironmentService
from shiftcodes.services.runs import RunService
from tests.conftest import DEV, PROD

SHA = "a" * 40


def _queue(service: RunService, slug: str, env_name: EnvironmentName = DEV, sha: str = SHA) -> Run:
    return service.create_queued(
        slug,
        env_name,
        trigger=RunTrigger.MANUAL,
        branch="development",
        commit_sha=sha,
        pipeline_source=PipelineSource.PLATFORM,
        pipeline_text="pipeline {}",
    )


def test_numbers_are_sequential_per_environment(uow: UnitOfWork, project: str) -> None:
    service = RunService(uow)
    numbers = [_queue(service, project).number for _ in range(3)]
    assert numbers == [1, 2, 3]
    assert _queue(service, project, PROD).number == 1
    run = _queue(service, project)
    assert run.number == 4
    assert run.status is RunStatus.QUEUED
    assert run.pipeline_text == "pipeline {}"


def test_invalid_input_and_missing_environment(uow: UnitOfWork, project: str) -> None:
    service = RunService(uow)
    with pytest.raises(ValidationError):
        _queue(service, project, sha="xyz")
    with pytest.raises(NotFoundError):
        _queue(service, "missing")


def test_duplicate_number_rejected_by_database(uow: UnitOfWork, project: str) -> None:
    run = _queue(RunService(uow), project)
    with pytest.raises(IntegrityError), uow:
        uow.runs.add(
            Run(
                environment_id=run.environment_id,
                number=run.number,
                trigger=RunTrigger.PUSH,
                branch="b",
                commit_sha=SHA,
                pipeline_source=PipelineSource.REPOSITORY,
                pipeline_text="t",
            )
        )


def test_stage_step_and_queries(uow: UnitOfWork, project: str) -> None:
    service = RunService(uow)
    run = _queue(service, project)
    _queue(service, project)
    with uow:
        stage = uow.runs.add_stage(StageRun(run_id=run.id, position=0, name="Build"))
        uow.runs.add_step(
            StepRun(stage_run_id=stage.id, position=0, step_type="sh", label="make", exit_code=0)
        )
    env = EnvironmentService(uow).get(project, DEV)
    with uow:
        assert [r.number for r in uow.runs.list_for_environment(env.id)] == [2, 1]
        assert [r.number for r in uow.runs.list_for_environment(env.id, limit=1, offset=1)] == [1]
        assert len(uow.runs.list_by_status(RunStatus.QUEUED)) == 2
        assert [s.name for s in uow.runs.list_stages(run.id)] == ["Build"]
        steps = uow.runs.list_steps(stage.id)
        assert [(s.label, s.status) for s in steps] == [("make", RunStatus.QUEUED)]
