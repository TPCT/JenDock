"""Control-flow exceptions of step execution."""

from __future__ import annotations


class StepFailed(Exception):  # noqa: N818 - reads naturally next to RunCancelled
    """A step failed; the message is shown in the step log and the run summary."""


class DeadlineExceeded(StepFailed):
    """A ``timeout`` (or the default step limit) ran out."""


class RunCancelled(Exception):  # noqa: N818 - a signal, not an error
    """The run was cancelled (or the worker is shutting down); never retried."""
