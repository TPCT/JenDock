# Local queue and scheduler runtime acceptance — 2026-10-11

The actual two-environment runtime fixture passed in terminal session `15098`
(exit `0`). This is local Docker and PostgreSQL acceptance for the deployed
workload primitives, not browser or production acceptance.

## Verified outcome

The fixture built a real Laravel 12.69.3 application with the measured workload
instrumentation package and synthetic job/schedule source. It then used the real
build-profile, immutable job target, artifact publication and isolated verifier
path. It did not supply self-asserted role verification evidence.

For both Development and Production, it:

- Adopted an independent PostgreSQL container using the complete Docker inventory,
  an explicit service selector, and a distinct named local volume.
- Created stopped queue and scheduler services through application operations and
  the managed provisioning executor.
- Saved purpose-specific application database credentials and bindings; applied
  the encrypted runtime configuration/attachment; started each managed service
  through lifecycle operations and observed its health.
- Dispatched a real Laravel job and checked its downstream database sentinel.
- Dispatched a synthetic first-attempt failure, read the failed job through the
  real workload control/transport, retried it through the durable retry journal,
  and checked the resulting database record was completed with exactly two
  attempts. Original-operation receipt recovery returned `requeued`.
- Observed worker heartbeats, a named recurring scheduler sentinel, and completed
  schedule history through the real workload protocol.

The fixture checked disjoint Development/Production workload networks. It stopped
both Development workloads, then verified that the same Production containers
processed another job and emitted a later scheduler tick. The additional
Production job did not appear in the Development database.

Workload control used a live disposable console session and current owner
resolution against the private fixture SQLite database. Calls exercised the
shared control and Docker transport directly; they did not traverse an HTTP
receiver or browser.

## Attempts and corrections

1. The first command used system `python3`, which lacked `psycopg`. It failed at
   import before creating Docker resources. The rerun used the project's existing
   `.runtime/mcp-venv/bin/python`; no dependency installation was required.
2. Session `59127` built and independently verified the candidate and completed
   both adoption calls, but service creation rejected the environment as a
   non-managed workload. The collector determines database engine from the
   configured image name: a raw `sha256:...` image reference did not identify
   PostgreSQL, so the adoption contained no database proof. This was not treated
   as successful database adoption. All fixture containers, networks and volumes
   were removed; peer and source-hash equality passed.
3. Session `15098` reused the same candidate and launched PostgreSQL through a
   local immutable `postgres@sha256:...` repository digest, re-inspected to ensure
   it resolved to the required fixed image ID. All runtime and cleanup assertions
   then passed. No adoption validation or database ownership check was bypassed.

Before execution, review also caught that a direct PostgreSQL tmpfs would not
satisfy retained persistent-storage evidence. The fixture was corrected to named
volumes before the attempted run; this was not an observed runtime failure.

## Evidence and cleanup

[Machine-readable evidence](workload-runtime-evidence-20261011.json) records the
candidate and PostgreSQL image IDs, executed product source hashes, and outcomes.

- Candidate: `sha256:2670a4d3422f2faf26b5fc1872fe6361efaecbaf154ee730d87131091d5f675c`.
- PostgreSQL: `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`.
- Every recorded fixture container, application/database bridge, environment
  network and database volume was removed. Volume identity and lack of remaining
  users were checked before removal.
- The before/after peer container IDs, images and running states were identical.
  Executed source hashes were unchanged during the successful run.
- The runtime candidate image and earlier artifact-review candidate remain for
  follow-up work. No broad prune or unrelated-resource deletion occurred.

## Scope exclusions

This does not establish browser UI acceptance, GitHub webhook delivery, a real
repository build/deployment, production deployment, application rollout or
rollback, arbitrary Laravel job compatibility, or migration execution. The
synthetic job uses plain scalar properties and an explicit retry allowlist; the
scheduler uses one named minute task. Broader framework and workload behavior
remains outside this fixture's proof.

Reproduction entry point: `tests/live_workload_runtime.py`. Reserve an exclusive
local Docker window, use the existing project Python runtime, and reuse the
candidate with `--image` when appropriate. The script never calls a deployed
monitor or a production provider.
