# Workload build verifier configuration

The worker's ordinary image publication and idle publication recovery use the same
trusted verifier callback. Queue and scheduler artifact publication requires this
callback; ordinary application profiles do not instantiate it.

Set `WORKLOAD_POSTGRES_IMAGE` in the worker deployment environment to an approved,
locally available immutable PostgreSQL image ID or repository digest. Mutable tags
are not valid verifier dependencies. The Compose worker forwards this variable;
an empty value leaves workload verification unavailable without affecting ordinary
application publication. No public API, MCP argument or repository profile can
override this administrator setting.

The verifier uses the worker's existing `DOCKER_BIN`/`DOCKER_HOST` and stores private
durable ownership journals under `STATE_DIR/workload-verification` (the default
worker state directory is `/worker-state`). Preserve this directory across restarts.
Do not remove journals to retry an ambiguous operation: recover exact-owned Docker
resources first. The PostgreSQL fixture is disposable and must never be configured
with an existing application's database endpoint, volume or credentials.

Do not use `worker.run` for the candidate protocol. It is a deployment diagnostic
runner rather than the bounded, sanitized transport required by the verifier.

Artifact verification establishes compatibility with the supported protocol and
reviewed package in an isolated fixture. It does not establish that a deployed
service is operational or that its business jobs/schedules have completed. Those
outcomes require the runtime acceptance matrix in `../specs/workload-roles.md`.
