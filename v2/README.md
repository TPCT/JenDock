# Shiftcodes v2

Clean rewrite of the self-hosted, Jenkins-like CI/CD platform. See `PLAN.md` for decisions
and the task list. v1 (the parent directory) keeps running until v2 reaches parity.

## Layout

- `shiftcodes/domain` - pure enums, validated value objects, errors, audit rules; no I/O
- `shiftcodes/services` - use cases (project, environment, pipeline, run, secret, audit);
  each receives a `UnitOfWork`, no globals
- `shiftcodes/repositories` - SQLAlchemy models, `database.py` (engine, `UnitOfWork`), one
  query-only repository per aggregate, Alembic migrations in `migrations/`
- `shiftcodes/adapters` - external systems: `ContainerRuntime` port with the docker CLI
  implementation, git source, workspaces, deployer port, HTTP probe (GitHub, Cloudflare later)
- `shiftcodes/execution` - worker-side pipeline execution (runner, steps, docker agent rules,
  logs, run queue, crash recovery)
- `shiftcodes/api` - FastAPI HTTP layer (`create_app`, `AppContainer`, routers, schemas, error
  mapping, security headers)
- `shiftcodes/worker` - worker process (`python -m shiftcodes.worker`): RPC server, the run
  queue and the only holder of the secret key
- `shiftcodes/pipeline` - Jenkinsfile engine (lexer, parser, typed AST, step catalogue, validator,
  serializer); pure code, never executes anything
- `shiftcodes/cli.py` - admin CLI (`python -m shiftcodes.cli users ...`)
- `shiftcodes/mcp` - MCP server for AI agents
- `shiftcodes/config.py`, `shiftcodes/logging.py` - settings (`SHIFTCODES_*`) and JSON logging
- `shiftcodes/hosthelper` - the host helper (stdlib + cryptography): Apache vhosts, certificates;
  ingress design in `docs/ingress.md`, install in `docs/ingress-host-helper.md`
- `docker/` - `api.Dockerfile`, `worker.Dockerfile`; `compose.yml` wires them together

## Make targets

- `make install` - editable install with dev extras into `.venv`
- `make lint` - ruff check and ruff format --check
- `make format` - apply ruff fixes and formatting
- `make typecheck` - mypy (strict) on `shiftcodes` and `tests`
- `make test` - pytest (tests marked `docker` are deselected)
- `make test-docker` - the real-Docker integration tests (`pytest -m docker`); they are skipped
  when no Docker daemon or git is available. They build and run containers in the local daemon,
  label everything `shiftcodes.test=1`, and the teardown removes it and fails if anything is left
- `make migrate` - `python -m shiftcodes.repositories.migrate` (upgrade head on
  `SHIFTCODES_DATABASE_URL`); the same command works inside the api/worker images
- `make lock` - regenerate `requirements.lock` (needs Docker)
- `make test-py313` - run tests on Python 3.13 in Docker (installs git in the throwaway container)
- `make check` - lint, typecheck, test
- `make lint-jenkins` - compare the Jenkinsfile engine with a real Jenkins linter in a throwaway
  container (needs Docker and network); rewrites `docs/jenkins-compatibility.md`

## Deployment variables (compose)

- `DOCKER_SOCKET_PATH` - host path of the Docker socket mounted into the worker
  (default `/var/run/docker.sock`; e.g. `/run/docker-isolation.sock` on the production VPS).
  It is always mounted at `/var/run/docker.sock` inside the container, which is what
  `SHIFTCODES_DOCKER_HOST` points to.
- `DOCKER_GID` - numeric group id owning that socket on the host (default `999`;
  see `stat -c %g <socket>`). Added to the worker via `group_add`.

## Reproducible builds

`requirements.lock` pins runtime dependencies and is used as pip constraints in both
Dockerfiles. `make lock` regenerates it inside `python:3.13.9-slim`.
`make test-py313` runs the test suite in that image.

## Authentication and security

### Components and trust boundaries

- The **API** is internet-facing. It has no Docker socket and **no secret encryption key**
  (building the app never reads a key file). All secret encryption/decryption happens in the
  **worker**, reached over a Unix socket (`SHIFTCODES_WORKER_SOCKET_PATH`).
- The worker owns `SecretCipher`. The key file (`SHIFTCODES_SECRET_KEY_FILE`, mode 0600) lives in
  the `keys` volume, mounted **only** in the worker (`/run/shiftcodes-keys/secret.key`). The API
  container mounts only `data` and `worker-socket`. Upgrading from Task 2: copy `secret.key`
  from the data volume into the `keys` volume, then delete it from the data volume.
- Worker RPC (`shiftcodes/worker/rpc.py`, client `shiftcodes/adapters/worker_client.py`):
  JSON lines, method allowlist (`ping`, `secrets.set|list|delete|reveal`), 1 MiB request limit,
  per-request id, 5 s client timeout, structured errors mapped back to domain errors. Each secret
  call carries the acting principal (user or token) which the worker writes to the audit log; the
  worker takes that identity at face value (see below). The socket is created with mode 0660
  inside the `worker-socket` volume, a setgid 2770 directory owned by group gid 10001 in both
  images; only members of that group can connect.
- Users: the API runs as uid 10002, the worker as uid 10001, both in group gid 10001. The data
  volume (SQLite DB, WAL/SHM) and the socket directory are setgid 2770 and every entrypoint sets
  umask 0007, so both processes can write while others cannot. The `keys` volume is 0700 owned
  by uid 10001: the API user cannot read it even if it were mounted.
- **Trust model of the actor identity.** The worker records the `actor` sent by the API in the
  audit log without being able to verify it. The socket is the trust boundary: only processes in
  the shared group can reach it, and the API is the only such process. This is acceptable
  because a compromised API could already do everything its principals can do (it holds the
  database and decides whom to serve), and the worker exposes only a fixed allowlist of secret
  operations, never Docker or key access. If the API is compromised, audit actor attribution
  for that period is unreliable, which is a known limit rather than an isolation failure.

### Browser auth (sessions)

- Admin users use argon2id hashes (`argon2-cffi`), rehashed on login when parameters are weaker
  than the current policy. Minimum password length 12.
- Login creates a server-side session: a 32-byte random token in the cookie, only its SHA-256
  is stored. Idle expiry 1 hour, absolute 12 hours (`SESSION_IDLE_TIMEOUT`,
  `SESSION_ABSOLUTE_TIMEOUT` in `config.py`). Expired sessions are rejected and deleted lazily.
  Background endpoints (the run event stream) use `require_scope(..., background=True)` /
  `get_background_principal`, which authenticate without extending the idle timer.
- Cookie: `__Host-session`, Secure, HttpOnly, SameSite=Strict, Path=/, no Domain. Only a
  development setup with an `http://` `SHIFTCODES_PUBLIC_ORIGIN` uses the plain name
  `shiftcodes_session` without Secure.
- CSRF: every POST/PUT/PATCH/DELETE authenticated by cookie (and the login request) needs
  `Origin` exactly equal to `SHIFTCODES_PUBLIC_ORIGIN` and header `X-CSRF-Token` equal to the
  session's CSRF token (`GET /api/auth/session` or the login response returns it). The token is
  an HMAC of the cookie value, so nothing CSRF-related is stored; it is compared with
  `hmac.compare_digest`.
- Login rate limit: 5 failed attempts per username and 20 per client IP per 15 minutes, then
  HTTP 429 with `Retry-After`. State is in process memory (single API process); a restart
  resets it and multiple replicas would not share it. The client IP is the peer address; behind
  Apache/Cloudflare set `SHIFTCODES_TRUSTED_PROXIES` (IPs/CIDRs, default empty = trust nobody;
  compose var `TRUSTED_PROXIES`, e.g. the docker bridge gateway). `python -m shiftcodes.api`
  (the image CMD) then starts uvicorn with proxy headers trusted only from those addresses.
  Without it every client appears as the proxy and the per-IP limit becomes a global lockout.
  Unknown users and wrong passwords return the same 401 and both run one argon2 verification.
- Security headers on every response: CSP (`default-src 'self'; frame-ancestors 'none';
  base-uri 'none'`), nosniff, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`,
  `Cache-Control: no-store`, and HSTS in production. Swagger UI is disabled (its CDN assets
  conflict with the CSP); `/openapi.json` exists in development only.

### Creating the first admin

Passwords are read from a hidden prompt or, with `--password-stdin`, from stdin. There is
deliberately no argv or environment option.

```
make migrate
.venv/bin/python -m shiftcodes.cli users create admin            # prompts twice
printf '%s\n' "$PW" | python -m shiftcodes.cli users reset-password admin --password-stdin
# inside the deployment:
docker compose run --rm -T api python -m shiftcodes.cli users create admin --password-stdin
```

`reset-password` also ends all sessions of that user. Both commands are audited
(`user.create`, `user.reset_password`, actor `system:cli`).

### API tokens (agents / MCP)

- `Authorization: Bearer sct_...`; no cookie, no CSRF. A request with an `Authorization` header
  never falls back to a session cookie.
- Created only by a logged-in browser session (`POST /api/tokens`, also `GET /api/tokens`,
  `DELETE /api/tokens/{id}`); a token can never manage tokens. The value is returned exactly
  once; only its SHA-256 and an 8-character display prefix are stored.
- Scopes: `read`, `run`, `deploy`, `containers`, `terminal`, `pipelines`, `admin` (`admin`
  implies every other scope except token management, which is session-only). Secrets: list needs
  `read`; set, delete and reveal need `admin`. Sessions hold all scopes.
- Revoked or expired tokens are rejected; `last_used_at` is updated at most once per minute;
  token actions are audited with actor type `token`. Runs can record `triggered_by_token_id`.

### Secret endpoints

`GET|PUT|DELETE /api/projects/{slug}/environments/{env}/secrets[/{name}]` and
`POST .../{name}/reveal` all call the worker. Values are never returned except by `reveal`,
which is audited (name only, never the value).

## Execution (Task 5)

### Model

A run is created `queued` (Task 6 adds the webhook and manual triggers; tests use `RunService`).
The worker polls the queue (`SHIFTCODES_QUEUE_POLL_INTERVAL`), claims the **oldest** queued run
with an atomic conditional update (`queued -> running`) while fewer than
`SHIFTCODES_MAX_CONCURRENT_RUNS` (default 1, a global limit of this worker) are executing, and
runs it on its own thread. `RunExecutor` loads the run, parses and validates its pipeline
snapshot again (a secret may have been deleted since), creates the workspace, executes, then
always cleans up (kills leftover containers by label, deletes the workspace) and only then
records the final status (`succeeded`, `failed`, `cancelled` or `interrupted`) and a short
`message`.

`PipelineRunner` creates every stage row up front (so the whole shape is visible at once):
`Checkout` (clone at the pinned commit), the pipeline's stages (nested stages are shown as
`Parent / Child`) and `Post Actions` when the pipeline has a `post`. Statuses are `queued`,
`running`, `succeeded`, `failed`, `cancelled`, `interrupted`, `skipped`. Every executed step
(including block steps such as `retry` and `timeout`) gets a step row, a log file and, for scripts,
the exit code.

- Stages run in order; a failing step fails its stage and the run; later stages are `skipped`;
  `post` still runs. `parallel` branches run concurrently on a pool of
  `SHIFTCODES_PARALLEL_WORKERS` threads (default 2); a failing branch does not stop its siblings
  but the following stages are skipped. Nested sequential `stages` skip the rest of their
  group after a failure.
- `when` is evaluated against the run context: `branch` uses Ant-style globs (`*` stays inside
  a path segment, `**` crosses `/`), `environment name:, value:` compares a variable of the
  stage's parent scope (built-ins such as `ENVIRONMENT` included), plus `not`, `allOf`, `anyOf`.
  A stage's own `environment` is not visible to its own `when`.
- `post` follows Jenkins: blocks run in the order always, changed, aborted, failure, success,
  unstable, cleanup (cleanup last) whatever order they are written in. `changed` compares the
  outcome with the previous finished run of the same environment (for stages: the same-named
  stage); with no previous run it does not fire. `aborted` means cancelled. `unstable` never
  fires because no step can make a run unstable. A failing post step fails the run but the
  remaining blocks (and `cleanup`) still run. Post steps are not interrupted by a cancel.
- `options`: `timeout` bounds the pipeline or stage (it fails; Jenkins would call it aborted),
  `retry(n)` re-runs the stage (or the whole pipeline, whose stage rows are reset). The
  `retry`/`timeout` steps do the same for a block; a cancel is never retried. A container step
  without an enclosing timeout is still bounded by `SHIFTCODES_STEP_TIMEOUT_SECONDS` (3600).
  `skipDefaultCheckout` and `disableConcurrentBuilds` are accepted and have no effect (the workspace
  always holds the commit; only the global concurrency limit applies).
- Steps are classes registered by name (`shiftcodes/execution/step_registry.py`, one per catalogue
  step). Strings are rendered from the AST parts and the resolved environment, never evaluated.
  `credentials('X')` is resolved by the worker through `SecretService.resolve_for_run` (audited as
  `secret.resolve`) and registered with the run's redactor.
- `deploy` calls a `Deployer`; until Task 7 the worker wires `UnavailableDeployer`, which fails
  the step with "deploy is not available yet (Task 7)". `healthCheck` does an HTTP GET (redirects
  are not followed) every 2 s until the expected status or its timeout.

### Docker agents

Every `sh` runs in a container started with `--rm`, labels `shiftcodes.run_id` and
`shiftcodes.step_id`, `--security-opt no-new-privileges`, `--cpu-shares 512` (so production services
win under contention) and `--entrypoint sh -c <script>`.

- **Logical image** (built by `dockerBuild`, tagged `sc/<project>-<env>-<image>:<sha12>-r<run
  number>` with labels `shiftcodes.project|environment|run_id|commit`): runs with the image's own
  `WORKDIR`, user and no workspace mount. `dir()` becomes a `cd` at the start of the script.
- **External image** (`node:22`): pulled when not present locally (once per run), the workspace is
  mounted at `/workspace`, which is the working directory (`dir()` is relative to it), and the
  container runs as the worker's uid:gid with `HOME=/tmp` unless set, so the files it creates can
  be deleted afterwards.
- **Network**: the environment's `<compose_project>_default` network when the environment has a
  compose project and that network exists (otherwise a log line and the default bridge), the default
  bridge without a compose project, `--network none` when the agent args say so.
- **Limits**: `-e NAME=value`, `--memory` and `--cpus` from the allowlisted args. The defaults
  and the maxima are `SHIFTCODES_CONTAINER_MEMORY` (2g) and `SHIFTCODES_CONTAINER_CPUS` (2); a
  pipeline may lower them but a larger value fails the step ("exceeds the configured maximum").
  Builds use `--memory SHIFTCODES_BUILD_MEMORY` (2g).

### Secrets and the Docker client

- The container environment (secrets included) is written to a temporary `--env-file` created
  0600 in `<data_dir>/worker-private` (0700) and deleted as soon as the daemon has created the
  container (or at the first output line / exit, whichever is first). It is never in argv. A value
  with a line break cannot be represented and is refused.
- In a `sh` script, a variable bound to a credential is written as `${NAME}` instead of its value
  (so even `sh "curl ${TOKEN}"` keeps the secret out of the command line; the shell expands it
  from the environment). Inside single quotes in the script this is not expanded, which differs
  from Groovy interpolation.
- `dockerBuild` `args` values are passed with `--build-arg NAME` and the value in the docker client's
  environment (not argv); names that change the client's behaviour (`PATH`, `HOME`, `LD_*`, `DOCKER_*`,
  `BUILDKIT_*`, proxies ...) are refused.
- The docker client is `SHIFTCODES_DOCKER_BINARY` (default `docker`; set a non-default path in
  production) with `DOCKER_HOST` from `SHIFTCODES_DOCKER_HOST`. It always gets an explicit argv list.

### Workspaces and source

`<data_dir>/workspaces/<run_id>` is created 0750 and removed after the run, also on failure,
cancel and shutdown. `LocalGitSource` runs `git clone --no-checkout`, refuses a commit that
tracks `.env`, `id_rsa`, `id_ed25519`, `id_ecdsa` or `id_dsa` (before writing the tree), then
`git checkout --detach <sha>` and verifies `HEAD == sha`. The clone URL is
`SHIFTCODES_GIT_URL_TEMPLATE` (default `https://github.com/{repository}.git`, public repositories
only until Task 6 adds deploy keys); the `SourceProvider` protocol is what Task 6 implements. A workspace
larger than `SHIFTCODES_WORKSPACE_MAX_BYTES` (2 GiB) fails the run; it is measured after checkout and
after each `sh` step (a single step can still exceed it until it ends). When the worker itself runs
in a container, `SHIFTCODES_WORKSPACE_HOST_DIR` must name the host directory mounted at
`<data_dir>/workspaces` because the host daemon resolves bind mounts (compose does this with
`WORKSPACES_HOST_DIR`, default `/var/lib/shiftcodes-workspaces`, which must exist and be owned by
uid 10001). The worker image contains git and a pinned docker CLI.

### Logs

Output of each step goes to `<data_dir>/logs/<run_id>/<step_id>.log` (files 0640, directories 0750)
after secrets were masked **line by line** (a multi-line secret is also masked per line); a step
log stops at `SHIFTCODES_STEP_LOG_MAX_BYTES` (5 MiB) with one truncation marker. Known limits:
a secret split over two lines, or transformed (base64, URL-encoded), is not recognised; a line
longer than 64 KiB is delivered in pieces.

API (scope `read`; sessions hold every scope):

- `GET /api/runs/{id}` - run, stages and steps (statuses, exit codes, timestamps, `message`,
  `cancel_requested_at`).
- `GET /api/runs/{id}/steps/{step_id}/log?offset=N` - at most 64 KiB starting at byte `N`, cut at
  a line boundary: `{offset, next_offset, size, text, complete}`; continue with
  `offset=next_offset` (`complete` is true when the step ended and nothing is left).
- `GET /api/runs/{id}/events` - Server-Sent Events: `status` (full run JSON, sent when it changes),
  `log` (`{step_id, offset, text}` for new lines; the history is replayed first), `: heartbeat`
  comments every `SHIFTCODES_SSE_HEARTBEAT_INTERVAL` (15 s) and `end` when the run finished. It uses
  the background principal, so it never extends the session's idle timer; the session is
  re-checked (also without touching it) at each heartbeat and the stream closes when it expired.
  A stream lasts at most one hour (clients reconnect).

### Cancel, shutdown and crash recovery

`POST /api/runs/{id}/cancel` (scope `run`, CSRF and exact Origin for sessions, audited as
`run.cancel`): a queued run becomes `cancelled` immediately; a running run gets
`cancel_requested_at`. The worker watches that column every `SHIFTCODES_CANCEL_POLL_INTERVAL`
(1 s): it stops between steps, kills containers labelled `shiftcodes.run_id=<id>` on every poll
(and terminates a running build), the run ends `cancelled`, `post` `aborted`/`always`/`cleanup` still
run. A finished run answers 409. On SIGTERM the worker interrupts its runs (`interrupted`, post
skipped) and waits up to 30 s.

At start the worker runs crash recovery: runs left `running` have their containers killed by
label and every workspace directory removed, then are marked `interrupted` with a message
(running stages/steps `interrupted`, queued stages `skipped`). It is idempotent.

### Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHIFTCODES_DOCKER_BINARY` | `docker` | docker CLI path |
| `SHIFTCODES_DOCKER_HOST` | `unix:///var/run/docker.sock` | daemon address |
| `SHIFTCODES_MAX_CONCURRENT_RUNS` | `1` | runs executing at once |
| `SHIFTCODES_PARALLEL_WORKERS` | `2` | threads per `parallel` group |
| `SHIFTCODES_CONTAINER_MEMORY` / `_CPUS` | `2g` / `2` | step container default and maximum |
| `SHIFTCODES_BUILD_MEMORY` | `2g` | `docker build --memory` |
| `SHIFTCODES_STEP_TIMEOUT_SECONDS` | `3600` | default limit of a container step |
| `SHIFTCODES_WORKSPACE_MAX_BYTES` | 2 GiB | workspace bound |
| `SHIFTCODES_STEP_LOG_MAX_BYTES` | 5 MiB | log bound per step |
| `SHIFTCODES_WORKSPACE_HOST_DIR` | unset | host path of the workspaces (worker in a container) |
| `SHIFTCODES_GIT_URL_TEMPLATE` / `_GIT_BINARY` | GitHub https / `git` | source clone |
| `SHIFTCODES_QUEUE_POLL_INTERVAL`, `_CANCEL_POLL_INTERVAL` | 1 s | worker polling |
| `SHIFTCODES_SSE_POLL_INTERVAL`, `_SSE_HEARTBEAT_INTERVAL` | 0.5 s / 15 s | event stream |
| `SHIFTCODES_FRONTEND_DIST` | unset | built web UI served by the API (the API image sets it) |

## Pipelines (Jenkinsfile)

Pipelines are a **subset of Jenkins Declarative Pipeline**. Everything the engine accepts is
meant to be parseable by real Jenkins too (platform steps look like shared-library steps); what
it does not support is rejected with a positioned error, never ignored. The engine is pure
code (`shiftcodes.pipeline`): `parse(text) -> Pipeline`, `validate(pipeline, context) ->
list[Diagnostic]`, `serialize(pipeline) -> str`, `PipelineSyntaxError`. Nothing evaluates Groovy
or runs a process. `PipelineService.save` validates with `JenkinsfileValidator` (secret names
come from the environment's secrets) and raises `ValidationError` whose `diagnostics` are
returned by the API as `error.diagnostics` (`severity`, `message`, `line`, `column`).

### Supported

- Exactly one `pipeline { ... }`. Sections in any order, each at most once: `agent` (required),
  `environment`, `options`, `parameters`, `stages` (at least one stage), `post`; none of them
  may be empty. A newline is allowed before a section's `{`.
- `agent any`, `agent none`, `agent { docker 'img' }`, `agent { docker { image 'img'; args '...' } }`
  at pipeline and stage level. `image` is either an image built by an earlier `dockerBuild`
  (logical name) or an external reference that pins a tag or digest (`node:22`,
  `registry.example.com:5000/x:1`). `args` allowlist: `--network none`, `-e NAME=value`,
  `--memory N`, `--cpus N`; anything else is an error naming the flag.
- `environment { NAME = 'value'; NAME = "value ${OTHER}"; NAME = credentials('SECRET') }`,
  names `[A-Z_][A-Z0-9_]*`; a variable can use earlier ones and outer scopes.
- `options { timeout(time: N, unit: 'SECONDS'|'MINUTES'|'HOURS'); retry(N);
  disableConcurrentBuilds(); skipDefaultCheckout() }` (pipeline and stage level; `retry`
  1..10; the same option twice is an error).
- `parameters { string(name:, defaultValue:, description:); booleanParam(...);
  choice(name:, choices: [...], description:) }`.
- `stage('Name') { agent / environment / options / when / one of steps | parallel | stages / post }`.
  `parallel` contains only stages (a branch may hold sequential `stages`); sequential `stages`
  nest to any depth (limit 32 levels of braces) but `parallel` cannot appear inside `parallel`. Stage names are unique per level.
- `when { branch 'glob'; environment name: 'X', value: 'y'; not { ... }; allOf { ... };
  anyOf { ... } }` (several top-level conditions mean all of them).
- `post { always | changed | aborted | failure | success | unstable | cleanup { steps } }`
  (pipeline and stage level).
- Steps (command or parenthesis syntax, `;` or newline separated, trailing commas allowed):
  - `sh 'cmd'` / `sh(script:, label:)` - needs a docker agent; `returnStatus` and
    `returnStdout` are rejected because there is no Groovy to consume them.
  - `echo 'msg'`, `error 'msg'`.
  - `timeout(time: N, unit: 'MINUTES') { }` (also `timeout(N) { }`), `retry(N) { }`, `dir('relative/path') { }`,
    `withEnv(['NAME=value']) { }` (names defined by `withEnv` are visible inside its block).
  - `dockerBuild(image: 'logical-name', dockerfile: 'Dockerfile', context: '.',
    target: 'stage', args: [KEY: 'value'])` - image `[a-z0-9][a-z0-9_.-]*`, paths relative
    without `..`.
  - `deploy(service: 'web', image: 'logical-name')` or `deploy(services: [web: 'image', ...])`
    - the image must be built by a `dockerBuild` in an earlier step or stage (parallel branches
    cannot depend on each other; later stages see everything the group built).
  - `healthCheck(url: 'https://...', expectStatus: 200, timeout: 60)`.
- Values: strings, integers (up to 9 digits), `true`/`false`, lists `[...]`, maps `[k: v]`
  (`[:]` is the empty map).
- Strings: `'...'` and `'''...'''` (no interpolation), `"..."` and `"""..."""` with `${NAME}`,
  `$NAME`, `${env.NAME}`, `${params.NAME}`; escapes `\n \t \r \\ \' \" \$`. Known names: the
  pipeline/stage `environment`, `withEnv` names and the built-ins `BRANCH_NAME`, `GIT_COMMIT`,
  `BUILD_NUMBER`, `ENVIRONMENT`, `PROJECT`; `params.X` only for declared parameters. A secret
  interpolated into a double-quoted string is a warning (use single quotes). Interpolations are
  kept as variable parts in the AST and never evaluated.
- Comments `//` and `/* */`. Limits: 256 KiB input, nesting depth 32.

### Rejected (with a message)

`script { }`, `def`, `if`/`for`/`try` and other Groovy statements, assignments, method
definitions, `@Library`, imports and any code outside `pipeline { }`; `when { expression { } }`
and `when` conditions other than the ones above; `triggers` in any form (triggers are configured
in the platform); `tools`, `input`, `libraries`, `agent { label/dockerfile/node }`, other
post conditions; `null`, negative or non-integer numbers; unknown steps (the error lists the
supported ones); any `${...}` that is not a plain name (method calls, arithmetic, `a.b.c`);
unknown variables, parameters and credentials; docker args outside the allowlist; external
images without a tag or digest; `sh` without a docker agent; `deploy` of an image no earlier
`dockerBuild` produced; empty `steps`, `stages` or `parallel`; duplicate sections, stage names,
environment names or parameters.

### Compatibility with real Jenkins

Every valid fixture, the example below and the serializer output of each are accepted by the
Declarative linter of a real Jenkins, and nothing Jenkins rejects is accepted here. The
comparison (with the reason for every deliberate difference) is in
`docs/jenkins-compatibility.md`; rerun it with `make lint-jenkins` (needs Docker).

### Round trip

`serialize` writes the canonical form (4-space indent, single quotes unless a string has
variables, sections in Jenkins' conventional order, post blocks sorted, positional arguments
written in the canonical call style). For every accepted text,
`parse(serialize(parse(x))) == parse(x)` (spans are excluded from equality). Comments are not
part of the AST and are not preserved: the visual editor rewrites the canonical form, while
text-mode edits keep the user's own text.

### Editors, templates and dry run

The browser never parses Jenkinsfile text. `shiftcodes/pipeline/model*.py` define the JSON
editing model (pydantic) and its two converters (`to_model` / `to_ast`, round trip tested on every
valid fixture); `catalog.py` generates the step catalogue (titles, help texts, enums, bounds and
image roles come from `steps.py`, so forms and validator cannot disagree);
`pipeline/templates/` holds the starter Jenkinsfiles (also compared with real Jenkins);
`execution/dry_run.py` plans a pipeline for a branch without executing it. The endpoints are in
`docs/api-contract.md`, "Pipelines". The repository's `Jenkinsfile` is detected by the worker
(`pipeline.repository_file` RPC, a depth-1 filtered fetch of the environment branch).

The API image builds `frontend/` (node stage) and serves it, with a fallback to `index.html`
for client routes (`shiftcodes/api/frontend.py`).

### Example

```groovy
pipeline {
    agent none
    environment {
        DB_PASSWORD = credentials('DB_PASSWORD')
    }
    options {
        timeout(time: 30, unit: 'MINUTES')
    }
    stages {
        stage('Build image') {
            steps {
                dockerBuild(image: 'laravel-app', dockerfile: 'docker/Dockerfile')
            }
        }
        stage('Test') {
            agent {
                docker {
                    image 'laravel-app'
                    args '--network none --memory 1g'
                }
            }
            steps {
                sh 'php artisan test'
            }
        }
        stage('Migrate') {
            when { branch 'production' }
            agent { docker 'laravel-app' }
            steps {
                sh 'php artisan migrate --force'
            }
        }
        stage('Deploy') {
            steps {
                deploy service: 'app', image: 'laravel-app'
                healthCheck url: 'https://example.test/up', timeout: 90
            }
        }
    }
    post {
        failure {
            echo "Build ${BUILD_NUMBER} failed on ${BRANCH_NAME}"
        }
    }
}
```
