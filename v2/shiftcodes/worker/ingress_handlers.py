"""RPC methods for ingress: preview, apply, remove, check and certificate issuance.

All of them run in the worker, which alone can read the Cloudflare token and reach the host
helper's socket. ``ingress.apply`` and ``ingress.remove`` demand the ``plan_hash`` of a preview.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from shiftcodes.services.certificates import CertificateStatus
from shiftcodes.services.domain_checks import CheckResult, DomainCheckService
from shiftcodes.services.ingress import IngressService
from shiftcodes.services.ingress_codec import plan_to_payload
from shiftcodes.worker.rpc import BadRequestError, Handler, RpcCall

SHA256_HEX_LENGTH = 64
IngressFactoryFn = Callable[[], IngressService]
CheckFactoryFn = Callable[[], DomainCheckService]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _DomainParams(_Params):
    domain_id: int = Field(ge=1)


class _PreviewParams(_DomainParams):
    operation: Literal["apply", "remove"] = "apply"


class _PlanParams(_DomainParams):
    plan_hash: str = Field(min_length=SHA256_HEX_LENGTH, max_length=SHA256_HEX_LENGTH)


def _parse[P: _Params](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params)
    except PydanticValidationError as error:
        raise BadRequestError("invalid params") from error


def certificate_payload(status: CertificateStatus) -> dict[str, object]:
    """Wire form of a certificate status."""
    return {
        "not_after": status.not_after.isoformat(),
        "days_remaining": status.days_remaining,
        "state": status.state.value,
    }


def check_payload(result: CheckResult) -> dict[str, object]:
    """Wire form of a check result."""
    return {
        "checked_at": result.checked_at.isoformat(),
        "ok": result.ok,
        "results": dict(result.results),
    }


class IngressHandlers:
    """Adapts the ingress services to RPC calls; fresh services (and units of work) per call."""

    def __init__(self, ingress: IngressFactoryFn, checks: CheckFactoryFn) -> None:
        self._ingress = ingress
        self._checks = checks

    def preview(self, call: RpcCall) -> object:
        """``ingress.preview``: show the DNS, vhost and TLS changes without applying them."""
        params = _parse(_PreviewParams, call)
        call.require_actor()
        return plan_to_payload(self._ingress().preview(params.domain_id, params.operation))

    def apply(self, call: RpcCall) -> object:
        """``ingress.apply``: apply a reviewed plan."""
        params = _parse(_PlanParams, call)
        plan = self._ingress().apply(params.domain_id, params.plan_hash, call.require_actor())
        return plan_to_payload(plan)

    def remove(self, call: RpcCall) -> object:
        """``ingress.remove``: tear down a reviewed removal plan."""
        params = _parse(_PlanParams, call)
        plan = self._ingress().remove(params.domain_id, params.plan_hash, call.require_actor())
        return plan_to_payload(plan)

    def check(self, call: RpcCall) -> object:
        """``ingress.check``: run and store the status checks."""
        params = _parse(_DomainParams, call)
        call.require_actor()
        return check_payload(self._checks().run(params.domain_id))

    def issue_certificate(self, call: RpcCall) -> object:
        """``ingress.certificate``: obtain or renew the certificate now."""
        params = _parse(_DomainParams, call)
        status = self._ingress().issue_certificate(params.domain_id, call.require_actor())
        return certificate_payload(status)


def build_ingress_handlers(
    ingress: IngressFactoryFn, checks: CheckFactoryFn
) -> Mapping[str, Handler]:
    """The method table merged into the worker's RPC server."""
    handlers = IngressHandlers(ingress, checks)
    return {
        "ingress.preview": handlers.preview,
        "ingress.apply": handlers.apply,
        "ingress.remove": handlers.remove,
        "ingress.check": handlers.check,
        "ingress.certificate": handlers.issue_certificate,
    }
