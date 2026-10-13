import pytest

from backend.domain.errors import NotFoundError, ValidationError
from backend.repositories.database import UnitOfWork
from backend.services.environments import EnvironmentService, EnvironmentUpdate
from backend.services.pipelines import PipelineService
from tests.conftest import DEV, PROD


def test_get_and_missing(uow: UnitOfWork, project: str) -> None:
    assert EnvironmentService(uow).get(project, DEV).branch == "development"
    with pytest.raises(NotFoundError):
        EnvironmentService(uow).get("missing", DEV)


def test_update_all_fields(uow: UnitOfWork, project: str) -> None:
    service = EnvironmentService(uow)
    before = service.get(project, PROD).updated_at
    change = EnvironmentUpdate(
        branch="main",
        auto_deploy=True,
        compose_project="demo-prod",
        compose_files=["/srv/demo/compose.yml"],
        services=["web", "worker"],
    )
    env = service.update(project, PROD, change)
    assert (env.branch, env.auto_deploy, env.compose_project) == ("main", True, "demo-prod")
    assert env.compose_files == ["/srv/demo/compose.yml"]
    assert env.services == ["web", "worker"]
    assert env.updated_at >= before
    assert service.get(project, DEV).branch == "development"


def test_compose_env_files_are_stored_and_validated(uow: UnitOfWork, project: str) -> None:
    service = EnvironmentService(uow)
    assert service.get(project, DEV).compose_env_files == []
    change = EnvironmentUpdate(compose_env_files=["/opt/demo/ports.env"])
    assert service.update(project, DEV, change).compose_env_files == ["/opt/demo/ports.env"]
    with pytest.raises(ValidationError):
        service.update(project, DEV, EnvironmentUpdate(compose_env_files=["ports.env"]))
    assert service.get(project, DEV).compose_env_files == ["/opt/demo/ports.env"]


def test_partial_update_and_clear_compose_project(uow: UnitOfWork, project: str) -> None:
    service = EnvironmentService(uow)
    service.update(project, DEV, EnvironmentUpdate(compose_project="x", auto_deploy=True))
    env = service.update(project, DEV, EnvironmentUpdate(branch="dev2"))
    assert (env.branch, env.compose_project, env.auto_deploy) == ("dev2", "x", True)
    assert (
        service.update(project, DEV, EnvironmentUpdate(clear_compose_project=True)).compose_project
        is None
    )


@pytest.mark.parametrize(
    "change",
    [
        EnvironmentUpdate(branch="a..b"),
        EnvironmentUpdate(compose_files=["relative.yml"]),
        EnvironmentUpdate(services=["-bad"]),
        EnvironmentUpdate(services=["a", "a"]),
        EnvironmentUpdate(compose_project="Bad Name"),
    ],
)
def test_update_validation_changes_nothing(
    uow: UnitOfWork, project: str, change: EnvironmentUpdate
) -> None:
    service = EnvironmentService(uow)
    with pytest.raises(ValidationError):
        service.update(project, DEV, change)
    env = service.get(project, DEV)
    assert (env.branch, env.compose_files, env.services) == ("development", [], [])


def _pipeline(stage: str) -> str:
    return (
        "pipeline {\n    agent any\n    stages {\n"
        f"        stage('{stage}') {{\n            steps {{\n                echo '{stage}'\n"
        "            }\n        }\n    }\n}\n"
    )


def test_pipeline_versions_are_append_only(uow: UnitOfWork, project: str) -> None:
    service = PipelineService(uow)
    with pytest.raises(NotFoundError):
        service.get_active(project, DEV)
    first = service.save(project, DEV, _pipeline("v1"))
    second = service.save(project, DEV, _pipeline("v2"))
    other = service.save(project, PROD, _pipeline("prod"))
    assert (first.version, second.version, other.version) == (1, 2, 1)
    assert service.get_active(project, DEV).jenkinsfile == _pipeline("v2")
    history = service.history(project, DEV)
    assert [h.version for h in history] == [2, 1]
    assert history[1].jenkinsfile == _pipeline("v1")


def test_pipeline_validator_hook_and_limits(uow: UnitOfWork, project: str) -> None:
    class Reject:
        def validate(self, jenkinsfile: str) -> None:
            raise ValidationError("nope")

    with pytest.raises(ValidationError):
        PipelineService(uow, Reject()).save(project, DEV, "x")
    assert PipelineService(uow).history(project, DEV) == []
    for bad in ["", "  \n", "x" * (256 * 1024 + 1)]:
        with pytest.raises(ValidationError):
            PipelineService(uow).save(project, DEV, bad)
