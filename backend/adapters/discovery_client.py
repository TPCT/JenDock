"""The API's view of the worker's ``discovery.*`` methods (Cloudflare and Apache are read there)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from backend.adapters.worker_client import WorkerClient
from backend.domain.actor import Actor
from backend.domain.errors import WorkerError, WorkerUnavailableError
from backend.services.discovery import NameLookup, RecordView, ZoneList
from backend.services.domain_checks import CheckResult

# Reading every zone's records takes a few Cloudflare calls per zone.
REFRESH_TIMEOUT = 180.0
QUICK_TIMEOUT = 45.0
CHECK_TIMEOUT = 60.0


class DiscoveryGateway(Protocol):
    """Discovery operations only the worker can do; implemented by the client and test fakes."""

    def refresh(self, actor: Actor) -> None:
        """Read Cloudflare and Apache and replace the discovery snapshot."""

    def zones(self, actor: Actor) -> ZoneList:
        """The zones the stored Cloudflare token can see."""

    def lookup(self, hostname: str, actor: Actor) -> NameLookup:
        """The address records that already exist for ``hostname``."""

    def verify(self, domain_id: int, actor: Actor) -> CheckResult:
        """DNS, Apache, HTTPS and backend status of an adopted domain."""


class DiscoveryClient(WorkerClient):
    """Calls the ``discovery.*`` methods of the worker."""

    def refresh(self, actor: Actor) -> None:
        """``discovery.refresh``."""
        self._call("discovery.refresh", actor, {}, REFRESH_TIMEOUT)

    def zones(self, actor: Actor) -> ZoneList:
        """``discovery.zones``."""
        data = _mapping(self._call("discovery.zones", actor, {}, QUICK_TIMEOUT))
        try:
            zones = data["zones"]
            if not isinstance(zones, list):
                raise TypeError("zones")
            return ZoneList(
                bool(data["configured"]), tuple(str(z) for z in zones), str(data.get("message", ""))
            )
        except (KeyError, TypeError) as error:
            raise WorkerError("worker returned an unexpected response") from error

    def lookup(self, hostname: str, actor: Actor) -> NameLookup:
        """``discovery.lookup``."""
        params = {"hostname": hostname}
        data = _mapping(self._call("discovery.lookup", actor, params, QUICK_TIMEOUT))
        try:
            records = data["records"]
            if not isinstance(records, list):
                raise TypeError("records")
            views = tuple(
                RecordView(
                    str(r["record_type"]), str(r["content"]), bool(r["proxied"]), bool(r["managed"])
                )
                for r in records
            )
            zone = data.get("zone")
            return NameLookup(
                str(data["hostname"]),
                bool(data["configured"]),
                str(zone) if zone else None,
                views,
                str(data.get("message", "")),
            )
        except (KeyError, TypeError) as error:
            raise WorkerError("worker returned an unexpected response") from error

    def verify(self, domain_id: int, actor: Actor) -> CheckResult:
        """``discovery.verify``."""
        params = {"domain_id": domain_id}
        data = _mapping(self._call("discovery.verify", actor, params, CHECK_TIMEOUT))
        try:
            return CheckResult(
                domain_id,
                datetime.fromisoformat(str(data["checked_at"])),
                bool(data["ok"]),
                dict(data["results"]),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise WorkerError("worker returned an unexpected response") from error


class UnavailableDiscovery:
    """The default gateway when no worker is wired: every call reports the worker as down."""

    def _fail(self) -> WorkerUnavailableError:
        return WorkerUnavailableError("discovery is not available")

    def refresh(self, actor: Actor) -> None:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def zones(self, actor: Actor) -> ZoneList:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def lookup(self, hostname: str, actor: Actor) -> NameLookup:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def verify(self, domain_id: int, actor: Actor) -> CheckResult:  # noqa: ARG002
        """Always raises."""
        raise self._fail()


def _mapping(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise WorkerError("worker returned an unexpected response")
    return result
