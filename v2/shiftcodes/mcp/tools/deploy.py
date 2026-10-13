"""Deployments and rollbacks: STUB until Task 7 (deployment) is merged.

The planned tools, to be implemented here with scope ``deploy`` on the same services the REST
endpoints use, are listed in ``PLANNED_TOOLS``. Until then none is registered or advertised.
"""

from __future__ import annotations

from shiftcodes.mcp.registry import Tool

PLANNED_TOOLS = ("list_deployments", "redeploy", "rollback")
TOOLS: tuple[Tool, ...] = ()
