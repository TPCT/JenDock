"""Queries for the discovery snapshot: discovered host names and the state of the last refresh."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, delete, func, select
from sqlalchemy.orm import Session

from backend.domain.discovery import Confidence, LinkFilter
from backend.domain.paging import PageQuery
from backend.repositories.models import DiscoveredDomain, DiscoveryState, Domain
from backend.repositories.paging import contains, ordered

STATE_ID = 1
SORTS = {
    "hostname": DiscoveredDomain.hostname,
    "zone": DiscoveredDomain.zone_name,
}
SEARCH_COLUMNS = (
    DiscoveredDomain.hostname,
    DiscoveredDomain.record_content,
    DiscoveredDomain.suggested_project,
    DiscoveredDomain.upstream_host,
)


class DiscoveryRepository:
    """Reads and replaces the discovery snapshot."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def replace_all(self, rows: Iterable[DiscoveredDomain]) -> None:
        """Replace the whole snapshot with ``rows``."""
        self._session.execute(delete(DiscoveredDomain))
        self._session.add_all(list(rows))
        self._session.flush()

    def get_state(self) -> DiscoveryState | None:
        """The state row, or ``None`` before the first refresh."""
        return self._session.get(DiscoveryState, STATE_ID)

    def save_state(self, state: DiscoveryState) -> DiscoveryState:
        """Insert or update the single state row."""
        state.id = STATE_ID
        merged = self._session.merge(state)
        self._session.flush()
        return merged

    def get_by_hostname(self, hostname: str) -> DiscoveredDomain | None:
        """The discovered row of exactly this host name."""
        return self._session.scalar(
            select(DiscoveredDomain).where(DiscoveredDomain.hostname == hostname)
        )

    def list_all(self) -> Sequence[DiscoveredDomain]:
        """Every discovered host name ordered by name."""
        query = select(DiscoveredDomain).order_by(DiscoveredDomain.hostname)
        return self._session.scalars(query).all()

    def list_obvious_unlinked(self) -> Sequence[DiscoveredDomain]:
        """Unlinked rows whose suggestion is complete and certain (HIGH): safe to adopt in bulk."""
        query = self._unlinked(select(DiscoveredDomain)).where(
            DiscoveredDomain.suggested_project.is_not(None),
            DiscoveredDomain.suggested_service.is_not(None),
            DiscoveredDomain.suggested_container_port.is_not(None),
            DiscoveredDomain.suggested_published_port.is_not(None),
            DiscoveredDomain.suggestion_confidence == Confidence.HIGH,
        )
        return self._session.scalars(query.order_by(DiscoveredDomain.hostname)).all()

    def zones(self) -> list[str]:
        """The distinct zone names of the snapshot, sorted."""
        query = select(DiscoveredDomain.zone_name).where(DiscoveredDomain.zone_name.is_not(None))
        return sorted({name for name in self._session.scalars(query) if name})

    def page(
        self, query: PageQuery, link: LinkFilter | None, zone: str | None
    ) -> tuple[list[tuple[DiscoveredDomain, Domain | None]], int]:
        """One page of rows with the linked domain (if any) and the total match count."""
        joined = select(DiscoveredDomain, Domain).outerjoin(
            Domain, Domain.hostname == DiscoveredDomain.hostname
        )
        counted = (
            select(func.count())
            .select_from(DiscoveredDomain)
            .outerjoin(Domain, Domain.hostname == DiscoveredDomain.hostname)
        )
        total = self._session.scalar(self._filtered(counted, query, link, zone))
        statement = ordered(
            self._filtered(joined, query, link, zone),
            query,
            SORTS,
            "hostname",
            tiebreak=DiscoveredDomain.id,
        )
        rows = self._session.execute(statement.offset(query.offset).limit(query.limit)).all()
        return [(row[0], row[1]) for row in rows], int(total or 0)

    def _unlinked[S: Select[*tuple[Any, ...]]](self, statement: S) -> S:
        return statement.outerjoin(Domain, Domain.hostname == DiscoveredDomain.hostname).where(
            Domain.id.is_(None)
        )

    def _filtered[S: Select[*tuple[Any, ...]]](
        self, statement: S, query: PageQuery, link: LinkFilter | None, zone: str | None
    ) -> S:
        conditions: list[ColumnElement[bool]] = []
        if query.term is not None:
            conditions.append(contains(SEARCH_COLUMNS, query.term))
        if zone is not None:
            conditions.append(DiscoveredDomain.zone_name == zone)
        if link is LinkFilter.LINKED:
            conditions.append(Domain.id.is_not(None))
        elif link is LinkFilter.UNLINKED:
            conditions.append(Domain.id.is_(None))
        elif link is LinkFilter.SUGGESTED:
            conditions.append(Domain.id.is_(None))
            conditions.append(DiscoveredDomain.suggested_project.is_not(None))
        return statement.where(*conditions) if conditions else statement
