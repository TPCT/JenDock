"""Tool modules. Each defines ``TOOLS``; add a module here to add an area (one module each)."""

from __future__ import annotations

from types import ModuleType

from backend.mcp.registry import ToolRegistry, registry_from_modules
from backend.mcp.tools import (
    containers,
    databases,
    deploy,
    domains,
    environments,
    pipelines,
    projects,
    runs,
)


def tool_modules() -> tuple[ModuleType, ...]:
    """Every tool module; containers, databases and domains join here when those tasks merge."""
    return (projects, runs, pipelines, domains, deploy, environments, containers, databases)


def default_registry() -> ToolRegistry:
    """The registry of all platform tools."""
    return registry_from_modules(tool_modules())
