from datetime import UTC, datetime, timedelta

import pytest

from backend.domain.actor import Actor
from backend.domain.audit import AuditTarget
from backend.domain.enums import ActorType
from backend.domain.errors import ValidationError
from backend.repositories.database import UnitOfWork
from backend.services.audit import AuditService

USER = Actor(ActorType.USER, "1")
START = datetime(2026, 1, 1, tzinfo=UTC)


def _service(uow: UnitOfWork) -> AuditService:
    ticks = iter(range(1000))
    return AuditService(uow, clock=lambda: START + timedelta(seconds=next(ticks)))


def test_record_and_list_newest_first(uow: UnitOfWork) -> None:
    service = _service(uow)
    service.record(USER, "a.one", AuditTarget("project", "1"), {"k": "v"})
    service.record(Actor.system(), "a.two", AuditTarget("environment", "2"))
    events = service.list()
    assert [e.action for e in events] == ["a.two", "a.one"]
    assert events[1].details == {"k": "v"}
    assert events[1].occurred_at == START
    assert events[0].actor_type is ActorType.SYSTEM
    assert events[0].details == {}


def test_filters_and_pagination(uow: UnitOfWork) -> None:
    service = _service(uow)
    for index in range(5):
        service.record(USER, "x.even" if index % 2 == 0 else "x.odd", AuditTarget("t", str(index)))
    service.record(USER, "x.even", AuditTarget("other", "9"))
    assert len(service.list(action="x.even")) == 4
    assert len(service.list(target=AuditTarget("t"))) == 5
    assert [e.target_id for e in service.list(target=AuditTarget("t", "3"))] == ["3"]
    page = service.list(target=AuditTarget("t"), limit=2, offset=2)
    assert [e.target_id for e in page] == ["2", "1"]


def test_sensitive_details_rejected_not_stored(uow: UnitOfWork) -> None:
    service = _service(uow)
    with pytest.raises(ValidationError):
        service.record(USER, "x", AuditTarget("t"), {"outer": {"password": "p"}})
    assert service.list() == []


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (201, 0), (10, -1)])
def test_invalid_pagination(uow: UnitOfWork, limit: int, offset: int) -> None:
    with pytest.raises(ValidationError):
        _service(uow).list(limit=limit, offset=offset)
