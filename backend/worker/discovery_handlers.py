"""RPC methods of domain discovery: refresh the snapshot, list zones, look a name up, check one.

They are registered whether or not ingress is enabled: discovery only reads (Cloudflare, the
host helper's read-only listing and the container inventory) and the Cloudflare token only ever
exists inside the worker. ``discovery.refresh`` writes the snapshot into the database, the API
reads it from there.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.services.adopted_checks import AdoptedCheckService
from backend.services.discovery import DiscoveryService, NameLookup, ZoneList
from backend.worker.ingress_handlers import check_payload
from backend.worker.rpc import BadRequestError, Handler, RpcCall

MAX_HOSTNAME = 253


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _LookupParams(_Params):
    hostname: str = Field(min_length=1, max_length=MAX_HOSTNAME)


class _DomainParams(_Params):
    domain_id: int = Field(ge=1)


def _parse[P: _Params](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params)
    except PydanticValidationError as error:
        raise BadRequestError("invalid params") from error


def zones_payload(result: ZoneList) -> dict[str, object]:
    """Wire form of the zone list."""
    return {"configured": result.configured, "zones": list(result.zones), "message": result.message}


def lookup_payload(result: NameLookup) -> dict[str, object]:
    """Wire form of a name lookup."""
    return {
        "hostname": result.hostname,
        "configured": result.configured,
        "zone": result.zone,
        "message": result.message,
        "records": [
            {
                "record_type": record.record_type,
                "content": record.content,
                "proxied": record.proxied,
                "managed": record.managed,
            }
            for record in result.records
        ],
    }


class DiscoveryHandlers:
    """Adapts the discovery services to RPC calls; fresh services per call."""

    def __init__(
        self,
        discovery: Callable[[], DiscoveryService],
        checks: Callable[[], AdoptedCheckService],
    ) -> None:
        self._discovery = discovery
        self._checks = checks

    def refresh(self, call: RpcCall) -> object:
        """``discovery.refresh``: read Cloudflare and Apache again and replace the snapshot."""
        state = self._discovery().refresh(call.require_actor())
        refreshed = state.refreshed_at
        return {
            "refreshed_at": refreshed.isoformat() if refreshed else None,
            "cloudflare": state.cloudflare,
            "helper": state.helper,
        }

    def zones(self, call: RpcCall) -> object:
        """``discovery.zones``: the zone names the stored token can see."""
        call.require_actor()
        return zones_payload(self._discovery().zones())

    def lookup(self, call: RpcCall) -> object:
        """``discovery.lookup``: the existing address records of a host name."""
        params = _parse(_LookupParams, call)
        call.require_actor()
        return lookup_payload(self._discovery().lookup(params.hostname))

    def verify(self, call: RpcCall) -> object:
        """``discovery.verify``: DNS, Apache, HTTPS and backend status of an adopted domain."""
        params = _parse(_DomainParams, call)
        call.require_actor()
        return check_payload(self._checks().run(params.domain_id))


def build_discovery_handlers(
    discovery: Callable[[], DiscoveryService], checks: Callable[[], AdoptedCheckService]
) -> Mapping[str, Handler]:
    """The method table merged into the worker's RPC server."""
    handlers = DiscoveryHandlers(discovery, checks)
    return {
        "discovery.refresh": handlers.refresh,
        "discovery.zones": handlers.zones,
        "discovery.lookup": handlers.lookup,
        "discovery.verify": handlers.verify,
    }
