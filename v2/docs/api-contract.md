# API contract used by the web UI

Status per endpoint: **exists** (shape copied from the code under `shiftcodes/api`) or
**proposed** (the frontend is built and mocked against this shape; the backend should match it).
The frontend types live in `frontend/src/api/types.ts`; the MSW mock in
`frontend/src/mocks/` implements everything below and is the executable form of this document.

## Conventions (exists)

- JSON everywhere, UTC ISO-8601 timestamps, same-origin (`/api/...`), session cookie
  (`__Host-session` over https, `shiftcodes_session` over http), sent with `credentials: include`.
- Unsafe methods (POST/PUT/PATCH/DELETE) need `X-CSRF-Token: <session.csrf_token>` and an
  `Origin` exactly equal to `SHIFTCODES_PUBLIC_ORIGIN`. The browser sets `Origin` itself.
- Errors: `{"error": {"code", "message", "diagnostics"?, "fields"?}}`.
  - `diagnostics`: `[{severity: "error"|"warning", message, line, column}]` (pipeline validation, 422).
  - `fields`: `[{loc: string[], msg: string}]` on request-validation 422 (`loc` is e.g. `["body", "username"]`).
  - Codes: `not_found` 404, `conflict` 409, `validation_error` 422, `unauthenticated` 401,
    `forbidden` 403, `rate_limited` 429 (+ `Retry-After` seconds), `worker_unavailable` 503,
    `worker_error` 502, `configuration_error` 500, `internal_error` 500.
- Any 401 (other than the login attempt itself) makes the UI go to `/login?next=<path>`.
- Statuses of runs, stages and steps: `queued | running | succeeded | failed | cancelled |
interrupted | skipped`. Finished = not queued and not running.

## Auth

### `POST /api/auth/login` (exists)

Body `{username: string (1-64), password: string (1-1024)}`, extra keys rejected.
200 `{user: {id, username}, csrf_token, expires_at}` + session cookie.
401 `unauthenticated` (same body for unknown user and wrong password), 422 with `fields`,
429 `rate_limited` with `Retry-After`.

### `GET /api/auth/session` (exists)

200 `{user: {id, username}, csrf_token, expires_at}`; 401 when there is no valid session.
The UI calls it at start and keeps `csrf_token` in memory.

### `POST /api/auth/logout` (exists)

204, clears the cookie.

## Projects (proposed)

All need scope `read` (sessions always have it). Archived projects are omitted.

### `GET /api/projects`

200 `Project[]`, ordered by name:

```json
{
  "slug": "shop-api", "name": "Shop API", "repository": "acme/shop-api",
  "created_at": "2026-09-01T10:00:00Z",
  "environments": [
    {"name": "development", "branch": "develop", "auto_deploy": true,  "last_run": RunSummary | null},
    {"name": "production",  "branch": "main",    "auto_deploy": false, "last_run": RunSummary | null}
  ]
}
```

`environments` always has exactly two entries, development first. `last_run` is the newest run
(by `created_at`) of that environment, any status.

### `GET /api/projects/{slug}`

200 `Project` (same shape). 404 `not_found`.

### `GET /api/projects/{slug}/environments/{env}/runs?limit=20&offset=0`

`env` is `development|production`. `limit` 1-100 (default 20), `offset` >= 0.
200 `{items: RunSummary[], total, limit, offset}`, newest first. 404 for unknown project.

### `GET /api/runs?limit=20&offset=0`

Same page shape across all projects and environments, newest first (the "Runs" sidebar page).

### `GET /api/projects/{slug}/environments/{env}/head?branch=<name>`

Resolves the current head of a branch so the user sees what a manual run will build.
Query parameter instead of a path segment because branch names contain `/`.
200 `{branch, commit_sha (40 hex), message (first line), author, committed_at}`;
404 `not_found` when the branch does not exist. Scope `read`.

### `POST /api/projects/{slug}/environments/{env}/runs`

Manual run. Scope `run`, CSRF. Body `{branch: string, commit_sha: string}`: the sha the user
previewed; the server must build exactly that commit (reject with 409 `conflict` if the sha is
not on that branch). Trigger is `manual`. 201 `RunSummary` (status `queued`).

### `RunSummary`

Identical to the existing `RunResponse` without `stages`:
`id, number, project (slug), environment, trigger (push|manual|redeploy), branch, commit_sha,
pipeline_source (repository|platform), status, message|null, cancel_requested_at|null,
created_at, started_at|null, finished_at|null`.
The UI shows the project slug in cross-project lists; it loads the project name from
`GET /api/projects/{slug}` for headings and breadcrumbs.

## Runs

### `GET /api/runs/{run_id}` (exists)

200 `RunResponse` = `RunSummary` + `stages`:

```json
{
  "id": 7,
  "number": 3,
  "project": "shop-api",
  "environment": "development",
  "trigger": "push",
  "branch": "develop",
  "commit_sha": "...40 hex...",
  "pipeline_source": "repository",
  "status": "running",
  "message": null,
  "cancel_requested_at": null,
  "created_at": "...",
  "started_at": "...",
  "finished_at": null,
  "stages": [
    {
      "id": 1,
      "position": 0,
      "name": "Build",
      "status": "succeeded",
      "started_at": "...",
      "finished_at": "...",
      "steps": [
        {
          "id": 1,
          "position": 0,
          "step_type": "sh",
          "label": "npm ci",
          "status": "succeeded",
          "exit_code": 0,
          "started_at": "...",
          "finished_at": "..."
        }
      ]
    }
  ]
}
```

**Proposed addition:** each stage gets `column: int | null`. Stages that run in parallel share
the same `column`; columns are drawn left to right. Today stages are only ordered by `position`,
so the UI cannot tell parallel from sequential stages. Until the backend sends `column`
(`null`/absent), the graph draws every stage as its own sequential column. A parallel branch
with sequential sub-stages is not representable with one integer; the first version may give
each such sub-stage its own column.

### `GET /api/runs/{run_id}/steps/{step_id}/log?offset=0` (exists)

200 `{offset, next_offset, size, text, complete}`; `offset` is a **byte** offset, the server caps
the chunk (`CHUNK_BYTES`); continue with `offset=next_offset`; `complete` is true when the step
finished and nothing is left. The text is already secret-masked. The UI loops until `complete`
or `next_offset == offset`.

### `GET /api/runs/{run_id}/events` (exists)

`text/event-stream`, session cookie (no idle-timer extension). Events (JSON `data`):

- `status`: a full `RunResponse`; first event, then again whenever it changes.
- `log`: `{step_id, offset, text}`; `offset` is the byte offset where `text` starts, so the end is
  `offset + utf8_length(text)`. The stream starts at offset 0 for every step, so a reconnect
  replays logs; the UI appends only chunks that continue exactly at its current end and ignores
  or re-fetches (`/log?offset=`) otherwise. Chunk boundaries may differ between connections.
- `end`: `{status}`, then the server closes. Heartbeat comment lines keep the connection alive.

The UI uses `EventSource` with credentials, lets the browser reconnect, and marks the view
**stale** (banner, dashed log border) while the connection is down. It does not open a stream
for finished runs.

### `POST /api/runs/{run_id}/cancel` (exists)

Scope `run`, CSRF. 200 `{outcome: "cancelled" | "requested", run: RunResponse}`.
`cancelled` = queued run cancelled at once; `requested` = running run flagged, it ends later
(`cancel_requested_at` set, status still `running`). The UI offers cancel only while
`queued|running`, behind a confirmation, and shows "Cancel requested" afterwards.

## Pipelines (proposed)

The browser never parses or serializes Jenkinsfile text. The server is the single source of
truth: `parse` turns text into an editing **model**, `render` turns a model back into the
canonical text (the existing serializer), `validate` reports diagnostics. Every visual edit goes
through `render`, so the stored text is always canonical. All endpoints need scope `read`
(`pipelines` for `PUT`); mutating ones need CSRF.

`Diagnostic` is the existing shape `{severity: "error"|"warning", message, line, column}`
(1-based). The editor underlines from `column` to the end of the word at that position.

### `GET /api/projects/{slug}/environments/{env}/pipeline`

200 `{source, version, jenkinsfile, updated_at, updated_by}`:

- `source: "platform"`: stored in the platform; `version` >= 1; editable.
- `source: "repository"`: a `Jenkinsfile` exists in the repository at the environment branch's
  head; `jenkinsfile` is that file's text, **read-only** (the run will use the repo's file).
  `version` is the latest platform-stored version (0 if none); stored versions stay listable.
- `source: "none"`: nothing stored, no repository file: `version: 0`, `jenkinsfile: ""`,
  `updated_at/updated_by: null`. The UI then offers a starter pipeline rendered via `render`.

### `PUT /api/projects/{slug}/environments/{env}/pipeline`

Body `{jenkinsfile, base_version}` (`base_version` = the `version` the editor started from;
0 when `source` was `none`). 200 returns the same shape as `GET` with the new version.

- 409 `conflict` when `base_version` is not the current platform version (also when the source is
  `repository`). Nothing is stored. The UI then re-reads `GET` and lets the user choose.
- 422 `validation_error` with `error.diagnostics` (existing `ValidationError` mapping).
- Saves are append-only versions (existing `PipelineDefinition`); audit as today.

### `GET .../pipeline/versions`

200 `[{version, created_at, created_by}]`, newest first (`created_by` is a username or null).

### `GET .../pipeline/versions/{version}`

200 `{version, created_at, created_by, jenkinsfile}`; 404 for an unknown version.

### `POST /api/pipelines/parse`

Body `{jenkinsfile, project?, environment?}` (the optional pair lets the validator use that
environment's secret names). 200 `{model: Model | null, diagnostics}`:
`model` is `null` when the text has **syntax errors** (it cannot be represented); semantic
errors (unknown variable, missing credential...) still return a model plus diagnostics. The UI
keeps the visual editor read-only only while `model` is `null`.

### `POST /api/pipelines/render`

Body `{model, project?, environment?}`. 200 `{jenkinsfile: string | null, diagnostics}`:
the canonical text of the model, validated like `validate`. `jenkinsfile` is `null` only if the
model is structurally unusable (wrong shape); 422 `validation_error` with `fields` for a body
that does not match the schema. Semantic problems (empty stage name, missing required argument)
must not prevent rendering: render what is there and report diagnostics, so the editor can show
the user a half-filled form.

### `POST /api/pipelines/validate`

Body `{jenkinsfile, project, environment}`. 200 `{diagnostics}` (syntax error = one diagnostic;
otherwise the validator output using that environment's secret names).

### `GET /api/pipelines/catalog`

Step definitions, generated from `steps.py`; the UI forms are built from it and nothing is
hard-coded except the preset helpers described below.

```
Catalog {
  steps:      StepDef[]      // usable inside steps { } and post { }
  options:    StepDef[]      // options { }
  parameters: StepDef[]      // parameters { }
  post_conditions: [{name: "always"|"changed"|"aborted"|"failure"|"success"|"unstable"|"cleanup",
                     description: string}]            // in Jenkins' conventional order
}
StepDef {
  name: string            // as written in the Jenkinsfile
  title: string           // plain-English label, e.g. "Build an image"   (new data)
  description: string     // one sentence for zero-knowledge users          (new data)
  takes_block: boolean    // Block.REQUIRED
  needs_container: boolean
  args: ArgDef[]          // positional args are listed by name; the model always uses names
}
ArgDef {
  name: string
  type: "string" | "int" | "bool" | "list" | "map"
  required: boolean
  default: string | number | boolean | null
  enum: string[] | null   // from `one_of(...)` checks (timeout.unit)
  min: number | null      // from int_range / positive_int
  max: number | null
  description: string     // help text                                      (new data)
  multiline: boolean      // true for sh.script
  role: "produces_image" | "uses_image" | null   // dockerBuild.image / deploy.image (+ services values)
}
```

`rejected` arguments (`sh.returnStatus`, `returnStdout`) are **omitted** from the catalogue.

### The model (JSON form of the AST)

Names mirror `shiftcodes/pipeline/ast.py`; spans are dropped. Optional things are `null`,
lists are never omitted.

```
Model    { agent: Agent|null, environment: EnvVar[], options: Call[], parameters: Call[],
           stages: Stage[], post: Post|null }
Stage    { name: string, agent: Agent|null, environment: EnvVar[], options: Call[],
           when: Condition[]|null,            // top-level conditions, all must hold
           body: Body, post: Post|null }
Body     { type: "steps",    steps: Call[] }
         | { type: "parallel", stages: Stage[] }     // parallel { stage... }
         | { type: "stages",   stages: Stage[] }     // nested sequential stages { }
Agent    { type: "any" } | { type: "none" }
         | { type: "docker", image: Str, args: Str|null }
EnvVar   { name: string, value: Str | { credentials: string } }
Call     { name: string, args: { [argName]: Value }, body?: Call[] }
           // steps, options and parameters. `args` is keyed by the catalogue arg name
           // (positional arguments are normalised to names). `body` only for takes_block steps.
Condition{ type: "branch", pattern: Str }
         | { type: "environment", name: string, value: Str }
         | { type: "not", condition: Condition }
         | { type: "allOf" | "anyOf", conditions: Condition[] }
Post     { blocks: [{ condition: PostCondition, steps: Call[] }] }
Value    = string                       // literal text, rendered single-quoted
         | { template: string }         // double-quoted; ${NAME}, $NAME, ${env.NAME}, ${params.NAME} are variables
         | number | boolean             // int / true|false
         | Value[]                      // list
         | { map: [{ key: string, value: Value }] }   // ordered map; [:] = {map: []}
Str      = string | { template: string }
```

Rules: a string without variables is always the plain JSON string (also when the source used
double quotes); the server decides `template` on `parse` and the serializer picks the quote
style on `render`. The UI sets `{template}` only when the user ticks "Substitute variables",
because single-quoted strings must stay literal (shell `$HOME` in `sh` scripts). Unknown
steps/args are preserved as given (`name`, `args`) so the UI can show "not in the catalogue"
and keep them untouched. Comments are not part of the model (same as the serializer today).

### UI behaviour the backend should know about

- **Presets** are composed by the UI from model parts: `when` presets use `branch` /
  `environment` (name `ENVIRONMENT`, value `production|development`) / `not{branch}`; `post`
  notification presets add an `echo` with the template `Build ${BUILD_NUMBER} failed on
${BRANCH_NAME}` to `failure` / `success`, and `cleanup`. A condition tree that matches no
  preset is shown as "custom" and kept as is.
- **Built images** for the agent / `deploy.image` pickers are collected in the browser from
  `args[<arg with role produces_image>]` of every call in the model.
- **Secret names** for the credentials picker come from the existing
  `GET /api/projects/{slug}/environments/{env}/secrets` (names only).
- Sync: Visual -> Text: model -> `render` (debounced 250 ms) -> text replaces the buffer.
  Text -> Visual: text -> `parse` on entering the Visual tab. Text tab: `validate` 400 ms after
  typing stops.

## Database (exists)

The database inspector. Base path `/api/projects/{slug}/environments/{env}/databases`. The API holds no
database password and never connects: the worker runs a short-lived, hardened helper container
(`docker/inspector.Dockerfile`: read-only root, no capabilities, non-root user, memory/CPU/pid limits,
30 s wall clock) attached **only** to the environment's compose network `<compose_project>_default`.
The helper refuses any host that resolves outside that network's subnets or to its gateway (the host).
The password is stored as environment secret `DBCONN_<NAME>_PASSWORD` (encrypted, worker-only key) and
is never returned. Every session is read-only (`default_transaction_read_only` + `READ ONLY`
transaction on PostgreSQL; `SET SESSION TRANSACTION READ ONLY` + `START TRANSACTION READ ONLY` on
MySQL/MariaDB) and rolled back. UI copy for production: *use a dedicated database user that only has
SELECT privileges; read-only sessions are defence in depth, the user is the strongest guarantee.*

Scopes: configuration (create/patch/delete/test) `admin`; list/get connection and metadata (schemas,
tables, table detail) `read` on both environments; **rows and console: `read` on development, `admin`
on production** (sessions hold every scope). Errors use the common envelope; specifics: 409
`conflict` = database unreachable or environment network missing, 422 `validation_error` = statement
rejected / failed / timed out or bad paging input, 404 = unknown connection/schema/table.

### Connection

```json
{"name": "main", "engine": "postgresql|mysql|mariadb", "host": "db", "port": 5432, "database": "app",
 "username": "reader", "tls_mode": "disable|require", "password_secret": "DBCONN_MAIN_PASSWORD",
 "read_only": true, "environment": "development", "notice": null,
 "created_at": "...", "updated_at": "...", "last_tested_at": null, "last_test_ok": null}
```

`host` is a compose service name (or other name/IP on the environment network; loopback, unspecified
and link-local are refused). `tls_mode=require` encrypts without verifying the certificate (no CA store
yet). `notice` is set on production (the read-only-user recommendation). Max 10 connections per
environment; `name` is `[a-z0-9][a-z0-9-]{0,47}`.

- `GET .../databases` (read) -> `Connection[]`; `GET .../databases/{name}` (read) -> `Connection`
- `POST .../databases` (admin) body `{name, engine, host, port?, database, username, password, tls_mode?}`
  -> 201 `Connection`; 409 on duplicate name/limit
- `PATCH .../databases/{name}` (admin) any of `engine, host, port, database, username, password, tls_mode`
  (a `password` rotates the secret) -> `Connection`
- `DELETE .../databases/{name}` (admin) -> 204 (also deletes the password secret)
- `POST .../databases/{name}/test` (admin) -> always 200:
  `{"ok": true, "server": {version, read_only, current_user, database, tls}, "error": null, "network": "proj_default"}`
  or `{"ok": false, "error": {"code": "connect_failed|network_denied|network_unavailable|secret_missing|helper_unavailable|timeout|...", "message"}, "server": null, "network"}`;
  updates `last_tested_at` / `last_test_ok`

### Metadata (read)

- `GET .../{name}/schemas?include_system=false` -> `{"schemas": [{"name", "system"}]}`
- `GET .../{name}/schemas/{schema}/tables` -> `{"tables": [{"schema", "name", "kind": "table|view|materialized_view|partitioned_table|foreign_table|sequence", "approx_rows": int|null}]}`
  (`approx_rows` is the planner/engine estimate)
- `GET .../{name}/schemas/{schema}/tables/{table}` -> `{"table", "columns": [{name, position, data_type, nullable, default, primary_key_position, extra}], "indexes": [{name, columns, unique, primary, method}], "foreign_keys": [{name, columns, ref_schema, ref_table, ref_columns, on_update, on_delete}]}`

Table names containing `/` cannot be addressed (path segment); all other names, including hostile ones,
are quoted by the driver / catalog-verified and never spliced from the URL.

### Rows (`read` dev, `admin` production)

`GET .../{name}/schemas/{schema}/tables/{table}/rows?limit=100&offset=0&cursor=`

`limit` 1-500 (default 100). Ordered by primary key (keyset paging); tables without a primary key are
ordered by their sortable columns (`ordering: "columns"`) or unordered (`"none"`) and only page by
`offset`. `cursor` (opaque, from a previous `next_cursor`) wins over `offset`.

```json
{"columns": [{"name": "id", "type": "integer"}], "rows": [[1, "x"]], "limit": 100, "offset": 0,
 "ordering": "primary_key|columns|none", "order_by": ["id"],
 "next_cursor": "WzJd" | null, "next_offset": 100 | null, "size_limited": false}
```

Cells are JSON scalars (integers beyond 2^53 and all decimals/dates/uuids/json/arrays are strings), or:
`{"truncated": true, "text": "<first 4096 bytes>", "limit": 4096}` for long text and
`{"binary": true, "hex": "<first 64 bytes as hex>", "bytes": <total length>}` for binary.
A page is cut (and `size_limited` is true) if it would exceed ~640 KiB; continue with the next cursor.
Reads of production rows are audited (`database.rows.read`: table and page, never data).

### Console (`read` dev, `admin` production)

`POST .../{name}/query` body `{"statement": "SELECT ..."}` (max 20,000 chars) ->

```json
{"columns": [{"name", "type"}], "rows": [[...]], "row_count": 1, "truncated": false, "size_limited": false,
 "limit": 500, "elapsed_ms": 3, "statement_kind": "SELECT|WITH|SHOW|EXPLAIN"}
```

One statement only (an optional final `;`), starting with SELECT, WITH, SHOW or EXPLAIN. Rejected (422):
chained statements, INSERT/UPDATE/DELETE/MERGE/DDL keywords anywhere (except SHOW), `INTO`
(OUTFILE/DUMPFILE/variables), locking reads, `EXPLAIN ANALYZE`, executable comments, and a function
deny-list (`pg_sleep*`/`sleep`/`benchmark`, locks, server file access, `dblink`, `set_config`,
`nextval`, ...). 5 s server-side statement timeout, 500 rows (`truncated: true` beyond), cells as above,
always rolled back. Audited as `database.query` with the statement text and outcome
(`ok|rejected|timeout|...`) and row count, **never the results**.

### MCP-friendly notes

All shapes are flat JSON with stable keys; there is no HTML or driver-specific type. A read-only agent
token (`read`) can enumerate schemas/tables/columns and read/query development data; production data
needs an `admin` token.
