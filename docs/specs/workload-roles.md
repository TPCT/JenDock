# Queue and scheduler workload roles

Status: implementation contract, not implemented or runtime acceptance evidence.
Date: 2026-10-11.

This extends [Dynamic projects, environments and delivery](dynamic-environment-platform.md)
and [MCP service creation](mcp-service-creation.md). It adds no deployment approval
step and does not narrow the platform to Laravel. The first verified adapter is
Laravel 12 with a PostgreSQL database queue; other Laravel queue drivers, other
frameworks and scheduler systems remain required follow-on adapters with their own
compatibility tests. A role label never implies an adapter is operational.

## User outcome and current seams

An operator creates separately resource-limited application, queue and scheduler
services from one verified release, sees their actual operational state, investigates
failed work and retries one authorized failed job. Both development and production
remain independently usable. Logs, terminal, lifecycle, files, resources, runtime
variables and application database connections remain available through existing
controls; role views supplement those controls.

Current implementation has these integration points:

- `build_profiles.py` emits only `application-v1` and validates a single command,
  health check and runtime user. Extend its versioned schema; do not reinterpret old
  artifacts or infer roles from command strings or container names.
- `managed_services.py` publishes verified artifacts, validates profiles, reserves
  capacity and saves immutable definitions. Extend its template catalog and public
  capability projection rather than add a parallel provisioning database.
- `service_provisioning.py`, `managed_rollout.py`, `managed_delivery.py` already own
  exact container identity, health, runtime references and rollback. Role profiles
  must participate in those same paths.
- `service_runtime.py`, `application_database_bindings.py` and the dedicated database
  bridge supply environment-scoped application credentials. Inspector and migrator
  credentials must never become queue/scheduler runtime credentials.
- `control.py`, authenticated worker RPC and `ContainerPage.jsx` are the existing
  operational boundary. Add shared role APIs and UI panels there; deployment jobs
  in `worker.py` are not evidence about customer application queues.

## Versioned artifact and template contract

Add a new explicitly versioned build profile schema supporting a map of role
profiles. Existing application artifact schemas remain readable unchanged. First
new templates are `laravel-database-queue-v1` and `laravel-scheduler-v1`.
Each profile pins template revision, role, image identity, resolved argv, runtime
user, health protocol, supported adapter protocol and instrumentation package
version/digest. The artifact records the tested Laravel version and instrumentation
compatibility result. A user selects a published profile, not arbitrary host commands.

Queue typed settings include an explicitly named database connection, bounded queue
names, worker timeout, retry-after, maximum attempts, sleep and shutdown grace.
Validate timeout strictly below retry-after with a defined safety margin; shutdown
must permit the declared maximum job duration. Scheduler settings include timezone,
registered application schedule source and overlap/cache capability. These settings
are immutable definition inputs, not untracked mutable container flags.

Build/test verifies the instrumentation package and command protocol in an isolated
fixture. Registration does not bootstrap candidate application code with production
secrets. Runtime application bootstrap occurs only inside the accepted workload,
with its ordinary application authority; this is distinct from the trusted migration
executor's prohibition on importing application code with migration credentials.

Service acceptance pins project/environment revisions, artifact/image digest,
profile/template revision, resource policy, runtime revision, database attachment
and adapter configuration digest. Recheck those pins before effects and when
reconciling. Missing instrumentation or unsupported framework/driver/schema returns
an actionable reason before creating an operational role. No public ports are
required. No Docker socket or broad legacy database network is exposed.

New services default to stopped as required by the service-creation contract.
Selecting running explicitly starts only selected services. Automatic image delivery
preserves each role's profile, configuration, credentials and stopped state. It must
resolve the corresponding role in the new artifact; disappearance or incompatible
adapter revision fails before cutover rather than substituting application argv.

## Actual instrumentation, not log inference

Ship a reviewed, versioned Laravel package with a closed protocol. It registers
queue and scheduler event listeners, a heartbeat, and a bounded read/action endpoint
inside the application's own trust boundary. Use a pinned helper/client to reach
that protocol on the exact registered container's loopback network namespace; no
public instrumentation endpoint, host shell or request-selected command. Authenticate
requests with a separate environment/service-scoped secret reference and nonce.
Peer container identity, image and accepted adapter digest are verified independently.

Persist instrumentation in an explicit environment-owned schema using application
credentials restricted to required objects. Install additive instrumentation tables
through the normal reviewed migration path, never implicit DDL on every read. The
package must neither alter existing queue payloads nor expose them to deploy-monitor.
Document retention and bounded cleanup for operational events independently of
business tables.

Queue observation returns:

- Configured connection/driver and queue names; ready, delayed and reserved counts
  from the actual accepted queue tables, with one observation time and bounded query
  time. Missing failed-job storage is unsupported instrumentation, not zero failures.
- Worker instance identity and fresh heartbeat, last completed/failed attempt and
  bounded counts. A live process with stale heartbeat is unhealthy instrumentation.
- Cursor-paginated failed-job IDs, queue, failure time and a sanitized classification.
  Never return serialized payloads, raw exceptions, stack traces or environment values.

Counts classify delayed and reserved jobs separately; an expired reservation is not
proof of failure. Laravel processing is at-least-once. The dashboard must not claim
exactly-once business execution or deduce success merely because a queue row vanished.
Completion evidence comes from actual observed worker events.

Scheduler observation returns stable instrumented schedule IDs, safe display names,
cron expression, timezone, overlap policy, last start/completion/outcome and next
scheduled time when computable. Schedule identity includes the accepted release
registry revision; a changed schedule cannot inherit another task's history by name.
Compute next occurrence with the supported framework's timezone semantics and report
that filters, locks, maintenance mode or daylight-saving changes may prevent actual
execution. Never present next occurrence as a guaranteed execution time.

Persist scheduler events for starts, finishes, failures and skipped work. A start
without completion after a crash remains interrupted/unknown, not successful.
Background tasks require their own completion evidence; unsupported background modes
must be explicit. Do not run scheduled tasks to populate a read-only dashboard.
Run one active scheduler per accepted schedule scope initially, fenced by a durable
scope reservation. Any later multiple-instance support must prove shared overlap
locking rather than assuming two containers are safe.

Laravel exposes queue job events and schedule event objects suitable for this
instrumentation; their presence does not itself implement durable monitoring.
See the official [queue event API](https://api.laravel.com/docs/12.x/Illuminate/Queue.html),
[schedule API](https://api.laravel.com/docs/12.x/Illuminate/Console/Scheduling/Schedule.html)
and [scheduled completion event](https://api.laravel.com/docs/12.x/Illuminate/Console/Events/ScheduledTaskFinished.html).
Pin and test the exact installed framework implementation before relying on command
output formats or event coverage.

## Guarded single-job retry

Retry is a write that may cause application business effects. It requires a dedicated
`workloads:retry` permission scoped to project/environment in addition to read access.
Add the permission to the authoritative scope catalog; existing credentials do not
silently gain it. Console owner authorization follows the existing explicit owner
bridge. No approval queue is added.

Closed request fields are `request_id`, `project`, `environment`, `service_id`,
`container_id`, `definition_digest`, `adapter_revision`, `connection_id`, `queue`,
`failed_job_id` and an opaque `failure_revision` obtained from the current read.
Reject unknown fields, unregistered queues, stale IDs and cross-environment targets.
Do not accept payloads, executable class names, SQL, connection URLs, retry-all,
clear-queue or delete-failure commands. The request contains no credentials.

Acceptance rechecks principal, binding, active configuration and the selected failed
record. Save a durable operation and canonical request fingerprint before dispatch.
The same actor/scope/request ID with different fields conflicts; exact replay returns
its original operation. Recheck authority immediately before dispatch. The worker
must not implicitly start a stopped consumer or its database.

For the first adapter, require queue, failure storage and retry receipt to use the
same accepted PostgreSQL database and transactional storage. Within a database
transaction, lock the exact failed record, validate its private revision, enqueue
using the tested framework-compatible retry transformation, remove the selected
failure record and save the unique retry receipt including the resulting queue ID.
All writes commit atomically. An unsupported custom failed-job provider, connection
or payload format is rejected before writes. Never unserialize/execute a payload in
the monitor or helper to perform retry; business execution belongs to the consumer.

This transactional adapter must be reviewed against the exact Laravel version,
including attempt reset and payload transformation semantics. Plain `queue:retry`
followed by an independent monitor receipt is insufficient proof of crash safety.
If atomic enqueue/receipt cannot be established for a driver, that driver's retry
stays explicitly unsupported until a suitable adapter exists.

After a lost reply, read the durable receipt; never blindly enqueue again. Return
`requeued` only with receipt and queue identity, distinct from subsequent `completed`
or `failed` execution. If the database cannot establish outcome, expose
`recovery-required` and block duplicate dispatch of that operation. Concurrent retries
of the same failure converge on one receipt or a conflict, never two new jobs.

## Shared API and UI flows

Use the existing authenticated HTTP/worker boundary and shared domain authorization
for console and MCP. Proposed actions are `workload-role-details`, `queue-failures`,
`queue-retry`, `workload-operation` and `scheduler-history`. All carry explicit
project/environment/service/container scope; cursors are signed and scope-bound.
Reads are bounded, default page 25/max 100, with fixed response and time budgets.
Errors contain stable codes and safe recovery guidance, not raw adapter output.

Service creation renders role profiles and typed fields from the catalog. Review
shows selected environment, release, role, resources, connection metadata and desired
state; submitting creates the durable operation without an extra approval gate.
Existing services expose Queue or Schedules next to generic tools only when role
metadata supports that panel. Adopted workloads need explicit verified instrumentation
association; naming a container “queue” is not sufficient.

Queue UI shows connection/queue filters, counts, worker heartbeat and paginated
failures. Retry one selected failure after showing its identity and explaining that
it can repeat business effects; the action itself is the authorized mutation, not a
pipeline approval stage. Persist the request ID before sending, recover after reload,
show requeued separately from processing/completion, and preserve selection/drafts
under polling. Do not offer an unauthorized button that only fails after navigation.

Scheduler UI shows registered tasks with timezone, last outcome and next occurrence,
plus bounded execution history. No arbitrary run-now or schedule editor is included
in this first adapter; schedule definitions continue through source/build/deploy.
Keep generic lifecycle actions visible and functioning.

Required separate states: loading, legitimate empty registry/queue, unsupported
adapter, instrumentation not installed, permission denied, dependency unavailable,
stopped workload, stale observation, identity changed and ambiguous mutation outcome.
Show `observed_at` and freshness explicitly. No cached count becomes a current zero;
no heartbeat becomes proof that a task or job completed. Environment switching cancels
reads and cannot retarget an in-flight mutation.

## Delivery, recovery and operational controls

Reuse current immutable service definitions and managed runtime operations for
settings/resource changes. Preserve application credential revisions for rollback.
Queue stop/restart observes configured graceful termination; interrupted work can be
redelivered according to framework semantics and remains visible. Health checks prove
both correct role process and recent adapter heartbeat, without executing business
work as a probe. Scheduler cutover stops the previous scheduler before starting its
replacement; recovery never starts another scheduler after an ambiguous start.

Safe stop must remain available with a stopped database or revoked credential after
exact application ownership verification. Start/restart require valid current
runtime dependency authority. Reconciliation observes actual identities and journals;
it does not replay uncertain retries, restart requests or scheduler execution.

## Implementation order and acceptance

1. Extend immutable artifact/template schemas, catalog and creation validation; test
   old application artifacts, missing profiles, changed role and stopped creation.
2. Implement the versioned Laravel package and bounded adapter protocol, event store,
   actual queue reads and schedule registry/history. Test against real Laravel 12 and
   PostgreSQL; document exact supported versions and custom-provider exclusions.
3. Implement transactional retry receipt and recovery, shared authorization/API/MCP
   actions, then role-specific UI using the same capabilities and safe projections.
4. Integrate ordinary multi-role image delivery, settings, lifecycle and rollback;
   run the complete local acceptance below before claiming the adapter supported.

Actual acceptance uses the product Compose project `deploy-monitor`, disposable
exact-owned fixtures and two simultaneous application environments. Preserve baseline
peer identities and business sentinels; record exact cleanup, source and image hashes.
No provider or production mutations are part of this acceptance.

- Create application/queue/scheduler from verified artifacts through authenticated
  APIs; inspect actual Docker argv/resources/networks and persisted/reloaded role UI.
- Dispatch real synthetic business work, observe depth, consumption and durable result;
  cause a controlled failure, inspect its sanitized row, retry through the UI/API and
  prove exactly one retry enqueue receipt and the subsequent business outcome.
- Exercise concurrent retry, changed request replay, lost response after commit,
  revoked authority, wrong environment, replaced container and unsupported provider.
  Prove no duplicate enqueue and no cross-environment reads/writes.
- Register a real recurring synthetic task, observe actual start/finish and persisted
  sentinel, failure and interrupted history; verify timezone/next occurrence and
  empty registry separately. Prove no duplicate scheduler during cutover/recovery.
- Stop one environment's workers while the other continues; preserve backlog and
  stopped state across a new release. Explicit start resumes selected work only.
- Deploy a changed artifact, then fail health and recover; verify role profiles,
  runtime settings, database identity, instrumentation history and peer preservation.
- Exercise browser desktop/mobile loading, empty/error/stale states, permission-hidden
  retry, operation reload and switching environments during reads/mutations.

Unit tests or generic container health alone do not satisfy this matrix. Broader
framework/driver support remains tracked as adapter work: Redis/Horizon and other
Laravel queue backends, other framework worker/scheduler protocols, and legacy
workload adoption. Each must expose comparable useful workflows with genuine runtime
evidence; this first adapter does not mark the full platform goal complete.
