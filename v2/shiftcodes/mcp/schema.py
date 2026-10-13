"""Small builders for the JSON Schemas of tool inputs (every schema forbids extra keys)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from shiftcodes.mcp.registry import JsonSchema

SLUG: JsonSchema = {"type": "string", "pattern": "^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"}
ENVIRONMENT: JsonSchema = {"type": "string", "enum": ["development", "production"]}
BRANCH: JsonSchema = {"type": "string", "minLength": 1, "maxLength": 200}
COMMIT_SHA: JsonSchema = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
RUN_ID: JsonSchema = {"type": "integer", "minimum": 1}
JENKINSFILE: JsonSchema = {"type": "string", "maxLength": 262144}
LIMIT: JsonSchema = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
OFFSET: JsonSchema = {"type": "integer", "minimum": 0, "default": 0}


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
