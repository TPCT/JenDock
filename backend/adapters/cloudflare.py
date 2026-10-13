"""Cloudflare DNS: a thin API client and an ownership-aware record manager.

The manager only ever changes records that carry the platform's tag in their comment. A record
with the same name that does not carry it is reported as a conflict and left alone, in the
preview and in the apply. Planning is read-only (GET requests), which is the dry-run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from backend.adapters.errors import AdapterError
from backend.domain.ingress import MANAGED_TAG, DnsRecordType, PlanAction
from backend.hosthelper.names import WILDCARD_PREFIX

AUTO_TTL = 1
PAGE_SIZE = 100
ZONE_PAGE_SIZE = 50  # Cloudflare refuses a larger ``per_page`` on the zone list
MAX_PAGES = 20
MAX_DISCOVERY_PAGES = 100
ALREADY_EXISTS_CODES = frozenset({81057, 81058})
ADDRESS_TYPES = frozenset({"A", "AAAA", "CNAME"})
DEFAULT_TIMEOUT = 15.0
MAX_ERROR_TEXT = 300


class CloudflareError(AdapterError):
    """Cloudflare refused a request or could not be reached; the message holds no credentials."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Zone:
    """A Cloudflare zone."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class DnsRecord:
    """A record as returned by Cloudflare."""

    id: str
    record_type: str
    name: str
    content: str
    proxied: bool
    comment: str | None

    @property
    def owned(self) -> bool:
        """Whether the platform created (and may therefore change) this record."""
        return self.comment is not None and self.comment.startswith(MANAGED_TAG)


@dataclass(frozen=True, slots=True)
class DnsDesired:
    """The record the platform wants for a hostname."""

    record_type: DnsRecordType
    name: str
    content: str
    proxied: bool
    comment: str


@dataclass(frozen=True, slots=True)
class DnsPlan:
    """What applying would do: create/update/noop/conflict, plus owned records to replace."""

    action: PlanAction
    zone: Zone
    desired: DnsDesired | None
    existing: DnsRecord | None = None
    stale: tuple[DnsRecord, ...] = ()
    message: str = ""
    foreign: tuple[DnsRecord, ...] = field(default=())

    @property
    def changes(self) -> bool:
        """Whether applying changes anything at Cloudflare."""
        return self.action in {PlanAction.CREATE, PlanAction.UPDATE, PlanAction.DELETE}


class CloudflareClient:
    """Minimal Cloudflare v4 client (zones and DNS records)."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.cloudflare.com/client/v4",
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=timeout)

    def find_zone(self, hostname: str) -> Zone:
        """Return the most specific zone containing ``hostname``."""
        labels = hostname.removeprefix(WILDCARD_PREFIX).split(".")
        for start in range(len(labels) - 1):
            candidate = ".".join(labels[start:])
            result = self._get(
                "/zones", {"name": candidate, "status": "active"}, per_page=ZONE_PAGE_SIZE
            )
            if result:
                return Zone(str(result[0]["id"]), str(result[0]["name"]))
        raise CloudflareError(f"no Cloudflare zone is reachable with this token for {hostname}")

    def verify_token(self) -> str:
        """The token's status as Cloudflare reports it (``GET /user/tokens/verify``)."""
        payload = self._request("GET", "/user/tokens/verify", params=None, body=None)
        result = payload.get("result")
        status = result.get("status") if isinstance(result, dict) else None
        return str(status) if status else "unknown"

    def list_zone_names(self, limit: int = 200) -> list[str]:
        """Names of the active zones the token can see, sorted (at most ``limit``)."""
        return sorted(zone.name for zone in self.list_zones())[:limit]

    def list_zones(self) -> list[Zone]:
        """Every active zone the token can see (all pages)."""
        items = self._get(
            "/zones", {"status": "active"}, per_page=ZONE_PAGE_SIZE, max_pages=MAX_DISCOVERY_PAGES
        )
        return [
            Zone(str(item["id"]), str(item["name"]).lower()) for item in items if "name" in item
        ]

    def list_address_records(self, zone_id: str) -> list[DnsRecord]:
        """Every A, AAAA and CNAME record of a zone (all pages; read-only)."""
        records: list[DnsRecord] = []
        for record_type in sorted(ADDRESS_TYPES):
            items = self._get(
                f"/zones/{zone_id}/dns_records",
                {"type": record_type},
                max_pages=MAX_DISCOVERY_PAGES,
            )
            records.extend(_record(item) for item in items)
        return records

    def list_records(self, zone_id: str, name: str) -> list[DnsRecord]:
        """Return all records with exactly this name."""
        items = self._get(f"/zones/{zone_id}/dns_records", {"name": name})
        return [_record(item) for item in items]

    def create_record(self, zone_id: str, body: Mapping[str, object]) -> DnsRecord:
        """Create a record."""
        return _record(self._send("POST", f"/zones/{zone_id}/dns_records", body))

    def update_record(self, zone_id: str, record_id: str, body: Mapping[str, object]) -> DnsRecord:
        """Replace a record's data."""
        return _record(self._send("PUT", f"/zones/{zone_id}/dns_records/{record_id}", body))

    def delete_record(self, zone_id: str, record_id: str) -> None:
        """Delete a record."""
        self._send("DELETE", f"/zones/{zone_id}/dns_records/{record_id}", None)

    def _get(
        self,
        path: str,
        params: Mapping[str, str],
        *,
        per_page: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> list[Mapping[str, Any]]:
        collected: list[Mapping[str, Any]] = []
        for page in range(1, max_pages + 1):
            query = {**params, "per_page": str(per_page), "page": str(page)}
            payload = self._request("GET", path, params=query, body=None)
            result = payload.get("result")
            collected.extend(cast("list[Mapping[str, Any]]", result or []))
            info = payload.get("result_info") or {}
            if int(info.get("total_pages", 1)) <= page:
                return collected
        raise CloudflareError("Cloudflare returned too many pages")

    def _send(self, method: str, path: str, body: Mapping[str, object] | None) -> Mapping[str, Any]:
        result = self._request(method, path, params=None, body=body).get("result")
        return cast("Mapping[str, Any]", result or {})

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None,
        body: Mapping[str, object] | None,
    ) -> Mapping[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            response = self._client.request(
                method, f"{self._base}{path}", params=params, json=body, headers=headers
            )
        except httpx.HTTPError as error:
            raise CloudflareError("Cloudflare could not be reached") from error
        return _decode(response)


def _decode(response: httpx.Response) -> Mapping[str, Any]:
    if response.status_code in {401, 403}:
        raise CloudflareError("Cloudflare rejected the API token (check its scope)")
    try:
        payload = response.json()
    except ValueError as error:
        raise CloudflareError(f"Cloudflare returned HTTP {response.status_code}") from error
    if not isinstance(payload, dict):
        raise CloudflareError("Cloudflare returned an unexpected response")
    if response.status_code >= 400 or not payload.get("success", False):
        errors = payload.get("errors") or [{}]
        first = errors[0] if isinstance(errors[0], dict) else {}
        message = str(first.get("message", f"HTTP {response.status_code}"))[:MAX_ERROR_TEXT]
        code = first.get("code")
        raise CloudflareError(
            f"Cloudflare error: {message}", code if isinstance(code, int) else None
        )
    return cast("Mapping[str, Any]", payload)


def _record(item: Mapping[str, Any]) -> DnsRecord:
    try:
        return DnsRecord(
            id=str(item["id"]),
            record_type=str(item["type"]),
            name=str(item["name"]).lower(),
            content=str(item.get("content", "")),
            proxied=bool(item.get("proxied", False)),
            comment=item.get("comment"),
        )
    except KeyError as error:
        raise CloudflareError("Cloudflare returned an unexpected record") from error


def record_body(desired: DnsDesired) -> dict[str, object]:
    """The JSON body for creating or replacing the desired record."""
    return {
        "type": desired.record_type.value,
        "name": desired.name,
        "content": desired.content,
        "ttl": AUTO_TTL,
        "proxied": desired.proxied,
        "comment": desired.comment,
    }


class CloudflareDns:
    """Plans and applies the records of hostnames; idempotent and ownership-aware."""

    def __init__(self, client: CloudflareClient) -> None:
        self._client = client

    @property
    def client(self) -> CloudflareClient:
        """The underlying API client (also used for ACME TXT records)."""
        return self._client

    def plan_upsert(self, desired: DnsDesired) -> DnsPlan:
        """Compare ``desired`` with Cloudflare without changing anything."""
        zone = self._client.find_zone(desired.name)
        records = [
            r
            for r in self._client.list_records(zone.id, desired.name)
            if r.record_type in ADDRESS_TYPES
        ]
        owned = [r for r in records if r.owned]
        foreign = tuple(
            r for r in records if not r.owned and _clashes(r, desired.record_type.value)
        )
        if foreign:
            names = ", ".join(f"{r.record_type} {r.content}" for r in foreign)
            return DnsPlan(
                PlanAction.CONFLICT,
                zone,
                desired,
                message=f"a record not managed by Jendock already exists ({names})",
                foreign=foreign,
            )
        same = next((r for r in owned if r.record_type == desired.record_type.value), None)
        stale = tuple(r for r in owned if r is not same and _clashes(r, desired.record_type.value))
        return _plan_for(zone, desired, same, stale)

    def apply_upsert(self, plan: DnsPlan) -> DnsRecord | None:
        """Carry out a plan from ``plan_upsert``; returns the resulting record."""
        if plan.action is PlanAction.CONFLICT:
            raise CloudflareError(plan.message)
        desired = plan.desired
        if desired is None:
            raise CloudflareError("plan has no desired record")
        for record in plan.stale:
            self._client.delete_record(plan.zone.id, record.id)
        if plan.action is PlanAction.NOOP:
            return plan.existing
        if plan.action is PlanAction.UPDATE and plan.existing is not None:
            if not _differs(plan.existing, desired):
                return plan.existing
            return self._client.update_record(plan.zone.id, plan.existing.id, record_body(desired))
        return self._create(plan, desired)

    def _create(self, plan: DnsPlan, desired: DnsDesired) -> DnsRecord | None:
        try:
            return self._client.create_record(plan.zone.id, record_body(desired))
        except CloudflareError as error:
            if error.code not in ALREADY_EXISTS_CODES:
                raise
        # Lost a race with another creator: look again and converge (never overwrite foreign data).
        replanned = self.plan_upsert(desired)
        if replanned.action is PlanAction.CONFLICT or replanned.action is PlanAction.CREATE:
            raise CloudflareError(replanned.message or "record could not be created")
        return self.apply_upsert(replanned)

    def plan_delete(self, name: str) -> DnsPlan:
        """Plan the removal of the platform's own address records for ``name``."""
        zone = self._client.find_zone(name)
        records = [
            r for r in self._client.list_records(zone.id, name) if r.record_type in ADDRESS_TYPES
        ]
        owned = tuple(r for r in records if r.owned)
        foreign = tuple(r for r in records if not r.owned)
        action = PlanAction.DELETE if owned else PlanAction.NOOP
        message = "records not managed by Jendock are left untouched" if foreign else ""
        return DnsPlan(action, zone, None, stale=owned, message=message, foreign=foreign)

    def apply_delete(self, plan: DnsPlan) -> int:
        """Delete the owned records of a ``plan_delete`` plan; returns how many were removed."""
        for record in plan.stale:
            if not record.owned:  # defence in depth
                raise CloudflareError("refusing to delete a record not managed by Jendock")
            self._client.delete_record(plan.zone.id, record.id)
        return len(plan.stale)


def _clashes(record: DnsRecord, wanted_type: str) -> bool:
    """Whether ``record`` cannot coexist with a record of ``wanted_type`` at the same name."""
    if record.record_type == wanted_type:
        return True
    return "CNAME" in {record.record_type, wanted_type}


def _differs(record: DnsRecord, desired: DnsDesired) -> bool:
    return record.content != desired.content or record.proxied != desired.proxied


def _plan_for(
    zone: Zone, desired: DnsDesired, same: DnsRecord | None, stale: tuple[DnsRecord, ...]
) -> DnsPlan:
    if same is None:
        return DnsPlan(PlanAction.CREATE, zone, desired, stale=stale)
    if _differs(same, desired) or stale:
        return DnsPlan(PlanAction.UPDATE, zone, desired, existing=same, stale=stale)
    return DnsPlan(PlanAction.NOOP, zone, desired, existing=same)
