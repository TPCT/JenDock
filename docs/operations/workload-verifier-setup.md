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

## Opt-in console and MCP runtime channels

Build verification and runtime access are separate capabilities. A successful
artifact probe does not enable private console/MCP peer access or grant retry
permission. Both channels remain disabled by default in the Compose configuration.

For the packaged receiver (`USER 65534:65534`), explicitly set
`WORKLOAD_RECEIVER_UID=65534` in the Compose deployment environment. `compose.yml`
passes that value to the worker; an unset or empty value fails the worker's peer
check. The receiver verifies the worker's kernel UID using
`WORKLOAD_WORKER_UID=0`, matching the packaged worker's `user: 0:65534`.

To additionally enable workload tools in the optional `compose.mcp.yml` overlay,
set both `WORKLOAD_MCP_UID=65534` and
`MCP_WORKLOAD_SOCKET=/state/control.sock`. The overlay passes the former to the
worker and the latter to MCP. MCP also verifies worker UID 0. Leaving the socket
empty suppresses workload tool advertisement. Configuring only the socket while
leaving the peer UID empty still denies worker access; neither value is a grant.
The packaged MCP image also uses UID/GID 65534. If deployment overrides process
UIDs, configure the corresponding expected peer UID explicitly; do not broaden
socket permissions or remove kernel identity checks.

All three processes already mount the same `state:/state` volume. The existing
worker creates `/state/control.sock` with mode 0660 and worker group 65534. No new
public listener, socket mount, Docker socket access, database scope or database
inspection key is required for workload reads/retries. Keep the existing shared
credentials file mounted so each boundary can revalidate the current owner.

Issue new MCP credentials with explicitly selected `workloads:read` and exact
project/repository/environment grants. Retrying also needs `workloads:retry`.
Existing tokens do not gain these grants. `MCP_ENABLE_CREATION` controls creation
operations separately; it does not authorize failed-job retry. Runtime authority
and exact service/container/definition checks still apply after channel opt-in.
These settings document packaging, not production activation or runtime acceptance.

Runtime protocol calls revalidate the retained database candidate and its accepted
bridge before invoking the application, and repeat target verification around
protocol dispatch. Docker inventory, volume identity, namespace start identity,
original networks and sibling bridges are checked through the same configured
Unix socket and bounded request deadline. A recorded bridge descriptor alone is
not current database identity. Compose provenance uses the configured standalone
Compose executable with the same socket and deadline.
