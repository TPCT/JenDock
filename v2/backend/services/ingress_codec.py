"""JSON form of ingress plans, shared by the worker (encode) and the API client (decode)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from backend.domain.ingress import (
    DnsPlanView,
    DnsRecordView,
    IngressPlanView,
    PlanAction,
    TlsPlanView,
    VhostPlanView,
)


def plan_to_payload(plan: IngressPlanView) -> dict[str, object]:
    """Encode a plan for the RPC wire."""
    return {
        "domain_id": plan.domain_id,
        "hostname": plan.hostname,
        "operation": plan.operation,
        "dns": {
            "action": plan.dns.action.value,
            "zone": plan.dns.zone,
            "current": _record_payload(plan.dns.current),
            "desired": _record_payload(plan.dns.desired),
            "removes": [_record_payload(r) for r in plan.dns.removes],
            "message": plan.dns.message,
        },
        "vhost": {
            "action": plan.vhost.action.value,
            "current": plan.vhost.current,
            "proposed": plan.vhost.proposed,
            "diff": plan.vhost.diff,
        },
        "tls": {
            "action": plan.tls.action,
            "challenge": plan.tls.challenge,
            "not_after": plan.tls.not_after.isoformat() if plan.tls.not_after else None,
        },
        "steps": list(plan.steps),
        "plan_hash": plan.plan_hash,
    }


def plan_from_payload(data: Mapping[str, Any]) -> IngressPlanView:
    """Decode a plan; raises ``KeyError``/``ValueError`` on a malformed payload."""
    dns, vhost, tls = data["dns"], data["vhost"], data["tls"]
    return IngressPlanView(
        domain_id=int(data["domain_id"]),
        hostname=str(data["hostname"]),
        operation=str(data["operation"]),
        dns=DnsPlanView(
            action=PlanAction(dns["action"]),
            zone=str(dns["zone"]),
            current=_record(dns["current"]),
            desired=_record(dns["desired"]),
            removes=tuple(r for item in dns["removes"] if (r := _record(item)) is not None),
            message=str(dns["message"]),
        ),
        vhost=VhostPlanView(
            action=PlanAction(vhost["action"]),
            current=vhost["current"],
            proposed=vhost["proposed"],
            diff=str(vhost["diff"]),
        ),
        tls=TlsPlanView(
            action=str(tls["action"]),
            challenge=tls["challenge"],
            not_after=datetime.fromisoformat(tls["not_after"]) if tls["not_after"] else None,
        ),
        steps=tuple(str(step) for step in data["steps"]),
        plan_hash=str(data["plan_hash"]),
    )


def _record_payload(record: DnsRecordView | None) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        "record_type": record.record_type,
        "name": record.name,
        "content": record.content,
        "proxied": record.proxied,
    }


def _record(data: Mapping[str, Any] | None) -> DnsRecordView | None:
    if data is None:
        return None
    return DnsRecordView(
        str(data["record_type"]), str(data["name"]), str(data["content"]), bool(data["proxied"])
    )
