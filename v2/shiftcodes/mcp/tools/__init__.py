"""Tool modules. Each defines ``TOOLS``; add a module here to add an area (one module each)."""

from __future__ import annotations

from types import ModuleType

from shiftcodes.mcp.registry import ToolRegistry, registry_from_modules
from shiftcodes.mcp.tools import deploy, domains, pipelines, projects, runs


def tool_modules() -> tuple[ModuleType, ...]:
    """Every tool module; containers, databases and domains join here when those tasks merge."""
    return (projects, runs, pipelines, domains, deploy)


def default_registry() -> ToolRegistry:
    """The registry of all platform tools."""
    return registry_from_modules(tool_modules())
