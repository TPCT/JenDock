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

**Optional log timestamps (proposed):** if each log line starts with an RFC 3339 timestamp followed by a
space (`2026-01-15T10:00:01.250Z text`, as `docker logs -t` writes), the UI hides it from the text and
shows it in the gutter when the user turns on "Show timestamps". Without them the toggle is disabled.

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

## Pipelines (implemented)

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

### Details of the implementation (status: exists)

- **Context pair.** `project` and `environment` of `parse`/`render`/`validate` must be given
  together (422 otherwise); an unknown project is 404. Without the pair the validator does not
  judge `credentials('NAME')` (an editor without a project cannot know the secret names), every
  other check still runs. With the pair, the secret names of that environment are used.
- **Size.** Pipeline text is limited to 256 KiB (422 `validation_error`).
- **Repository detection.** `GET .../pipeline` asks the worker (`pipeline.repository_file`) for the
  `Jenkinsfile` at the head of the environment's branch. If git cannot read the repository at all
  the worker answers "unreachable" and the API falls back to the stored pipeline (`platform` or
  `none`); a run decides again at its own commit. If the worker itself is down the answer is 503
  `worker_unavailable`, not a guess. `PUT` while the repository has a `Jenkinsfile` is 409.
- **`updated_at`/`updated_by`** are `null` for `source: "repository"`. `updated_by` is the user
  name of the session that saved; versions saved with an API token have `created_by: null`.
- **Template strings.** In `{template: ...}` a literal `$` is written `\$` and a literal backslash
  `\\`; every other backslash stands for itself. A lone `$` or an unterminated `${` stays literal
  (a half-typed template still renders). Variables are always rendered as `${NAME}`,
  `${env.NAME}`, `${params.NAME}`.
- **Unnamed arguments.** Arguments of unknown steps that have no name are keyed `$0`, `$1`, ...
  in `args`, so they survive a round trip. If an argument is written twice in one call the model
  keeps the first one (the diagnostics of the original text still report the duplicate).
- **Catalogue.** `deploy.services` has `role: "uses_image"` too (its map values are images).
  Argument order and step order follow `steps.py`.

### `GET /api/pipelines/templates` (status: exists)

Scope `read`. 200 `[{name, title, description, jenkinsfile}]` for Laravel (`laravel`), Next.js
(`nextjs`), Node API (`node-api`), Python/FastAPI (`python-fastapi`), static site (`static-site`)
and any project with a Dockerfile (`dockerfile`). Every template validates without errors and is
accepted by a real Jenkins linter (`make lint-jenkins`). They use placeholder service names and
addresses; the comment at the top of each says what to change.

### `POST /api/projects/{slug}/environments/{env}/pipeline/dry-run` (status: exists)

Scope `read`, CSRF. Body `{jenkinsfile?: string | null, branch?: string | null}`: unsaved text (else
the pipeline in force: repository file, else the stored one; 422 when there is none) and the
branch to plan for (else the environment's branch). Nothing is executed, no secret is read.

200 `{valid, diagnostics, plan}`. `valid` is false when there are syntax or validation errors;
`plan` is then `null`. Otherwise:

```
plan { branch, environment, agent: string|null,       // "any", "none", "docker <image>"
       stages: DryStage[], post: DryPost[] }
DryStage { name, path,                                 // path = "Parent / Child"
           will_run: bool, skip_reason: string|null,   // evaluated like a real run (`when`)
           agent: string|null, mode: "steps"|"parallel"|"stages",
           steps: DryStep[], stages: DryStage[], post: DryPost[] }
DryStep  { name, label, children: DryStep[] }          // label as shown in a run, e.g. "sh: npm ci"
DryPost  { condition, steps: DryStep[] }
```

Stages inside a skipped stage are `will_run: false` with an explanatory `skip_reason`.
`environment` conditions see the built-in variables (`ENVIRONMENT`, `BRANCH_NAME`, `PROJECT`...)
and the literal values of `environment` sections; credentials are never read.

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

## Domains (exists, Task 11)

A **domain** routes a public hostname to one service port of an environment: Cloudflare DNS
record, Apache vhost (reverse proxy to `127.0.0.1:<published_port>`) and a TLS certificate.
Hostnames are unique across all projects and environments. Reads need scope `read`; every
mutation needs scope `admin` (plus CSRF for sessions) and is audited. Design and operations:
`docs/ingress.md`.

New error code: `upstream_error` 502 (Cloudflare, the Apache host helper or the ACME CA refused
or failed; unlike `worker_error` the `message` is meant to be shown, e.g. `Cloudflare rejected
the API token (check its scope)` or `configtest failed, change reverted: <apache output>`).
`conflict` 409 is also returned when a DNS record that Shiftcodes did not create is in the way,
and when an apply/remove `plan_hash` is stale.

```json
Domain {
  "id": 7, "hostname": "app.example.com", "service": "web",
  "container_port": 8080, "published_port": 20001,
  "dns_record_type": "A" | "AAAA" | "CNAME", "dns_target": "203.0.113.7" | null,
  "dns_proxied": false, "tls_enabled": true,
  "status": "draft" | "active" | "error", "status_message": string | null,
  "applied_at": ts | null, "last_verified_at": ts | null, "last_verified_ok": bool | null,
  "created_at": ts, "updated_at": ts
}
DomainDetail = Domain + { "certificate": Certificate | null, "check": Check | null }
Certificate { "not_after": ts, "days_remaining": 87,
              "state": "ok" | "renewal_due" | "warning" | "critical" | "expired",
              "last_attempt_at": ts | null, "last_error": string | null }
Check { "checked_at": ts, "ok": bool,
        "results": { "dns":     {ok, detail, checked_at, addresses, expected?},
                     "https":   {ok, detail, checked_at, status_code?, certificate_not_after?},
                     "backend": {ok, detail, checked_at, port} } }
```

`dns_target: null` means "the server's public address" from the deployment settings (A/AAAA);
a CNAME always needs an explicit target. `published_port` is the host loopback port the
container publishes (1024-65535); it must also lie in the host helper's allowlisted range.
`status`: `draft` = defined only, `active` = applied, `error` = the last apply failed.
Certificate `state`: `renewal_due` = inside the 30 day window (renewal runs in the worker),
`warning` < 14 days left, `critical` < 3 days, `expired`.

### `GET /api/projects/{slug}/environments/{env}/domains`

Scope `read`. 200 `DomainDetail[]` ordered by hostname.

### `POST /api/projects/{slug}/environments/{env}/domains`

Scope `admin`. Body (extra keys rejected):
`{hostname, service, container_port, published_port, dns_record_type?="A", dns_target?,
dns_proxied?=false, tls_enabled?=true}`. 201 `Domain` in status `draft`; **nothing is applied**.
`hostname` is normalized (lowercase, IDNA `xn--` labels, trailing dot dropped). 422
`validation_error` (invalid name, IP literal, wildcard unless enabled by the operator, port out
of range, service not in the environment's service list, target not matching the record type),
409 `conflict` when the hostname is taken anywhere.

### `GET .../domains/{id}`

Scope `read`. 200 `DomainDetail`; 404 for an id that belongs to another environment.

### `PATCH .../domains/{id}`

Scope `admin`. Body: any of `service, container_port, published_port, dns_record_type,
dns_target (null resets to the server default), dns_proxied, tls_enabled`. 200 `Domain`. The
hostname cannot change (delete and recreate). Changes reach DNS/Apache only through
preview + apply.

### `DELETE .../domains/{id}`

Scope `admin`. 204 only for `draft` domains; otherwise 409 (remove it first, below).

### `POST .../domains/{id}/preview`

Scope `admin` (it uses the Cloudflare token). Body `{operation?: "apply" | "remove" = "apply"}`.
Read-only toward Cloudflare/Apache (GET requests and a dry-run render). 200:

```json
Plan {
  "domain_id": 7, "hostname": "app.example.com", "operation": "apply", "blocked": false,
  "dns":   { "action": "create"|"update"|"delete"|"noop"|"conflict", "zone": "example.com",
             "current": Record | null, "desired": Record | null, "removes": Record[],
             "message": "" },
  "vhost": { "action": "create"|"update"|"delete"|"noop", "current": string | null,
             "proposed": string | null, "diff": "unified diff, current -> proposed" },
  "tls":   { "action": "issue"|"keep"|"remove"|"none", "challenge": "dns-01"|"http-01"|null,
             "not_after": ts | null },
  "steps": ["DNS: create", "Apache: HTTP vhost, then reload", "TLS: issue certificate (dns-01)", ...],
  "plan_hash": "<64 hex>"
}
Record { "record_type", "name", "content", "proxied" }
```

`blocked: true` (`dns.action: "conflict"`) means a record not created by Shiftcodes exists at
that name; apply is refused and nothing is touched. Cloudflare errors (no token secret, token
rejected, no zone) come back as 404 (missing `CLOUDFLARE_API_TOKEN` secret of the environment),
500 `configuration_error` (no server address configured) or 502 `upstream_error`.

### `POST .../domains/{id}/apply`

Scope `admin`. Body `{plan_hash}` (exactly the value of the preview the user reviewed). The
server recomputes the plan and returns 409 `conflict` if it differs, so the shown diff is the
applied diff. 200 returns the applied `Plan`. Order: DNS record; HTTP vhost; certificate
(ACME, only when needed); TLS vhost. On failure the domain becomes `error` with
`status_message`, the response is 502 `upstream_error`, and Apache is back on its previous
configuration (the helper reverts a change that fails `configtest` or reload). The call can
take minutes (certificate issuance); the UI should show progress and not time out before
about 5 minutes.

### `POST .../domains/{id}/remove`

Scope `admin`. Body `{plan_hash}` from a `preview` with `operation: "remove"`. Removes the
vhost, certificate files/row and only the DNS records Shiftcodes created; the domain returns
to `draft`. 200 the applied `Plan`.

### `POST .../domains/{id}/check`

Scope `admin` (it makes outbound requests and stores the result). 200 `Check`. DNS must
resolve to the server (for proxied records: resolve at all), HTTPS must answer < 500 with a
valid certificate for the hostname (plain HTTP when `tls_enabled` is false), and the backend
port must accept connections on the host. The result and its time are stored and returned by
`GET .../domains/{id}` as `check`, and `last_verified_*` on the domain are updated.

### `POST .../domains/{id}/certificate`

Scope `admin`. Issue or renew the certificate now (domain must be `active` with
`tls_enabled`; otherwise 409). 200 `Certificate` (`last_*` fields are null). Renewal otherwise
happens automatically 30 days before expiry.

### `GET /api/ingress/certificates`

Scope `read`. Expiry monitoring: 200 `[{domain_id, hostname} + Certificate]`, soonest expiry first.

## Serving the UI (status: exists)

When `SHIFTCODES_FRONTEND_DIST` points to a Vite `dist` directory (the API image sets it) the API
serves it: static files at `/`, any other `GET` that is not below `/api` or `/healthz` and does not
look like a missing file (`.js`, `.css`, `.png`...) answers `index.html` (client-side routes).
Unknown `/api/*` paths, missing `/assets/*` files and missing files with a file suffix keep the JSON
error envelope (404 `not_found`). `/assets/*` (content-hashed) is sent with
`Cache-Control: public, max-age=31536000, immutable`; everything else keeps `no-store`. The
Content-Security-Policy is unchanged (`default-src 'self'; frame-ancestors 'none'; base-uri 'none'`):
the built UI, including CodeMirror in its shadow root, runs under it.
