"""Small builders for the JSON Schemas of tool inputs (every schema forbids extra keys)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from backend.mcp.registry import JsonSchema

SLUG: JsonSchema = {"type": "string", "pattern": "^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"}
ENVIRONMENT: JsonSchema = {"type": "string", "enum": ["development", "production"]}
BRANCH: JsonSchema = {"type": "string", "minLength": 1, "maxLength": 200}
COMMIT_SHA: JsonSchema = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
RUN_ID: JsonSchema = {"type": "integer", "minimum": 1}
JENKINSFILE: JsonSchema = {"type": "string", "maxLength": 262144}
LIMIT: JsonSchema = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
OFFSET: JsonSchema = {"type": "integer", "minimum": 0, "default": 0}


def paging(sorts: Sequence[str] = (), default_per_page: int = 20) -> dict[str, JsonSchema]:
    """The list parameters every paged tool takes (``Args.page_query`` reads them).

    ``limit`` and ``offset`` stay accepted for older agents but are marked deprecated.
    """
    properties: dict[str, JsonSchema] = {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": default_per_page},
        "q": {"type": "string", "maxLength": 100, "description": "search text"},
        "limit": {**LIMIT, "deprecated": True, "description": "use per_page"},
        "offset": {**OFFSET, "deprecated": True, "description": "use page"},
    }
    if sorts:
        properties["sort"] = {"type": "string", "enum": list(sorts)}
        properties["order"] = {"type": "string", "enum": ["asc", "desc"]}
    return properties


def obj(
    properties: Mapping[str, JsonSchema] | None = None, required: Sequence[str] = ()
) -> JsonSchema:
    """An object schema without additional properties."""
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }


def described(schema: JsonSchema, description: str) -> JsonSchema:
    """``schema`` with a description the agent sees."""
    return {**schema, "description": description}
