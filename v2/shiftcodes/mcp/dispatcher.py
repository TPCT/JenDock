"""Running a tool call: scope check, input validation, the handler, error mapping and audit."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from enum import StrEnum

import jsonschema  # type: ignore[import-untyped]  # no stubs; used for validation only
from mcp import types

from shiftcodes.api.container import AppContainer
from shiftcodes.api.errors import describe_domain_error, diagnostic_body
from shiftcodes.api.principal import Principal
from shiftcodes.domain.audit import AuditTarget
from shiftcodes.domain.errors import DomainError
from shiftcodes.mcp.registry import Args, Tool, ToolContext, ToolRegistry
from shiftcodes.services.audit import AuditService

logger = logging.getLogger(__name__)
AUDIT_ACTION = "mcp.tool_call"
MAX_ARG_TEXT = 200
MAX_TOOL_NAME = 64


class Outcome(StrEnum):
    """How a call ended (recorded in the audit event)."""

    OK = "ok"
    DENIED = "denied"  # unknown tool, or the token lacks the scope
    INVALID = "invalid"  # arguments do not match the tool's schema
    ERROR = "error"  # the platform refused or failed (not found, conflict, validation...)


def audit_arguments(tool: Tool | None, arguments: Mapping[str, object]) -> dict[str, object]:
    """The arguments as recorded: scalars clipped, redacted ones reduced to their length.

    Nothing here is a secret: tools take no secret values, and large texts are not stored.
    """
    recorded: dict[str, object] = {}
    redact = tool.redact if tool else frozenset[str]()
    for key, value in list(arguments.items())[:20]:
        if key in redact and isinstance(value, str):
            recorded[key[:40]] = f"<{len(value)} characters>"
        elif isinstance(value, str):
            recorded[key[:40]] = value[:MAX_ARG_TEXT]
        elif isinstance(value, bool | int | float) or value is None:
            recorded[key[:40]] = value
        else:
            recorded[key[:40]] = "<omitted>"
    return recorded


def _text_result(payload: object, *, is_error: bool) -> types.CallToolResult:
    structured = payload if isinstance(payload, dict) else {"items": payload}
    text = json.dumps(structured, ensure_ascii=False, default=str)
    return types.CallToolResult(
        content=[types.TextContent(text=text)],
        structured_content=structured,
        is_error=is_error,
    )


def _error_result(code: str, message: str, **extra: object) -> types.CallToolResult:
    return _text_result({"error": {"code": code, "message": message, **extra}}, is_error=True)


class ToolDispatcher:
    """Lists the tools a token may use and executes calls on the platform's own services."""

    def __init__(self, registry: ToolRegistry, container: AppContainer) -> None:
        self._registry = registry
        self._container = container

    def list_tools(self, principal: Principal) -> list[types.Tool]:
        """The tools whose scope the token holds, with their schemas."""
        return [
            types.Tool(
                name=tool.name,
                description=tool.description,
                input_schema=dict(tool.input_schema),
                annotations=types.ToolAnnotations(
                    read_only_hint=tool.read_only,
                    destructive_hint=False,
                    idempotent_hint=tool.read_only,
                    open_world_hint=False,
                ),
            )
            for tool in self._registry.visible_to(principal)
        ]

    def call(
        self, principal: Principal, name: str, arguments: Mapping[str, object] | None
    ) -> types.CallToolResult:
        """Execute one call and audit it (also when it is denied or fails)."""
        values = arguments or {}
        tool = self._registry.get(name)
        outcome, result = self._execute(principal, tool, values)
        self._audit(principal, name, tool, values, outcome)
        return result

    def _execute(
        self, principal: Principal, tool: Tool | None, values: Mapping[str, object]
    ) -> tuple[Outcome, types.CallToolResult]:
        if tool is None or not principal.has_scope(tool.scope):
            # The same answer for an unknown tool and a missing scope: nothing to probe.
            return Outcome.DENIED, _error_result("forbidden", "This tool is not available.")
        errors = sorted(
            jsonschema.Draft202012Validator(dict(tool.input_schema)).iter_errors(values),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            where = ".".join(str(part) for part in errors[0].absolute_path) or "arguments"
            message = f"invalid input at {where}: {errors[0].validator} constraint not met"
            return Outcome.INVALID, _error_result("invalid_input", message)
        try:
            payload = tool.handler(ToolContext(self._container, principal), Args(values))
        except DomainError as error:
            return Outcome.ERROR, _domain_failure(error)
        except Exception:
            logger.exception("mcp tool failed", extra={"tool": tool.name})
            return Outcome.ERROR, _error_result("internal_error", "internal server error")
        return Outcome.OK, _text_result(payload, is_error=False)

    def _audit(
        self,
        principal: Principal,
        name: str,
        tool: Tool | None,
        values: Mapping[str, object],
        outcome: Outcome,
    ) -> None:
        details: dict[str, object] = {
            "tool": name[:MAX_TOOL_NAME],
            "outcome": outcome.value,
            "arguments": audit_arguments(tool, values),
        }
        try:
            AuditService(self._container.uow(), self._container.clock).record(
                principal.actor,
                AUDIT_ACTION,
                AuditTarget("mcp_tool", name[:MAX_TOOL_NAME]),
                details,
            )
        except Exception:
            logger.exception("could not audit an mcp tool call", extra={"tool": name[:64]})


def _domain_failure(error: DomainError) -> types.CallToolResult:
    described = describe_domain_error(error)
    extra: dict[str, object] = {}
    if described.diagnostics:
        extra["diagnostics"] = [diagnostic_body(item) for item in described.diagnostics]
    return _error_result(described.code, described.message, **extra)
