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

## Containers (exists, Task 8)

Base: `/api/projects/{slug}/environments/{env}/containers`. Only containers of the environment's
compose project (`com.docker.compose.project` label = the environment's `compose_project`) are
visible; any other container id answers 404. `{container_id}` is always the **full 64 hex id**
(422 otherwise); names and prefixes are not identities. Every Docker operation is executed by the
worker; the API has no Docker access. Scopes: `read` = inventory, env names, stats, telemetry;
`containers` = logs, files, start/stop/restart, env edit, limits; `admin` = env reveal;
`terminal` = terminal tickets. Sessions hold every scope. Mutations need CSRF as usual.
Operations on one environment are serialized with deployments; if the environment stays busy
for more than 30 s the call answers 409 `conflict`.

`ContainerView`: `{id, name, service, image (tag), image_id (sha256:...), revision (OCI revision
label | null), commit (shiftcodes.commit label | null), state (created|running|paused|restarting|
exited|removing|dead), health (starting|healthy|unhealthy | null), restart_count, started_at |
null, exit_code | null, cpus | null, memory_bytes | null, ports: [{container_port, host_ip,
host_port}], mounts: [{type, source, destination, read_only}]}`. No env values.

- `GET ""` -> `ContainerView[]` ordered by service, name. `GET "/{id}"` -> `ContainerView`.
- `POST "/{id}/actions"` body `{action: "start"|"stop"|"restart"}` -> step one, changes nothing:
  `{action, container, effects: string[], confirmation, expires_at}`. 409 if not applicable
  (start of a running / stop of a stopped container).
- `POST "/{id}/actions/confirm"` body `{confirmation}` -> `{action, previous_state, container}`.
  The confirmation is single use, valid 60 s, bound to the principal, the environment, the exact
  container id and the action (422 otherwise). If the container was replaced meanwhile: 404.
- `GET "/{id}/logs?tail=200"` (1-1000) -> `{lines: [{timestamp, text}], cursor: {since, skip} |
  null, dropped}`. Text is secret-masked (values of credential-like env vars, `password=...`,
  bearer tokens, private keys); lines are cut at 8 KiB. Continue with
  `?since=<cursor.since>&skip=<cursor.skip>` to get only newer lines.
- `GET "/{id}/logs/stream?tail=100"` SSE (background principal, no idle-timer extension). Events:
  `log` `{lines, dropped}` (at most 500 lines per poll, polls >= 1 s apart; `dropped` counts
  skipped lines; the first event holds the tail), `error` `{message}` (container vanished; the
  stream ends), `end` after 1 hour; heartbeat comments.
- `GET "/{id}/env"` -> `[{name, sensitive, overridden, masked_value: "********"}]`.
- `POST "/{id}/env/{name}/reveal"` (admin) -> `{name, value}`; audited.
- `PUT "/{id}/env"` body `{set: {NAME: value}, remove: [NAME]}` stores a per-service managed
  override and recreates **only that service** (`up -d --no-deps --no-build`), then verifies that
  it runs (healthy if it has a health check, stable for 3 s). A stopped service stays stopped
  (created, not started). On failure the previous override file is restored, the service is
  recreated with it and the call answers 409 "previous configuration was restored".
  200 `{service, restarted, containers: ContainerView[]}` (ids change: the container is new).
  Compose-file variables can be overridden, not deleted; `remove` drops the override.
- `PUT "/{id}/limits"` body `{cpus?, memory?}` (`1.5`, `"512m"`; at least one; capped by
  `SHIFTCODES_MANAGED_MAX_CPUS` / `SHIFTCODES_MANAGED_MAX_MEMORY`, 422 above) -> `ContainerView`.
  Applied live with `docker update` and persisted in the override file so recreates keep them.
- `GET "/{id}/files?path=/app"` -> `{path, entries: [{name, kind: directory|file}], truncated}`
  (running container with `ls`; max 500 entries; credential-like names hidden).
- `GET "/{id}/files/content?path=/app/x&max_bytes=65536"` (max 262144) -> `{path, size,
  truncated, binary, text | null}`. Paths must be absolute and normalized (no `..`, `.`, empty
  parts, control characters), outside `/proc`, `/sys`, `/dev`, and not credential-like files
  (`.env*`, `*.pem`, `*.key`, `id_rsa`, `shadow`...): 422. Symlinks and directories: 422. Reads
  are audited. Done with `docker exec <id> ls` / `docker cp`, never through a shell.
- `GET "/{id}/stats"` -> `{latest, history}`; sample `{timestamp, cpu_percent, memory_used,
  memory_limit, memory_percent, net_rx, net_tx, block_read, block_write, pids}`; history is an
  in-memory ring buffer (240 samples; the worker samples every 15 s). 409 when not running.
- `GET /api/telemetry/host` (read) -> `{latest, history}`, sample `{timestamp, cpu_percent (null
  for the first sample), cpu_count, load_1, load_5, load_15, memory_total, memory_used,
  memory_available, swap_total, swap_free, disk_total, disk_used, disk_free}`. CPU/RAM come from
  the worker's `/proc`, which inside a container shows the host kernel's values (unless lxcfs is
  used); disk is `statvfs` of the worker's data directory (the data volume's file system, normally
  the host's Docker disk, not guaranteed).

Audit actions: `container.start|stop|restart`, `container.env.update` (variable names only;
outcome applied|restored|restore_failed), `container.env.reveal`, `container.limits.update`,
`container.file.read` (path), `terminal.open`, `terminal.close`.

## Terminal (exists, Task 9)

1. `POST /api/projects/{slug}/environments/{env}/containers/{id}/terminal/tickets` (scope
   `terminal`; CSRF for sessions) body `{terminal_id?}` -> `{ticket, expires_at, websocket_path:
   "/api/terminal/ws", container_id}`. The container must belong to the environment and (for a new
   terminal) run: 404/409 otherwise. The ticket is single use, valid 30 s, bound to the principal
   and the exact container id. `terminal_id` reattaches a still living terminal after a disconnect.
2. Open `wss://<host>/api/terminal/ws`. The browser sends the session cookie and `Origin` (must be
   the public origin; a session ticket without it is refused; API-token tickets need neither).
   First message (within 5 s): `{"type":"auth","ticket":"...","rows":24,"cols":80}`. Failure: close
   code 1008. Success: text `{"type":"ready","terminal_id":"..."}`.
3. Browser to server: **binary** messages = keystrokes (<= 64 KiB); text JSON
   `{"type":"resize","rows":R,"cols":C}`, `{"type":"close"}` (ends the shell), `{"type":"ping"}`.
   Server to browser: **binary** = terminal output (feed it to xterm.js); text JSON
   `{"type":"exit","code","reason"}` (reason exited|closed|idle_timeout|absolute_timeout|shutdown),
   `{"type":"error","message"}`, `{"type":"pong"}`. Close codes: 1000 normal, 1008 refused, 1013
   worker unavailable, 4401 session ended (logout or expiry, checked every 15 s).
4. Lifetime: after a disconnect the shell lives 2 minutes (`terminal_idle_seconds`) for a reattach
   (new ticket with `terminal_id`; output produced meanwhile is dropped); absolute limit 30
   minutes; at most 5 terminals (3 per user). The shell is `docker exec -it <exact id> /bin/sh` on a
   PTY in the worker. Input is rate limited (64 KiB/s); frames are sequenced and bounded. Commands
   and output are never stored or logged; the audit holds only open/close with who, container,
   reason, duration and exit code.
