# Execution and deployment

## Model

A run is created `queued` by a webhook push, a manual run or a retry (see `github.md`).
The worker polls the queue (`JENDOCK_QUEUE_POLL_INTERVAL`), claims the **oldest** queued run
with an atomic conditional update (`queued -> running`) while fewer than
`JENDOCK_MAX_CONCURRENT_RUNS` (default 1, a global limit of this worker) are executing, and
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
  `JENDOCK_PARALLEL_WORKERS` threads (default 2); a failing branch does not stop its siblings
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
  without an enclosing timeout is still bounded by `JENDOCK_STEP_TIMEOUT_SECONDS` (3600).
  `skipDefaultCheckout` and `disableConcurrentBuilds` are accepted and have no effect (the workspace
  always holds the commit; only the global concurrency limit applies).
- Steps are classes registered by name (`backend/execution/step_registry.py`, one per catalogue
  step). Strings are rendered from the AST parts and the resolved environment, never evaluated.
  `credentials('X')` is resolved by the worker through `SecretService.resolve_for_run` (audited as
  `secret.resolve`) and registered with the run's redactor.
- `deploy` calls a `Deployer` (`ComposeDeployer`, see Deployment below); optional
  `healthUrl`/`healthStatus`/`healthTimeout` arguments add an HTTP check that must pass before
  the deployment counts as good. `healthCheck` does an HTTP GET (redirects are not followed)
  every 2 s until the expected status or its timeout.

## Deployment

`ComposeDeployer` (`backend/adapters/compose_deployer.py`) deploys the requested services of
an environment and nothing else:

0. Every compose invocation (config, ps, up, rm; deploy, rollback and recovery) starts with
   `docker compose --env-file <f>... -p <project> -f <file>... [-f <override>]`, built only by
   `ComposeProject.base_command`. `compose_env_files` of the environment
   supply the variable interpolation of the compose files; every compose and env file must be
   readable by the worker, otherwise the deployment is refused with a clear error.
1. It checks that every service exists in `docker compose config` of the environment's own files
   and that the new image exists locally (it never pulls or builds).
2. It records, per service, the running image tag and id in a `deployments` row, and writes a
   journal `<data_dir>/deployments/<compose project>/journal.json` (fsynced) with everything
   needed to undo the change.
3. It writes the managed override `<data_dir>/deployments/<compose project>/override.yml`
   (atomic; the replaced version stays as `override.yml.prev`) that pins `image:` of each
   deployed service, and runs `docker compose -p <project> -f <files...> -f <override> up -d
   --no-deps --no-build --pull never <services>`. The override is always the last compose file, so
   later restarts or recreations of the service keep the deployed image.
4. It waits (`JENDOCK_DEPLOY_HEALTH_TIMEOUT`, default 120 s) until every container is
   `running` and, when it has a healthcheck, `healthy`, and stays so for
   `JENDOCK_DEPLOY_SETTLE_SECONDS` (default 3) without its restart count increasing; then the
   optional HTTP check runs.
5. On any failure it restores the previous override, recreates the services from their previous
   image (pinned by id when the tag moved; a service that did not exist before is removed) and
   verifies them again: the deployment becomes `rolled_back`, or `rollback_failed` (the run fails
   with a message starting `ROLLBACK FAILED`). Either way the step fails.
6. After success it removes old images (`JENDOCK_IMAGE_RETENTION`, default 5): per logical
   image (repository) of this project and environment (labels `jendock.project` and
   `jendock.environment` are required, so foreign images are never touched) only the newest N
   remain, plus any image used by a container and the images of the latest deployment
   (previous and new).

At worker start `recover_interrupted_deployments` (before runs are marked `interrupted`) rolls
back every deployment whose journal is still on disk and marks deployments left `deploying`
without a journal as `failed`. Recovery always rolls back rather than finishing, because the run
that owned the deployment ends `interrupted`. One deployment per environment runs at a time.

`GET .../deployments` lists the history and `POST .../deployments/{id}/redeploy` queues a run
(trigger `redeploy`) that deploys the images of a successful deployment again without checkout
or build; see `docs/api-contract.md`.

## Docker agents

Every `sh` runs in a container started with `--rm`, labels `jendock.run_id` and
`jendock.step_id`, `--security-opt no-new-privileges`, `--cpu-shares 512` (so production services
win under contention) and `--entrypoint sh -c <script>`.

- **Logical image** (built by `dockerBuild`, tagged `jendock/<project>-<env>-<image>:<sha12>-r<run
  number>` with labels `jendock.project|environment|build_run|commit`): runs with the image's own
  `WORKDIR`, user and no workspace mount. `dir()` becomes a `cd` at the start of the script.
- **External image** (`node:22`): pulled when not present locally (once per run), the workspace is
  mounted at `/workspace`, which is the working directory (`dir()` is relative to it), and the
  container runs as the worker's uid:gid with `HOME=/tmp` unless set, so the files it creates can
  be deleted afterwards.
- **Network**: the environment's `<compose_project>_default` network when the environment has a
  compose project and that network exists (otherwise a log line and the default bridge), the default
  bridge without a compose project, `--network none` when the agent args say so.
- **Limits**: `-e NAME=value`, `--memory` and `--cpus` from the allowlisted args. The defaults
  and the maxima are `JENDOCK_CONTAINER_MEMORY` (2g) and `JENDOCK_CONTAINER_CPUS` (2); a
  pipeline may lower them but a larger value fails the step ("exceeds the configured maximum").
  Builds use `--memory JENDOCK_BUILD_MEMORY` (2g).

## Secrets and the Docker client

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
- The docker client is `JENDOCK_DOCKER_BINARY` (default `docker`; set a non-default path in
  production) with `DOCKER_HOST` from `JENDOCK_DOCKER_HOST`. It always gets an explicit argv list.

## Workspaces and source

`<data_dir>/workspaces/<run_id>` is created 0750 and removed after the run, also on failure,
cancel and shutdown. `LocalGitSource` runs `git clone --no-checkout`, refuses a commit that
tracks `.env`, `id_rsa`, `id_ed25519`, `id_ecdsa` or `id_dsa` (before writing the tree), then
`git checkout --detach <sha>` and verifies `HEAD == sha`. The clone URL is
`JENDOCK_GIT_URL_TEMPLATE` (default `https://github.com/{repository}.git`, public repositories
only) for projects on the `https` transport; `ssh` projects use the deploy key (see `github.md`). A workspace
larger than `JENDOCK_WORKSPACE_MAX_BYTES` (2 GiB) fails the run; it is measured after checkout and
after each `sh` step (a single step can still exceed it until it ends). When the worker itself runs
in a container, `JENDOCK_WORKSPACE_HOST_DIR` must name the host directory mounted at
`<data_dir>/workspaces` because the host daemon resolves bind mounts (compose does this with
`WORKSPACES_HOST_DIR`, default `/var/lib/jendock-workspaces`, which must exist and be owned by
uid 10001). The worker image contains git and a pinned docker CLI.

## Logs

Output of each step goes to `<data_dir>/logs/<run_id>/<step_id>.log` (files 0640, directories 0750)
after secrets were masked **line by line** (a multi-line secret is also masked per line); a step
log stops at `JENDOCK_STEP_LOG_MAX_BYTES` (5 MiB) with one truncation marker. Known limits:
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
  comments every `JENDOCK_SSE_HEARTBEAT_INTERVAL` (15 s) and `end` when the run finished. It uses
  the background principal, so it never extends the session's idle timer; the session is
  re-checked (also without touching it) at each heartbeat and the stream closes when it expired.
  A stream lasts at most one hour (clients reconnect).

## Cancel, shutdown and crash recovery

`POST /api/runs/{id}/cancel` (scope `run`, CSRF and exact Origin for sessions, audited as
`run.cancel`): a queued run becomes `cancelled` immediately; a running run gets
`cancel_requested_at`. The worker watches that column every `JENDOCK_CANCEL_POLL_INTERVAL`
(1 s): it stops between steps, kills containers labelled `jendock.run_id=<id>` on every poll
(and terminates a running build), the run ends `cancelled`, `post` `aborted`/`always`/`cleanup` still
run. A finished run answers 409. On SIGTERM the worker interrupts its runs (`interrupted`, post
skipped) and waits up to 30 s.

At start the worker runs crash recovery: runs left `running` have their containers killed by
label and every workspace directory removed, then are marked `interrupted` with a message
(running stages/steps `interrupted`, queued stages `skipped`). It is idempotent.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JENDOCK_DOCKER_BINARY` | `docker` | docker CLI path |
| `JENDOCK_DOCKER_HOST` | `unix:///var/run/docker.sock` | daemon address |
| `JENDOCK_MAX_CONCURRENT_RUNS` | `1` | runs executing at once |
| `JENDOCK_PARALLEL_WORKERS` | `2` | threads per `parallel` group |
| `JENDOCK_CONTAINER_MEMORY` / `_CPUS` | `2g` / `2` | step container default and maximum |
| `JENDOCK_BUILD_MEMORY` | `2g` | `docker build --memory` |
| `JENDOCK_STEP_TIMEOUT_SECONDS` | `3600` | default limit of a container step |
| `JENDOCK_WORKSPACE_MAX_BYTES` | 2 GiB | workspace bound |
| `JENDOCK_STEP_LOG_MAX_BYTES` | 5 MiB | log bound per step |
| `JENDOCK_WORKSPACE_HOST_DIR` | unset | host path of the workspaces (worker in a container) |
| `JENDOCK_GIT_URL_TEMPLATE` / `_GIT_BINARY` | GitHub https / `git` | source clone |
| `JENDOCK_GIT_SSH_KEY_DIR` | `/run/jendock-keys/deploy_keys` | per-repository deploy keys `<owner>__<name>` (ssh transport); worker-only keys volume |
| `JENDOCK_GIT_SSH_KEY_FILE` | unset | optional single deploy key, used for repositories without their own key |
| `JENDOCK_GIT_SSH_KNOWN_HOSTS_FILE` | `/etc/jendock/github_known_hosts` | pinned GitHub host key |
| `JENDOCK_GITHUB_WEBHOOK_SECRET_FILE` | unset | webhook secret; worker-only keys volume |
| `JENDOCK_GITHUB_STATUS_ENABLED` / `_GITHUB_STATUS_TOKEN_SECRET` | `false` / `GITHUB_STATUS_TOKEN` | commit statuses |
| `JENDOCK_QUEUE_POLL_INTERVAL`, `_CANCEL_POLL_INTERVAL` | 1 s | worker polling |
| `JENDOCK_SSE_POLL_INTERVAL`, `_SSE_HEARTBEAT_INTERVAL` | 0.5 s / 15 s | event stream |
| `JENDOCK_FRONTEND_DIST` | unset | built web UI served by the API (the API image sets it) |
