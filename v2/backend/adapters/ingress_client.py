"""The API's view of the worker's ingress methods (the worker does the actual work)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from backend.adapters.worker_client import WorkerClient
from backend.domain.actor import Actor
from backend.domain.errors import WorkerError, WorkerUnavailableError
from backend.domain.ingress import CertificateState, IngressPlanView
from backend.services.domain_checks import CheckResult
from backend.services.ingress_codec import plan_from_payload

# Issuing a certificate waits for DNS propagation and the CA, so these calls get minutes.
LONG_TIMEOUT = 330.0
PREVIEW_TIMEOUT = 60.0
CHECK_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class IssuedInfo:
    """Result of a manual certificate issuance."""

    not_after: datetime
    days_remaining: int
    state: CertificateState


class IngressGateway(Protocol):
    """Ingress operations the API needs; implemented by ``IngressClient`` and test fakes."""

    def preview(self, domain_id: int, operation: str, actor: Actor) -> IngressPlanView:
        """Plan an apply or remove without changing anything."""

    def apply(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:
        """Apply a previously previewed plan."""

    def remove(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:
        """Remove the domain from DNS, Apache and TLS."""

    def check(self, domain_id: int, actor: Actor) -> CheckResult:
        """Run and store the status checks."""

    def issue_certificate(self, domain_id: int, actor: Actor) -> IssuedInfo:
        """Issue or renew the certificate now."""


class IngressClient(WorkerClient):
    """Calls the ``ingress.*`` methods of the worker."""

    def preview(self, domain_id: int, operation: str, actor: Actor) -> IngressPlanView:
        """``ingress.preview``."""
        params = {"domain_id": domain_id, "operation": operation}
        return _plan(self._call("ingress.preview", actor, params, PREVIEW_TIMEOUT))

    def apply(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:
        """``ingress.apply``."""
        params = {"domain_id": domain_id, "plan_hash": plan_hash}
        return _plan(self._call("ingress.apply", actor, params, LONG_TIMEOUT))

    def remove(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:
        """``ingress.remove``."""
        params = {"domain_id": domain_id, "plan_hash": plan_hash}
        return _plan(self._call("ingress.remove", actor, params, LONG_TIMEOUT))

    def check(self, domain_id: int, actor: Actor) -> CheckResult:
        """``ingress.check``."""
        result = self._call("ingress.check", actor, {"domain_id": domain_id}, CHECK_TIMEOUT)
        data = _mapping(result)
        try:
            return CheckResult(
                domain_id,
                datetime.fromisoformat(str(data["checked_at"])),
                bool(data["ok"]),
                dict(data["results"]),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise WorkerError("worker returned an unexpected response") from error

    def issue_certificate(self, domain_id: int, actor: Actor) -> IssuedInfo:
        """``ingress.certificate``."""
        result = self._call("ingress.certificate", actor, {"domain_id": domain_id}, LONG_TIMEOUT)
        data = _mapping(result)
        try:
            return IssuedInfo(
                datetime.fromisoformat(str(data["not_after"])),
                int(data["days_remaining"]),
                CertificateState(data["state"]),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise WorkerError("worker returned an unexpected response") from error


class UnavailableIngress:
    """The default gateway when ingress is not wired: every call reports the worker as down."""

    def _fail(self) -> WorkerUnavailableError:
        return WorkerUnavailableError("ingress is not available")

    def preview(self, domain_id: int, operation: str, actor: Actor) -> IngressPlanView:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def apply(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def remove(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def check(self, domain_id: int, actor: Actor) -> CheckResult:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def issue_certificate(self, domain_id: int, actor: Actor) -> IssuedInfo:  # noqa: ARG002
        """Always raises."""
        raise self._fail()


def _mapping(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise WorkerError("worker returned an unexpected response")
    return result


def _plan(result: object) -> IngressPlanView:
    try:
        return plan_from_payload(_mapping(result))
    except (KeyError, ValueError, TypeError) as error:
        raise WorkerError("worker returned an unexpected response") from error
