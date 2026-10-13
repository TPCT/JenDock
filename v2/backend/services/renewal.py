"""The certificate renewal loop that runs inside the worker."""

from __future__ import annotations

import logging
from typing import Protocol

from backend.services.certificates import CertificateService, RenewalOutcome
from backend.services.ingress_lock import INGRESS_LOCK
from backend.services.notification_events import CertificateNotifier, safely

logger = logging.getLogger(__name__)


class StopSignal(Protocol):
    """``ShutdownHandler`` as the loop sees it."""

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout``; return True when shutdown was requested."""


class RenewalScheduler:
    """Renews certificates that expire within the policy window (30 days by default).

    ``tick`` does one pass and is what tests drive with a fake clock; ``run_forever`` repeats it
    every ``interval`` seconds until shutdown. With a ``notifier`` each pass also announces failed
    renewals and certificates with fewer than 14 days left. A failed renewal is retried after the
    policy's retry delay, long before the certificate actually expires.
    """

    def __init__(
        self,
        certificates: CertificateService,
        interval: float,
        notifier: CertificateNotifier | None = None,
    ) -> None:
        self._certificates = certificates
        self._interval = interval
        self._notifier = notifier

    def tick(self) -> list[RenewalOutcome]:
        """Renew whatever is due now."""
        with INGRESS_LOCK:
            outcomes = self._certificates.renew_due()
        for outcome in outcomes:
            if outcome.ok:
                logger.info("certificate renewed", extra={"domain_id": outcome.domain_id})
        if self._notifier is not None:
            notifier = self._notifier
            safely(lambda: notifier.after_pass(outcomes, self._certificates.report()), "renewal")
        return outcomes

    def run_forever(self, stop: StopSignal) -> None:
        """Run ``tick`` periodically until ``stop`` fires; one failing pass never ends the loop."""
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("certificate renewal pass failed")
            if stop.wait(self._interval):
                return
