"""The tool registry: one ``Tool`` per capability, one module per area (``mcp/tools/``)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import ModuleType

from shiftcodes.api.container import AppContainer
from shiftcodes.api.principal import Principal
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import EnvironmentName, TokenScope
from shiftcodes.domain.errors import ValidationError
from shiftcodes.services.run_triggers import RunOrigin

JsonSchema = Mapping[str, object]


class Args:
    """Typed access to validated tool arguments (the JSON schema already checked the shapes)."""

    def __init__(self, values: Mapping[str, object]) -> None:
        self._values = values

    @property
    def values(self) -> Mapping[str, object]:
        """The raw arguments."""
        return self._values

    def text(self, name: str) -> str:
        """A required string."""
        value = self._values.get(name)
        if not isinstance(value, str):
            raise ValidationError(f"argument {name} must be a string")
        return value

    def optional_text(self, name: str) -> str | None:
        """An optional string."""
        value = self._values.get(name)
        return value if isinstance(value, str) else None

    def integer(self, name: str, default: int | None = None) -> int:
        """An integer argument, or ``default`` when absent."""
        value = self._values.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"argument {name} must be an integer")
        return value

    def environment(self, name: str = "environment") -> EnvironmentName:
        """A required environment name."""
        return EnvironmentName(self.text(name))

    def optional_environment(self, name: str = "environment") -> EnvironmentName | None:
        """An optional environment name."""
        value = self.optional_text(name)
        return EnvironmentName(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool handler may use: the same collaborators as a REST request, and the caller."""

    container: AppContainer
    principal: Principal

    @property
    def actor(self) -> Actor:
        """The audit actor (the API token)."""
        return self.principal.actor

    def origin(self) -> RunOrigin:
        """Attribution for runs started by this call."""
        return self.principal.run_origin()


Handler = Callable[[ToolContext, Args], object]


@dataclass(frozen=True, slots=True)
class Tool:
    """A tool: its scope, input schema and the handler that calls the platform's services.

    ``redact`` names arguments whose value is replaced by its length in the audit record (large
    texts such as a Jenkinsfile); everything else is recorded, clipped.
    """

    name: str
    description: str
    scope: TokenScope
    input_schema: JsonSchema
    handler: Handler
    read_only: bool = True
    redact: frozenset[str] = field(default_factory=frozenset)


class ToolRegistry:
    """All tools by name; names are unique."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        """Register ``tool``; a duplicate name is a programming error."""
        if tool.name in self._tools:
            raise ValueError(f"duplicate MCP tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """The tool called ``name``."""
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        """Every tool, ordered by name."""
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    def visible_to(self, principal: Principal) -> list[Tool]:
        """The tools whose scope the principal holds."""
        return [tool for tool in self.all() if principal.has_scope(tool.scope)]


def registry_from_modules(modules: Iterable[ModuleType]) -> ToolRegistry:
    """Build a registry from modules that each define ``TOOLS: tuple[Tool, ...]``."""
    registry = ToolRegistry()
    for module in modules:
        for tool in module.TOOLS:
            registry.add(tool)
    return registry
