"""``when`` evaluation against the run context."""

from __future__ import annotations

import re
from functools import lru_cache

from backend.execution.scope import Scope
from backend.pipeline.ast import (
    AllOfCondition,
    AnyOfCondition,
    BranchCondition,
    Condition,
    EnvironmentCondition,
    NotCondition,
    When,
)


@lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile an Ant-style glob: ``*`` stays inside a path segment, ``**`` crosses them."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
        index += 1
    return re.compile("".join(out))


class WhenEvaluator:
    """Evaluates a ``when`` block for a branch and the variables of a scope."""

    def __init__(self, branch: str) -> None:
        self._branch = branch

    def matches(self, when: When | None, scope: Scope) -> bool:
        """Whether the stage should run; several top-level conditions mean all of them."""
        if when is None:
            return True
        return all(self._holds(condition, scope) for condition in when.conditions)

    def _holds(self, condition: Condition, scope: Scope) -> bool:
        if isinstance(condition, BranchCondition):
            pattern = condition.pattern.plain or ""
            return glob_regex(pattern).fullmatch(self._branch) is not None
        if isinstance(condition, EnvironmentCondition):
            return scope.env.get(condition.name) == (condition.value.plain or "")
        if isinstance(condition, NotCondition):
            return not self._holds(condition.condition, scope)
        if isinstance(condition, AllOfCondition):
            return all(self._holds(inner, scope) for inner in condition.conditions)
        if isinstance(condition, AnyOfCondition):
            return any(self._holds(inner, scope) for inner in condition.conditions)
        raise TypeError(f"unknown condition: {condition!r}")
