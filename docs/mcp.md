# MCP server for AI agents

The API process serves a [Model Context Protocol](https://modelcontextprotocol.io) endpoint at
`POST /mcp` (official `mcp` SDK 2.3.0, streamable HTTP, stateless JSON responses). It is mounted in
the API instead of being a separate service: one process and one database, and the tools call
the **same services as the REST routers** (the response shapes are the REST schemas), so no logic
is duplicated and nothing is reachable that the API itself could not do. `JENDOCK_MCP_ENABLED=false`
removes the route.

## Authentication and scopes

- Only `Authorization: Bearer jdt_...` (the API tokens described in `security.md`) is accepted; cookies are never read,
  so a browser session cannot reach `/mcp`. A missing or invalid token answers 401 with
  `WWW-Authenticate: Bearer`.
- A request with an `Origin` header must carry exactly `JENDOCK_PUBLIC_ORIGIN` (403 otherwise);
  agents normally send none. The SDK also checks the `Host` header against the public origin's host
  plus `JENDOCK_MCP_ALLOWED_HOSTS` (comma separated; set it when a reverse proxy changes the
  Host header, e.g. `127.0.0.1:8000`).
- `tools/list` shows only the tools whose scope the token holds and whose action its owner could
  ever be allowed, and `tools/call` re-checks the scope (an unknown tool and a missing scope give
  the same `forbidden` answer). `admin` implies every scope. Before a handler runs the dispatcher
  asks the same policy as REST (`backend/mcp/access.py` maps every tool to an action and to what
  its arguments point at): a token of a member can only use tools on projects, environments and
  containers granted to that member, `list_projects`, `list_runs` and `list_containers` only
  return what they may see, and refusals are audited as `denied`.

## Tools

| Scope | Tools |
| --- | --- |
| `read` | `list_projects`, `get_project`, `list_secret_names` (names only), `list_runs`, `get_run`, `read_step_log`, `preview_commit`, `get_pipeline`, `list_pipeline_versions`, `get_pipeline_version`, `validate_pipeline`, `dry_run_pipeline`, `get_pipeline_catalog`, `get_pipeline_templates` |
| `pipelines` | `save_pipeline` (needs the `base_version` you read; validated first) |
| `run` | `trigger_run`, `retry_run`, `cancel_run` |
| `read` (domains) | `list_domains` (status, certificate state and expiry, latest check), `get_domain`, `list_certificates` |
| `admin` (domains) | `check_domain`, `preview_domain_change` (DNS and vhost plan with its `plan_hash`; nothing changes), `apply_domain` / `remove_domain` (only with the previewed `plan_hash`), `issue_certificate`; scopes mirror the REST routes |
| `read` (deployments) | `list_deployments` |
| `read` (containers) | `list_containers`, `get_container`, `container_stats`, `host_telemetry`, `list_container_env_names` (names and flags only, never a value) |
| `read` (databases) | `list_database_connections`, `get_database_connection` (never a password), `list_database_schemas`, `list_database_tables`, `get_database_table`, and `read_table_rows` (opaque `next_cursor`) / `run_readonly_query`, which need `read` on development but `admin` on production exactly as in REST. Connection create, change, delete and test are operator-only and not available over MCP |
| `containers` | `tail_container_logs` (bounded tail, masked; reading logs needs this scope in REST too), `preview_container_action` then `confirm_container_action` (start/stop/restart with the worker's single-use confirmation), `update_container_limits`. Deliberately not available over MCP: terminal, live log follow, env reveal, env edit and the container file browser (agents must not read or change secrets or open a shell) |
| `deploy` | `preview_redeploy` (images that would be deployed, images running now, whether a deployment is in progress), `redeploy` (same `deployment_id`; refused while a deployment of the environment is in progress). There is no rollback tool because the REST API has no rollback endpoint (failed deployments roll back automatically) |

A manual run is two steps: `preview_commit` shows the branch head (sha, message, author, date) and
`trigger_run` must be given exactly that `commit_sha`; it fails with `conflict` when the branch
moved in between. There is no tool to reveal, set or delete a secret, and step logs are the
already-masked ones. Failed calls return `isError` results `{"error": {"code", "message",
"diagnostics"?}}` using the REST error codes.

Adding an area is one module in `backend/mcp/tools/` that defines `TOOLS` (a tuple of `Tool`
with scope, JSON Schema and a handler that uses `ToolContext.container`), registered in
`tools/__init__.py`; containers, database and domain tools join there when those tasks merge.

## Audit

Every `tools/call` writes an audit event `mcp.tool_call` with the **token** as actor
(`ActorType.TOKEN`, the token id), target `mcp_tool/<name>`, and details `{tool, outcome, arguments}`
where outcome is `ok`, `denied`, `invalid` or `error`. Arguments are recorded clipped to 200
characters; a Jenkinsfile is recorded only as its length. Mutations are additionally audited by the
services themselves (`run.create`, `run.cancel`, pipeline saves) with the same token identity.

## Using it

1. In the UI (session only; a token can never manage tokens) create a token under API tokens,
   with the smallest set of scopes the agent needs (for example `read` + `run`), and copy the
   `jdt_...` value, which is shown once. Revoke it there when the agent is done.
2. Point the client at `https://<host>/mcp` with the header `Authorization: Bearer <token>`:

   ```
   claude mcp add --transport http jendock https://<host>/mcp \
     --header "Authorization: Bearer jdt_..."
   ```

   Other clients take the same two values in their server configuration, for example
   `{"mcpServers": {"jendock": {"type": "http", "url": "https://<host>/mcp",
   "headers": {"Authorization": "Bearer jdt_..."}}}}`.
3. Check with `tools/list`: the tools shown are exactly those the token's scopes allow.
