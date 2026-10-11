# Workload image capability verification

Status: implementation proposal; the build hook and candidate-image acceptance are
not implemented. This does not establish runtime readiness.
Date: 2026-10-11. Extends [Workload roles](workload-roles.md).

## Current verified code boundaries

`worker.build_artifacts` validates the pinned recipe, builds images and runs the
source-owned test argv in a hardened container with `--network none`.
`worker.complete_build` records durable publication intent and calls
`artifact_publication.process`; the idle worker retries that same publication path.
Publication currently resolves Docker image IDs and publishes profiles. It does not
invoke `managed_services.verify_role_artifact`.

Role acceptance, provisioning and delivery require an immutable
`service_role_verifications` record. The existing worker-only callback seam compares
closed evidence with the artifact, but it does not itself prove who measured that
evidence. A callback returning the expected dictionary is a unit-test fixture, not
production verification. The missing implementation is the trusted measuring runner
and its integration in both publication paths.

The package's `deploy-monitor:protocol` capability request bootstraps the application's
own Laravel installation and requires PostgreSQL plus the instrumentation tables.
The present `workload-instrumentation/tests/live_laravel.py` uses a host PHP fixture
application and a disposable PostgreSQL container. It establishes package behavior,
not compatibility of a built application image. Its mutable PostgreSQL image tag is
not an acceptable production verifier dependency pin.

## Trust anchor: independently measured package bytes

The currently reviewed package version is `1.0.0`, framework `12.69.3`, with package
digest `b0a6dcf746f0e2a22393bd9ed44e6e020e1e94965b46bf7c3b08f854006aafa5`
(recorded in `workload-instrumentation/README.md`). Implementation must remeasure this
package before adopting that value; changes require review and a new explicit trust
allowlist entry, never automatic trust in the latest repository contents.

A requested role's package digest must be in the administrator/worker release-owned
allowlist. Matching a user-declared digest to the same value echoed by candidate PHP
is insufficient. Independently read the installed package from the immutable candidate
image and compare its exact file manifest to the reviewed package manifest.

Resolve the application root from the pinned image's normalized absolute WorkingDir,
where the existing fixed `php artisan` argv executes. Require the standard Composer
installation at `vendor/shiftcodes/deploy-monitor-workloads`; unsupported layouts
fail clearly rather than searching arbitrary filesystem paths. Use an exact owned,
stopped inspection container and bounded Docker archive output. Parse archive entries
in memory: reject traversal, links, device files, duplicate paths, unexpected files
within the measured directories and byte/file-count overflow. Never extract candidate
archive paths into the host filesystem.

Measure `composer.json` and every regular file under `src`, `config` and `database`
using the same sorted canonical manifest algorithm as `PackageIdentity`. Compare to
the independently bundled reviewed manifest, not only to the candidate's implementation
of `PackageIdentity`. Pin framework installation metadata and check the live protocol's
exact framework, package, role, protocol revision and supported operation set too.

This is package-integrity and compatibility evidence. Arbitrary application bootstrap
is still application code: it could replace bindings or falsify its own output.
The isolation boundary must remain safe even for hostile candidate code, and acceptance
must not claim that a successful handshake proves application honesty or future
business outcomes. Runtime identity, credentials and health remain separately checked.

## Smallest isolated candidate-image fixture

Introduce `workload_artifact_verifier.py` with an administrator-configured PostgreSQL
image digest, Docker executable/socket and private journal root. Missing or untrusted
image pins fail before creating resources. No automatic pull of a mutable tag.

For each immutable image/workload configuration:

1. Persist exact job/image/workload identity, random ownership nonce, resource names
   and creation intent before effects. All disposable containers have product Compose
   label `deploy-monitor` plus exact verifier/job/nonce labels. No separate product copy.
2. Start the pinned PostgreSQL fixture with `--network none`, no ports or host mounts,
   bounded RAM/CPU/PIDs, ephemeral tmpfs database storage and fresh private credentials.
   Verify the created ID, image and security/network configuration. Never reuse an
   existing application database or an unjournaled similarly named container.
3. Initialize fixed trusted queue and instrumentation schemas and a sentinel using
   the trusted PostgreSQL client inside that exact fixture. SQL comes from the reviewed
   verifier/package, not arbitrary request text. Create a separate non-superuser fixture
   application account with only required table/sequence privileges. Candidate code
   never receives the fixture administrator credential.
4. Start the candidate image in the fixture database's exact network namespace
   (`--network container:<verified-fixture-db-id>`). Because the database itself has
   network `none`, this provides only fixture loopback, without a broad Docker bridge,
   host publication or access to production endpoints. Verify both namespace identities.
   Do not attach the candidate to an existing managed/application/database network.
5. Override candidate ENTRYPOINT with the fixed PHP/Artisan protocol invocation.
   Run as the accepted non-root UID:GID, read-only rootfs, no added capabilities,
   no-new-privileges, no Docker socket, no host files, no production secrets and bounded
   tmpfs. Inject only fresh fixture APP_KEY/database credentials, testing environment,
   and generated trusted workload metadata. Avoid raw environment contents in logs.
6. Issue the actual capability request to the candidate's own `artisan` bootstrap;
   validate the complete protocol response against the independently measured package
   and declared role. Also issue the supported empty observation: queue counts must
   match the fixture schema; scheduler may honestly report `registry_not_observed`
   without executing schedules. Do not run business schedules/jobs as a capability probe.
7. Verify the sentinel and expected fixture schema remain intact, then remove only
   exact journal-owned candidate/inspection/database IDs. Verify removal before marking
   cleanup complete. No volume prune, name-only deletion or implicit peer restart.

All subprocesses need bounded stdin/stdout/stderr and a total deadline, not merely
per-command timeouts. Use the existing bounded exchange primitive, sanitized error
codes and exact-ID inspections. A killed Docker client does not prove a server-side
create or candidate process ended. Reconcile that journal and terminate/remove only
its verified owned container before releasing resources or trying a new attempt.

## Application bootstrap and fixture configuration

Normal Laravel applications can use custom connection names, cached configuration,
extra bootstrap dependencies or nonstandard schema. The verifier must not replace
their config with a synthetic fixture application and call that image-compatible.

The first implementation requires a documented conventional testing contract:
`APP_ENV=testing`, fresh `APP_KEY`, `DB_CONNECTION=pgsql`, loopback DB_HOST/DB_PORT and
fixture DB_DATABASE/DB_USERNAME/DB_PASSWORD. The role's accepted queue connection or
scheduler database connection must actually resolve to that fixture connection.
The protocol must reject mismatched queue driver, retry-after, failed-job provider,
connection, timezone or installed schema. Cached production configuration that ignores
fixture injection fails safely in the isolated namespace; do not clear caches by
running an unapproved project command or mount over configuration to force success.

A source-owned verification configuration is necessary for applications needing
additional nonsecret bootstrap values. Add it explicitly to the versioned build
schema, pinned into the job target: a bounded literal `fixture_environment` map only,
with no interpolation, shell commands, files, secret references or host endpoints.
Forbid overriding generated APP_KEY, database endpoint/credentials, Docker settings,
PATH and DEPLOY_MONITOR metadata. Document that these values are public build inputs
and must contain no real credentials. The default map is empty; normal supported
applications need no extra setup beyond their existing testing configuration.

Do not add arbitrary fixture SQL or a general setup command to this slice. Fixed
trusted queue/instrumentation tables suffice for capability verification. An app that
requires business tables or external services during bootstrap receives a concrete
unsupported-bootstrap error. The follow-on extension can use the existing typed
additive migration executor against only this ephemeral database, with explicit source
paths and fixture seeds represented as bounded typed data. That extension must be
implemented and tested before advertising those applications as supported; it does
not remove them from the wider platform goal.

Writable cache/log paths likewise require an explicit trusted template contract.
Prefer standard Laravel paths redirected to bounded `/tmp` locations through supported
testing settings. Do not silently add arbitrary writable mounts or change accepted
production security configuration to make the build probe pass. Differences between
probe and runtime configuration must be recorded and validated, not hidden.

## Durable publication hook

Extend `artifact_publication.process` with a trusted worker verifier dependency;
HTTP/MCP requests cannot supply it. Both immediate completion and idle/restart retry
must use the same dependency. Resolve immutable image IDs first, then verify each
role outside SQLite transactions. Key verification journals by source job, immutable
image ID, workload digest and verifier version; never by mutable image tags alone.

Only after all required profiles pass and owned cleanup is confirmed, commit artifact
rows, their validated verification evidence and publication success together. Preserve
existing atomic multi-profile publication. Refactor the current verification evidence
writer into a private validated insert usable by that transaction; do not nest a
long-running verifier in `registry.transaction`.

Failed verification remains visible as a bounded publication/capability phase with a
safe reason. It must not turn into a successful role artifact, and unverified artifacts
must not be accepted. Images and journals needed for unresolved cleanup remain protected
from pruning. Known terminal incompatibility should wait for a new source/profile
revision rather than continuously rebuild the same fixture. Proven clean transient
failures can have bounded retries; ambiguous effects require observation first.

Successful evidence means `artifact-verified`, with runtime `operational:false` until
actual service health/heartbeat and runtime protocol checks succeed. Do not relabel the
whole build/deployment Ready because the isolated fixture passed.

## Required next implementation and real acceptance

Implement the verifier, journal/recovery, trusted package manifest and pinned dependency
configuration first, then the publication hook. Build a real Laravel candidate image
with the reviewed package and run the normal queued build/publication path. Do not stop
at the existing host-PHP fixture or mocked evidence callback.

Prove exact package tampering rejection, false echoed digest rejection, changed image
ID rejection, unsupported bootstrap failure, output/timeout bounds, lost create/start
recovery and cleanup after failure. Assert no production/environment vault lookup, no
host mounts, no public ports and no broad network membership. Preserve peer identities
and report exact cleanup separately. Exercise publication restart and multi-profile
atomicity, then create the verified role through the actual API and prove runtime
operation separately against its authorized environment database.

No production changes, live-provider mutations or new approval gate are introduced.
