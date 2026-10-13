# API contract used by the web UI

Status per endpoint: **exists** (shape copied from the code under `backend/api`) or
**proposed** (the frontend is built and mocked against this shape; the backend should match it).
The frontend types live in `frontend/src/api/types.ts`; the MSW mock in
`frontend/src/mocks/` implements everything below and is the executable form of this document.

## Conventions (exists)

- JSON everywhere, UTC ISO-8601 timestamps, same-origin (`/api/...`), session cookie
  (`__Host-session` over https, `jendock_session` over http), sent with `credentials: include`.
- Unsafe methods (POST/PUT/PATCH/DELETE) need `X-CSRF-Token: <session.csrf_token>` and an
  `Origin` exactly equal to `JENDOCK_PUBLIC_ORIGIN`. The browser sets `Origin` itself.
- Errors: `{"error": {"code", "message", "diagnostics"?, "fields"?}}`.
  - `diagnostics`: `[{severity: "error"|"warning", message, line, column}]` (pipeline validation, 422).
  - `fields`: `[{loc: string[], msg: string}]` on request-validation 422 (`loc` is e.g. `["body", "username"]`).
  - Codes: `not_found` 404, `conflict` 409, `validation_error` 422, `unauthenticated` 401,
    `forbidden` 403, `rate_limited` 429 (+ `Retry-After` seconds), `worker_unavailable` 503,
    `worker_error` 502, `source_error` 502 (git or GitHub could not be reached; the message says
    why, for example "SSH access is not configured"), `configuration_error` 500,
    `internal_error` 500.
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

## Projects (implemented, Task 6)

Reads need scope `read` (sessions always have it). Archived projects are omitted. Everything in
this and the Runs section below is implemented; where the implementation differs from the original
proposal the entry says so ("Deviation").

### `GET /api/projects`

200 `Project[]`, ordered by name:

```json
{
  "slug": "shop-api", "name": "Shop API", "repository": "acme/shop-api",
  "created_at": "2026-09-01T10:00:00Z",
  "environments": [
    {"name": "development", "branch": "develop", "auto_deploy": true,
     "compose_project": "shop-dev" | null, "compose_files": ["/abs/compose.yml"],
     "compose_env_files": [], "services": ["web"], "has_pipeline": true,
     "last_run": RunSummary | null},
    {"name": "production",  "branch": "main",    "auto_deploy": false, ... same keys ...}
  ],
  "git_transport": "ssh" | "https"
}
```

`git_transport` is an addition (not in the original proposal): how the worker clones the
repository, `ssh` = deploy key over ssh.github.com:443, `https` = public repositories.

`has_pipeline` (addition) is true when a pipeline is stored for the environment (a repository
`Jenkinsfile` alone does not count). With the compose settings it tells the UI whether an
environment can deploy by itself: the Projects page offers "turn on automatic deploy for all
configured environments" only for environments with `compose_project`, `compose_files` and
`has_pipeline`, and sends one `PATCH .../environments/{env}` per environment (each audited).

`environments` always has exactly two entries, development first. `last_run` is the newest run
(by `created_at`) of that environment, any status.

### `POST /api/projects`

Scope `admin`, CSRF. Body `{slug, name, repository: "owner/name", development_branch?,
production_branch?, git_transport?}` (extra keys rejected; branches default to `development` and
`production`, must differ; `git_transport` defaults to `ssh`). 201 `Project`. 409 `conflict` for
an existing slug, 422 for invalid values. Audited as `project.create`.

### `GET /api/projects/{slug}`

200 `Project` (same shape). 404 `not_found` (also for an archived project).

### `PATCH /api/projects/{slug}`

Scope `admin`, CSRF. Body with at least one of `name`, `repository`, `git_transport`. 200 `Project`.
Audited as `project.update`. Lets a project switch between the deploy key and public https.

### `PATCH /api/projects/{slug}/environments/{env}`

Branch mapping, auto-deploy toggle and Docker Compose settings. Scope `admin`, CSRF. Body with at
least one of `branch` (a push to this branch triggers the environment), `auto_deploy` (when false a
push is recorded but queues no run), `compose_project` (the Compose project name, or `null` to clear
it), `compose_files` and `compose_env_files` (lists of absolute paths, replaced as a whole; the
env files supply the variable interpolation of the compose files) and `services` (the service
names the environment manages). A key that is absent is left unchanged. The two environments of a
project must track different branches (422); paths must be absolute and clean, names valid (422).
200 `{name, branch, auto_deploy, compose_project, compose_files, compose_env_files, services,
last_run}` (the `Project.environments[]` entry has the same keys). Audited as
`environment.update` (before and after branch and auto-deploy, plus the resulting compose
settings).

### `GET /api/projects/{slug}/environments/{env}/runs?limit=20&offset=0`

`env` is `development|production`. `limit` 1-100 (default 20), `offset` >= 0.
200 `{items: RunSummary[], total, limit, offset}`, newest first. 404 for unknown project.

### `GET /api/runs?limit=20&offset=0`

Same page shape across all projects and environments, newest first (the "Runs" sidebar page).

### `GET /api/projects/{slug}/environments/{env}/head?branch=<name>`

Resolves the current head of a branch so the user sees what a manual run will build.
Query parameter instead of a path segment because branch names contain `/`.
200 `{branch, commit_sha (40 hex), message (first line), author, committed_at}`;
404 `not_found` when the branch does not exist. Scope `read`. The worker resolves it
(`git ls-remote`, then a depth-1 fetch of that commit for message, author and date); the API
never runs git. 502 `source_error` when GitHub cannot be reached or the deploy key is missing.

### `POST /api/projects/{slug}/environments/{env}/runs`

Manual run. Scope `run`, CSRF. Body `{branch: string, commit_sha: string}`: the sha the user
previewed; the server must build exactly that commit (extra keys rejected). The worker looks up
the branch head again and the request is rejected with 409 `conflict` when it is not that sha
any more (the branch moved after the preview, so the user should look at the new head first);
404 when the branch does not exist. Any branch may be built, not only the environment's tracked
one. Trigger is `manual`; `triggered_by` or `triggered_by_token_id` is recorded. 201 `RunSummary`
(status `queued`). Audited as `run.create`.
Deviation: the proposal said "not on that branch"; the implemented rule is "not the head of
that branch", which is what makes the previewed commit the confirmed one.

### `RunSummary`

Identical to the existing `RunResponse` without `stages`:
`id, number, project (slug), environment, trigger (push|manual|redeploy), branch, commit_sha,
pipeline_source (repository|platform), status, message|null, cancel_requested_at|null,
created_at, started_at|null, finished_at|null`.
`pipeline_source` of a queued run is provisional (`platform`): the worker decides at start
whether the repository's `Jenkinsfile` at the commit is used (then `repository`) and writes the
exact text it ran onto the run.
The UI shows the project slug in cross-project lists; it loads the project name from
`GET /api/projects/{slug}` for headings and breadcrumbs.

## Runs

### `POST /api/runs/{run_id}/retry`

Scope `run`, CSRF. Queues a new run of the same environment, branch and commit with trigger
`manual` (the pipeline is chosen again when it starts). The original must be finished: 409
`conflict` while it is `queued` or `running`, 404 for an unknown run. 201 `RunSummary`. Audited
as `run.create` with `retry_of`.

### `POST /api/github/webhook`

Not for the UI: GitHub calls it, authenticated by `X-Hub-Signature-256` only (no session, no
CSRF). See the README, "GitHub integration", for the full behaviour.

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

## Deployments (exists)

A deployment is one rollout of built images to the services of an environment. The worker
records it (previous and new image per service) before it changes anything, verifies container
health afterwards and rolls back automatically when the new containers do not become healthy.
Deployment statuses: `pending` (a queued redeploy that has not started), `deploying`,
`succeeded`, `failed` (refused before anything changed), `rolled_back` (the new images failed
and the previous ones run again) and `rollback_failed` (both failed: needs manual attention; the
run is failed with a message starting `ROLLBACK FAILED`). A `pending` row whose run ended
without deploying is reported as `failed` with the run's message.

### `GET /api/projects/{slug}/environments/{env}/deployments?limit=20&offset=0`

Scope `read`. `env` is `development|production`; `limit` 1-100, `offset` >= 0. 200, newest first:

```json
{
  "items": [{
    "id": 12, "run_id": 41, "run_number": 9, "status": "succeeded",
    "commit_sha": "0123456789abcdef0123456789abcdef01234567",
    "services": [{
      "service": "web",
      "previous_image": "jendock/shop-production-web:aaaaaaaaaaaa-r8", "previous_image_id": "sha256:...",
      "new_image": "jendock/shop-production-web:bbbbbbbbbbbb-r9", "new_image_id": "sha256:..."
    }],
    "message": null, "redeploy_of": null,
    "created_at": "2026-10-13T10:00:00Z", "started_at": "...", "finished_at": "..."
  }],
  "total": 1, "limit": 20, "offset": 0
}
```

`previous_image` and `previous_image_id` are `null` for a service that was not running before.
`message` explains `failed`, `rolled_back` and `rollback_failed`. `redeploy_of` is the id of the
deployment whose images a redeploy reuses. 404 for an unknown project, 422 for a bad `env`.

### `POST /api/projects/{slug}/environments/{env}/deployments/{id}/redeploy`

Scope `deploy`, CSRF. Queues a run (trigger `redeploy`, pipeline source `platform`) whose
pipeline is a synthetic `deploy` of the images of deployment `{id}`; nothing is rebuilt and the
source is not checked out (the Checkout stage is `skipped`). Only a `succeeded` deployment of
this environment can be redeployed, and its images must still exist on the host (image
retention keeps the newest ones). Audited as `deployment.redeploy`.
201 `{run: RunSummary, deployment_id}` where `deployment_id` is the new `pending` deployment;
404 `not_found` (unknown deployment, or one of another environment), 409 `conflict` (the
deployment did not succeed, or the run number was taken concurrently: retry).

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

Names mirror `backend/pipeline/ast.py`; spans are dropped. Optional things are `null`,
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
label | null), commit (jendock.commit label | null), state (created|running|paused|restarting|
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
  The override file is also applied by every later deployment; the deployer's image override
  stays the last compose file, so env edits never revert a deployed image.
- `PUT "/{id}/limits"` body `{cpus?, memory?}` (`1.5`, `"512m"`; at least one; capped by
  `JENDOCK_MANAGED_MAX_CPUS` / `JENDOCK_MANAGED_MAX_MEMORY`, 422 above) -> `ContainerView`.
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

## Domains (exists, Task 11)

A **domain** routes a public hostname to one service port of an environment: Cloudflare DNS
record, Apache vhost (reverse proxy to `127.0.0.1:<published_port>`) and a TLS certificate.
Hostnames are unique across all projects and environments. Reads need scope `read`; every
mutation needs scope `admin` (plus CSRF for sessions) and is audited. Design and operations:
`docs/ingress.md`.

New error code: `upstream_error` 502 (Cloudflare, the Apache host helper or the ACME CA refused
or failed; unlike `worker_error` the `message` is meant to be shown, e.g. `Cloudflare rejected
the API token (check its scope)` or `configtest failed, change reverted: <apache output>`).
`conflict` 409 is also returned when a DNS record that Jendock did not create is in the way,
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

`blocked: true` (`dns.action: "conflict"`) means a record not created by Jendock exists at
that name; apply is refused and nothing is touched. Cloudflare errors (no token, token
rejected, no zone) come back as 404 (neither the platform token of Settings > Integrations nor
a `CLOUDFLARE_API_TOKEN` secret of the environment exists),
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
vhost, certificate files/row and only the DNS records Jendock created; the domain returns
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

## Notifications (exists, Jira 16.5)

Alerts to Slack, generic webhooks (Microsoft Teams / Discord compatible) and email. Everything
below needs scope `admin` (plus CSRF for sessions) except `GET /api/notifications/policy`
(`read`). Every configuration change is audited (`notification.channel.create|update|delete|test`,
`notification.rules.update`); audit details name fields and ids, never a URL or credential.

**Channel secrets are write-only.** A webhook URL, a signing secret or SMTP credentials are sent
once (create or update); the API forwards them to the worker, which validates, encrypts and stores
them. No response, log line or audit event contains them: a channel only shows `configured: true`,
`hint` (the last 4 characters of a webhook URL, `null` for email) and a non-secret `summary`.

### Events

| `event` | Severity | Scope | Meaning |
| --- | --- | --- | --- |
| `run.failed` | critical | project | a run failed (payload: failing stage and step, last 20 masked log lines) |
| `run.fixed` | info | project | a run succeeded after the previous finished run of the environment failed |
| `run.interrupted` | warning | project | the worker stopped or restarted mid-run |
| `deployment.succeeded` | info | project | |
| `deployment.rolled_back` | critical | project | the new images failed and the previous ones run again |
| `deployment.rollback_failed` | critical | project | rollback failed, manual intervention needed |
| `certificate.expiring` | warning | project | a certificate has fewer than 14 days left (checked hourly) |
| `certificate.renewal_failed` | critical | project | a renewal attempt failed |
| `webhook.signature_spike` | warning | default only | more than N invalid GitHub webhook signatures within a window |
| `disk.usage_high` | warning | default only | the data volume is fuller than the threshold |

Cancelled runs and deployments that changed nothing are not announced.

### `GET /api/notifications/policy`

200 `{enabled, dedupe_minutes, max_attempts, disk_threshold_percent, webhook_failure_threshold,
webhook_failure_window_minutes, events: [{event, label, severity, project_scoped}]}`. The numbers are
set by the operator (`JENDOCK_NOTIFY_*`, see the README); `events` is the catalogue above.

### Channels

`NotificationChannel`: `{id, name, kind: slack|webhook|email, enabled, configured: true,
hint: string|null, summary, last_test_at, last_test_ok, created_at, updated_at}`. `summary` is
`{}` for Slack, `{signed: bool}` for a webhook and `{host, port, security, from_address,
to_addresses, authenticated}` for email.

- `GET /api/notifications/channels`: 200 `[NotificationChannel]` by name.
- `POST /api/notifications/channels` `{name, kind, config}`: 201 `NotificationChannel`.
  `config` per kind: Slack `{webhook_url}` (https); webhook `{url, signing_secret?}` (http or https,
  no credentials in the URL); email `{host, port=587, security=starttls|tls|none, username?,
  password?, from_address, to_addresses: [1-10]}` (a username needs a password). 409 for a duplicate
  name, 422 for invalid input (the message never echoes the input).
- `PATCH /api/notifications/channels/{id}` `{name?, enabled?, config?}`: 200. Fields left out of
  `config` keep their stored value (rotate only the password, only the URL...).
- `DELETE /api/notifications/channels/{id}`: 204; its rules and delivery history go with it.
- `POST /api/notifications/channels/{id}/test`: sends a real test message now (recorded in the
  history as a test). 200 `{ok, error}`: a failed delivery is `ok: false` with a short, safe
  reason such as `the endpoint answered HTTP 404` or `SMTP authentication failed`, never a URL.

### Rules

A rule subscribes one channel to a list of events in a **scope**: the global default, a project, or
one environment. The most specific scope that has any rule decides (environment, else project, else
default). Below the default, a rule with an empty `events` list (or `enabled: false`) mutes that
channel for the scope without falling back to wider rules; replacing the set with `[]` removes the
override. `RuleSet`: `{scope: default|project|environment, rules: [{channel_id, events, enabled}],
inherited_from: "project"|"default"|null}` where `inherited_from` names the wider scope that applies
while the scope has no rules of its own.

| Scope | Endpoints |
| --- | --- |
| default | `GET`/`PUT /api/notifications/rules` |
| project | `GET`/`PUT /api/projects/{slug}/notifications/rules` |
| environment | `GET`/`PUT /api/projects/{slug}/environments/{env}/notifications/rules` |

`PUT {rules: [...]}` replaces the whole set atomically and returns the new `RuleSet`. 422 for an
unknown event, a channel listed twice, or a default-only event (`webhook.signature_spike`,
`disk.usage_high`) below the default scope; 404 for an unknown channel or project.

### `GET /api/notifications/deliveries?project=&channel_id=&status=&limit=50&offset=0`

The delivery history (the outbox), newest first. `status` is `pending` (queued or waiting for a
retry), `sending`, `sent` or `failed` (attempts exhausted). 200 `{items, total, limit, offset}` with
items `{id, channel_id, channel_name, event, title, project, status, attempts, suppressed, is_test,
last_error, created_at, next_attempt_at (pending only), delivered_at}`. `suppressed` counts the
duplicates collapsed into the message. `last_error` is a short safe reason.

### Delivery semantics

Emission points only write outbox rows; the worker's dispatcher sends them, so a notification never
blocks or fails a run, a deployment or a renewal. A failed attempt is retried with exponential
backoff (30 s, 1 min, 2 min, ... capped at 1 hour) up to `max_attempts` (default 7). Within the quiet
period the same event for the same resource (an environment, a domain...) collapses into one message
per channel: `dedupe_minutes` (default 30), at least 24 h for `certificate.expiring` and 6 h for
renewal failures and disk alerts. Messages link to the run or domain page on the public origin and
carry the 7 character commit, who or what triggered the run and the failing stage. The only log
content is the last 20 lines of the failing step's log, masked and cut to 240 characters per line.

Webhook body (JSON, `Content-Type: application/json`): `{event, severity, title, text, content,
project, environment, run_id, run_number, deployment_id, commit, triggered_by, failed_stage,
failed_step, message, log_tail, details, url, delivery_id, timestamp}`; `text` and `content` repeat a
readable summary for Teams and Discord. Headers: `X-Jendock-Event`, `X-Jendock-Delivery` and, when a
signing secret is configured, `X-Jendock-Signature: sha256=<hex>`, the HMAC-SHA256 of the exact body.

## Integrations (exists)

Settings > Integrations and the Domains page's readiness check. Every endpoint below except
`GET /api/ingress/readiness` is for **browser sessions only** (a bearer token gets 403) and every
mutation needs the CSRF header. Nothing here can return a credential: the Cloudflare token is
write-only and lives encrypted in the worker's platform secret store (table `platform_secrets`,
decrypted only by the worker); private deploy keys never leave the worker. Mutations are
audited without values (`integration.credential_set`, `integration.credential_clear`,
`integration.cloudflare_test`, `integration.ingress_settings_update` with the names of the changed
fields, `deploy_key.create`).

Ingress reads the Cloudflare token from the platform store first and falls back to the
environment secret `CLOUDFLARE_API_TOKEN`. The ACME email, the server's public IPv4/IPv6 and the
ACME challenge are stored platform settings (table `platform_settings`); when none is stored the
`JENDOCK_INGRESS_*` environment value is the default. Whether ingress is *enabled*
(`JENDOCK_INGRESS_ENABLED`) and where the host helper socket is stay environment settings; the API
reports them and the reason when ingress cannot work.

### `GET /api/integrations/cloudflare`

200:

```json
{
  "token_configured": true, "token_updated_at": "2026-10-14T09:00:00Z",
  "settings": {"stored": V, "defaults": V, "effective": V},
  "helper": {"enabled": true, "socket_path": "/run/jendock-ingress/host.sock",
             "reachable": false, "reason": "the socket ... does not exist in the worker"} | null,
  "ready": false,
  "reasons": [{"code": "helper_unreachable", "message": "...", "blocking": true}]
}
V = {"acme_email": str|null, "public_ipv4": str|null, "public_ipv6": str|null,
     "acme_challenge": "dns-01"|"http-01"|null}
```

`stored` is what an administrator saved (`null` = use the default), `defaults` is the environment
value, `effective` is what the worker uses. `helper` is `null` when the worker did not answer.
Reason codes: `worker_unavailable`, `ingress_disabled`, `helper_unreachable`, `no_token` (all
blocking) and `no_acme_email` (advice). `ready` is true when no reason is blocking.

### `PUT /api/integrations/cloudflare/token`

Body `{token}` (1 to 512 characters, no whitespace; extra keys rejected; the value is never echoed,
not even in a 422). The API hands it to the worker, which encrypts and stores it. 200 as
`GET /api/integrations/cloudflare`.

### `DELETE /api/integrations/cloudflare/token`

Forgets the stored token (environment secrets keep working as the fallback). 200 as above.

### `POST /api/integrations/cloudflare/test`

The worker verifies the stored token with Cloudflare (`GET /user/tokens/verify`) and lists the
active zones it can see. 200 `{ok, status: "active"|... |null, zones: [name...], message}`: a wrong
or missing token is `ok: false` with a `message`, not an HTTP error. Zone **names only**.

### `PUT /api/integrations/cloudflare/settings`

Body: any of `acme_email`, `public_ipv4`, `public_ipv6`, `acme_challenge`. Only present keys
change; `null` or `""` removes the stored value so the environment default applies again. 422 for an
invalid email, address or challenge. 200 `{stored, defaults, effective}` (as `V` above).

### `GET /api/integrations/github`

200:

```json
{
  "webhook": {"payload_url": "https://<origin>/api/github/webhook",
              "secret_configured": true | false | null,
              "counts_7d": {"queued": 14, "recorded": 5},
              "recent": [{"delivery_id", "event", "repository", "outcome", "received_at"}]},
  "deploy_keys": [{"slug", "name", "repository", "git_transport", "exists": true,
                   "public_key": "ssh-ed25519 AAAA... jendock:acme/shop-api" | null}]
}
```

`secret_configured` only says whether the worker has a non-empty webhook secret file (`null` when
the worker did not answer); the value is never returned. `counts_7d` and `recent` come from the
`webhook_deliveries` table (the newest 10). `public_key` is the public half only.

### `POST /api/integrations/github/projects/{slug}/deploy-key`

The worker creates the project's repository deploy key (ed25519, same logic as
`python -m backend.cli deploy-key create`). 201 `{slug, public_key}`; 409 when a key already
exists (rotation stays a CLI action); 404 unknown or archived project.

### `GET /api/ingress/readiness?project=<slug>&environment=<env>`

Scope `read`. Same `reasons` as the Cloudflare endpoint; with `project` and `environment` an
environment's own `CLOUDFLARE_API_TOKEN` secret also counts as a token. 200
`{ready, token_configured, reasons}`. The Domains page shows a Banner linking to Settings >
Integrations while a blocking reason exists.

## MCP tools of a scope selection (exists)

### `GET /api/mcp/tools?scopes=read,run`

Browser sessions only. The tools a token holding exactly those scopes would see (`admin` implies
all), derived from the MCP tool registry so the settings page can never drift from the server.
Unknown scope names give 422; no scopes give an empty list. 200:

```json
{"endpoint": "https://<origin>/mcp", "enabled": true,
 "tools": [{"name": "list_projects", "description": "...", "scope": "read", "read_only": true}]}
```

## Serving the UI (status: exists)

When `JENDOCK_FRONTEND_DIST` points to a Vite `dist` directory (the API image sets it) the API
serves it: static files at `/`, any other `GET` that is not below `/api` or `/healthz` and does not
look like a missing file (`.js`, `.css`, `.png`...) answers `index.html` (client-side routes).
Unknown `/api/*` paths, missing `/assets/*` files and missing files with a file suffix keep the JSON
error envelope (404 `not_found`). `/assets/*` (content-hashed) is sent with
`Cache-Control: public, max-age=31536000, immutable`; everything else keeps `no-store`. The
Content-Security-Policy is unchanged (`default-src 'self'; frame-ancestors 'none'; base-uri 'none'`):
the built UI, including CodeMirror in its shadow root, runs under it.
