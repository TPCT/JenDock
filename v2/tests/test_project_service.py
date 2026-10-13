import pytest
from sqlalchemy.exc import IntegrityError

from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import ConflictError, NotFoundError, ValidationError
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.environments import EnvironmentRepository
from shiftcodes.repositories.models import Environment
from shiftcodes.repositories.projects import ProjectRepository
from shiftcodes.services.projects import ProjectService


def test_create_makes_exactly_two_environments(uow: UnitOfWork) -> None:
    detail = ProjectService(uow).create("demo", " Demo ", "octo/demo", "dev", "release/prod")
    assert detail.project.name == "Demo"
    by_name = {e.name: e for e in detail.environments}
    assert set(by_name) == {EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION}
    assert by_name[EnvironmentName.DEVELOPMENT].branch == "dev"
    assert by_name[EnvironmentName.PRODUCTION].branch == "release/prod"
    assert all(not e.auto_deploy and e.services == [] for e in detail.environments)
    assert len(ProjectService(uow).get("demo").environments) == 2


def test_default_branches(uow: UnitOfWork) -> None:
    detail = ProjectService(uow).create("demo", "Demo", "octo/demo")
    assert sorted(e.branch for e in detail.environments) == ["development", "production"]


def test_create_rolls_back_when_environment_insert_fails(
    uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = EnvironmentRepository.add
    calls: list[int] = []

    def flaky(self: EnvironmentRepository, environment: Environment) -> Environment:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return original(self, environment)

    monkeypatch.setattr(EnvironmentRepository, "add", flaky)
    service = ProjectService(uow)
    with pytest.raises(RuntimeError):
        service.create("demo", "Demo", "octo/demo")
    monkeypatch.undo()
    assert service.list(include_archived=True) == []
    with uow:
        assert uow.session.query(Environment).count() == 0


def test_duplicate_slug_conflicts_even_when_archived(uow: UnitOfWork) -> None:
    service = ProjectService(uow)
    service.create("demo", "Demo", "octo/demo")
    with pytest.raises(ConflictError):
        service.create("demo", "Other", "octo/other")
    service.archive("demo")
    with pytest.raises(ConflictError):
        service.create("demo", "Other", "octo/other")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slug": "Bad"},
        {"repository": "nope"},
        {"name": " "},
        {"development_branch": "a..b"},
        {"production_branch": "-x"},
        {"development_branch": "main", "production_branch": "main"},
    ],
)
def test_create_validation(uow: UnitOfWork, kwargs: dict[str, str]) -> None:
    args = {"slug": "demo", "name": "Demo", "repository": "o/r"} | kwargs
    with pytest.raises(ValidationError):
        ProjectService(uow).create(**args)  # type: ignore[arg-type]
    assert ProjectService(uow).list(include_archived=True) == []


def test_archive_and_list(uow: UnitOfWork) -> None:
    service = ProjectService(uow)
    service.create("a", "A", "o/a")
    service.create("b", "B", "o/b")
    first = service.archive("a").archived_at
    assert first is not None
    assert service.archive("a").archived_at == first
    assert [p.slug for p in service.list()] == ["b"]
    assert [p.slug for p in service.list(include_archived=True)] == ["a", "b"]
    assert service.get("a").project.archived_at is not None


def test_update(uow: UnitOfWork) -> None:
    service = ProjectService(uow)
    service.create("a", "A", "o/a")
    updated = service.update("a", name="New", repository="o/new")
    assert (updated.name, updated.repository) == ("New", "o/new")
    assert service.update("a").name == "New"
    with pytest.raises(ValidationError):
        service.update("a", repository="bad")
    assert service.get("a").project.repository == "o/new"


def test_get_missing(uow: UnitOfWork) -> None:
    with pytest.raises(NotFoundError):
        ProjectService(uow).get("missing")


def test_non_slug_integrity_error_is_not_reported_as_duplicate_slug(
    uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: EnvironmentRepository, environment: Environment) -> Environment:
        raise IntegrityError("INSERT", {}, Exception("FOREIGN KEY constraint failed"))

    monkeypatch.setattr(EnvironmentRepository, "add", broken)
    with pytest.raises(ConflictError) as info:
        ProjectService(uow).create("demo", "Demo", "octo/demo")
    assert "slug" not in str(info.value)


def test_slug_race_detected_from_the_constraint(
    uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    ProjectService(uow).create("demo", "Demo", "octo/demo")
    monkeypatch.setattr(ProjectRepository, "get_by_slug", lambda *_args: None)
    with pytest.raises(ConflictError, match="slug already exists"):
        ProjectService(uow).create("demo", "Other", "octo/other")
