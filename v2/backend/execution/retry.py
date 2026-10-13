"""Retry loop shared by the ``retry`` step and the ``retry`` option."""

from __future__ import annotations

from collections.abc import Callable

from backend.execution.errors import StepFailed


def run_with_retries(
    count: int,
    attempt: Callable[[int], None],
    on_failure: Callable[[int, StepFailed], None],
) -> None:
    """Call ``attempt(n)`` until it succeeds or ``count`` attempts failed.

    Only ``StepFailed`` is retried: a cancellation (``RunCancelled``) passes straight through.
    The last failure is raised.
    """
    for number in range(1, count + 1):
        try:
            attempt(number)
        except StepFailed as error:
            if number == count:
                raise
            on_failure(number, error)
        else:
            return
