"""The immutable lexical state a step runs in: variables, agent, directory, deadline."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from backend.execution.errors import StepFailed
from backend.pipeline.ast import AgentDocker, StringValue, TextPart, VariablePart, VarSource


class FailureFlag:
    """Set when a stage of a branch failed; later siblings of that branch are skipped."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        """Mark the branch as failed."""
        self._event.set()

    def is_set(self) -> bool:
        """Whether the branch failed."""
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class Scope:
    """Variables and settings visible at one point of the pipeline."""

    env: Mapping[str, str]
    secret_names: frozenset[str] = frozenset()
    params: Mapping[str, str] = field(default_factory=dict)
    agent: AgentDocker | None = None
    directory: str = ""
    deadline: float | None = None
    post: bool = False
    flag: FailureFlag = field(default_factory=FailureFlag)

    def with_env(self, values: Mapping[str, str], secrets: frozenset[str] = frozenset()) -> Scope:
        """A scope with ``values`` added (a secret name stays secret when overridden)."""
        return replace(self, env={**self.env, **values}, secret_names=self.secret_names | secrets)

    def with_directory(self, relative: str) -> Scope:
        """A scope whose working directory is ``relative`` below the current one."""
        joined = f"{self.directory}/{relative}" if self.directory else relative
        parts = [part for part in joined.split("/") if part not in ("", ".")]
        return replace(self, directory="/".join(parts))

    def with_timeout(self, seconds: float) -> Scope:
        """A scope that must finish within ``seconds`` (never later than the current deadline)."""
        deadline = time.monotonic() + seconds
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        return replace(self, deadline=deadline)

    def for_post(self) -> Scope:
        """The scope post steps run in: cancellation and earlier deadlines no longer apply."""
        return replace(self, post=True, deadline=None)

    def remaining(self) -> float | None:
        """Seconds until the deadline (never negative), or ``None`` without one."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def render(self, value: StringValue, *, for_shell: bool = False) -> str:
        """Substitute the variable parts of ``value``; nothing is evaluated.

        With ``for_shell=True`` a variable bound to a credential is written as ``${NAME}`` so the
        shell reads it from the container environment and the secret never enters a command line.
        """
        return "".join(self._part(part, for_shell) for part in value.parts)

    def _part(self, part: TextPart | VariablePart, for_shell: bool) -> str:
        if isinstance(part, TextPart):
            return part.text
        if part.source is VarSource.EXPRESSION:
            raise StepFailed(f"unsupported expression '${{{part.name}}}'")
        if part.source is VarSource.PARAMS:
            if part.name not in self.params:
                raise StepFailed(f"unknown parameter 'params.{part.name}'")
            return self.params[part.name]
        if part.name not in self.env:
            raise StepFailed(f"undefined variable '{part.name}'")
        if for_shell and part.name in self.secret_names:
            return "${" + part.name + "}"
        return self.env[part.name]


def describe(value: StringValue) -> str:
    """The source form of a string (variables stay as ``${NAME}``), for labels."""
    pieces = []
    for part in value.parts:
        if isinstance(part, TextPart):
            pieces.append(part.text)
        elif part.source is VarSource.PARAMS:
            pieces.append("${params." + part.name + "}")
        else:
            pieces.append("${" + part.name + "}")
    return "".join(pieces)
