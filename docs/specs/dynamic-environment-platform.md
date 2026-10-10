# Dynamic projects, environments and delivery

Status: proposed architecture for review; not implemented or deployed.
Date: 2026-10-10

## Product outcome

The monitoring application becomes a container delivery and operations product.
An administrator can connect GitHub and Cloudflare, register a repository, configure
two independent environments and inspect or operate their actual workloads from
the UI. Adding a project or changing its branch must not require editing Python,
JSON files or rebuilding the monitoring application.

## Source of truth and import

The application database owns projects, environments, delivery recipes, service
bindings, integrations, domains, secrets references and operation history.
`projects.json` becomes a one-time legacy import input, never an ongoing override.
The importer is idempotent and records provenance and conflicts. Import the actual
saved settings and observed Docker ownership, not just the JSON defaults. Preserve
existing opt-outs, history, credentials references, volumes, networks and databases.
Existing run records stay associated with their original environment and commit.
No global label or branch name can silently reclassify an existing database.

Every project has development and production environment records. Each
owns its branch, automatic-deploy switch, recipe revision, domains, secret references,
container/service bindings, resource limits, database endpoint identity and runs.
Creating an environment record does not provision containers, databases or DNS.
Show Not provisioned as a legitimate state and expose a separate provisioning flow.
Import the 20 live deployments only; leave unprovisioned environment slots inactive.

Projects are not classified as development or production. Every project exposes both
environments. Import existing deployments without inferring environment ownership
from the former 15/5 project classification, branch names or APP_ENV alone. Preserve
runtime configuration until each existing deployment has an explicit environment
binding. Do not provision a second copy or move a database as a side effect of import.

An environment can be Not provisioned, Stopped or Running. Provision/start is an
explicit operator action. A push must not provision or wake a stopped environment
unless that environment's persisted policy explicitly enables that behavior. Stop
preserves database/storage volumes. Optional idle shutdown excludes environments
that have active builds or operations. Show per-environment resource limits and
observed host capacity before activation.

## Git and delivery

Repository branches use development and production. Preserve existing
branch contents and unrelated work. Missing branches start from the repository's
currently tracked branch, with the source SHA recorded. Preserve legacy branches
until unique commits and other consumers are assessed. Changing a default branch
or creating refs is not a merge, a deployment or automatic promotion.

Default binding is an environment's matching branch. An administrator can configure
bindings in the UI; deployment always targets an explicit environment and recipe
revision. Build-only runs can validate other branches without changing services.
A webhook resolves repository plus branch to enabled environment bindings and
queues durable, idempotent runs. Serialize changes to the same deployment/database
and bound concurrency across the shared host. Branch movement supersedes obsolete
unstarted runs; a build artifact remains pinned to its immutable commit.

Pipeline stages: source, build, isolated tests, database migration, service cutover,
health verification, cleanup. Stage results, timestamps and sanitized logs are
persisted and streamed. A failed stage cannot be shown as succeeded or silently
skipped. Cleanup failure is independently visible and retryable. A newer branch
commit must not substitute different code mid-run.

There is no approval subsystem. Ordinary delivery runs automatically. Production
forbids database resets, destructive migrations and data-deleting deployment steps.
A forbidden or unclassifiable production database operation fails with a concrete
reason before it executes; this is not an approval request. Regex searches alone
cannot establish safety for arbitrary PHP/Python/SQL. Production migration adapters
must either enforce a verified non-destructive execution boundary or fail closed.
Application business operations are outside this deployment migration policy.

Development supports its configured migration command, normally pending migrations.
The owner also permits fresh/refresh in development when explicitly configured; these
must never be introduced as an automatic default or fallback after a failed migrate.
Framework is discovered/configured per repository: Laravel, Alembic, other migration
runners are separate adapters; FastAPI does not itself imply Alembic.

Inspect pending files, their contents/diffs and the target database identity before
execution. Display that evidence in the run. Never infer database rollback from an
image rollback; after partial schema changes report the actual failure and recovery
requirements. Validation-only runs never receive the live database credentials.

After verified deployment, delete unreferenced superseded images and unused build
cache without force-deleting referenced images or pruning database/storage volumes.
Pending operations and needed artifacts must be considered when deciding retention.

## Cloudflare integration

Integrations offers connection, token verification, account/zone selection and
capability status. The token is entered into a protected UI or referenced from an
existing secure file, never pasted into chat or placed in URLs or logs. Persist
an encrypted secret reference; keep the encryption key outside the database.
Request only the zone/DNS permissions the selected integration uses.

An environment's Domains page selects a zone, hostname, record type, target, TTL
and proxy setting. Resolve and display current DNS before changing it. Manage DNS
records by stored Cloudflare record ID and ownership. A collision with an existing
unmanaged record requires explicit adoption; never silently overwrite it. Connection
alone does not change DNS. Use bounded retries and reread after ambiguous API outcomes.

Provisioning coordinates DNS, origin routing and TLS as distinct observable steps.
A proxied Cloudflare DNS record does not prove origin routing or TLS readiness.
Show failures and a resumable recovery path; do not label a domain Ready until
its intended application and TLS endpoint have been verified.

## Container operations

Environment Services groups actual containers by project and role. Bind operations
to immutable container identity and reject stale/replaced targets. Import the current
Compose groups without silently splitting shared groups. New environments use
separate networks, volumes and database credentials; never inherit live production
database endpoints by copying development configuration.

All roles: health/resources, actual image/revision, events, bounded live logs,
start/stop/restart and an authenticated, session-owned terminal. Do not label a full
shell as read-only. The application must honestly distinguish its pipeline safety
policy from actions a privileged shell or database administrator can perform.

Application: scoped file browsing with size/type limits and path/symlink escape
protection. Distinguish image files, ephemeral writable layer and persistent mounts.
Source edits must flow back through Git/build; transient container edits are not a
successful durable release. Mask secret files and avoid bulk content collection.

Database: schemas, tables, columns, keys, indexes and paginated data with real database
read permissions, query timeouts and result limits. Never load a whole database into
browser memory. Authorized query execution and any future writes use explicit target
identity; production destructive administration is not exposed as a normal action.

Scheduler/queue: adapter-backed registered schedules, timezone, last outcomes and
next execution when computable; queue depth, failed jobs and guarded retries. Missing
instrumentation is shown explicitly, not fabricated from container health alone.

## UI

Retain the approved React/GitHub Actions visual system, responsive sidebar and
breadcrumbs. Navigation: Projects, Runs, Services, Integrations and Settings.
Project workspace has Development / Production environment navigation.
Each environment exposes Overview, Pipeline, Services, Database, Domains, Variables
and Activity when supported. Integration secrets never appear in the global SSE
state. Controls have loading, empty, failure and stale states; live updates preserve
form drafts, focus and scroll. Required inputs use validation without visible stars.

## Acceptance and implementation sequence

1. Database-backed project/environment registry and idempotent legacy import.
2. Environment-aware settings, webhook routing, run history and service ownership.
3. Versioned pipeline recipes and framework migration adapters with production guards.
4. Integrations and resumable Cloudflare domain/routing/TLS management.
5. Role-specific container tools and isolated database browsing.
6. End-to-end local acceptance, then adoption of the existing server deployment.

Required evidence includes persistence after reload, webhook deduplication, environment
isolation, forbidden production operations rejected before mutation, development migration
success/failure, stale identities, secret redaction, integration errors and uncertain
write recovery, cleanup results, and desktop/mobile rendered flows. Passing mocks or
builds alone is not operational acceptance. Do not provision all 40 environments as a
side effect of migrating configuration; capacity and requested activation matter.

## Integration credential management — owner clarification

Settings > Integrations provides separate Cloudflare and GitHub connections.
Each connection supports saving/replacing a credential, testing it, disabling its
use by this product, and inspecting sanitized connection metadata. A disconnect
must not silently revoke the upstream credential, since other consumers may use it.
Replacement is tested before becoming active; a failed replacement preserves the
previous usable connection. Never send stored tokens back to the browser; show a
configured state and replacement input, not a reveal button. Record actor/time and
operation result without credentials, Authorization headers or upstream raw bodies.

Cloudflare must support account-owned tokens as well as user tokens. User-token
verification failure alone is not an invalid-token verdict. Verify the applicable
account endpoint and enumerate authorized zones; report only capabilities actually
verified. GitHub connection testing must distinguish API access, repository access,
clone access and webhook-management rights. Do not silently replace working SSH
checkout authentication or import the workstation's gh credential into the product.

On 2026-10-10 the supplied Cloudflare credential was tested read-only against the
official API: account-token verification succeeded with status active, and zones
listing returned four active zones. No DNS records or account configuration changed.
The credential value is deliberately absent from this document and Git. Credential
management UI, encrypted persistence and production integration remain unimplemented.

Resource scope clarification (2026-10-10): configurable limits cover RAM and CPU only. Disk usage remains visible for monitoring; no disk quota setting or storage migration is required.

## MCP and dashboard creation — owner requirement

The product must support creating projects and services both from the dashboard and
through an authenticated MCP interface. Both adapters call the same application
operations, enforce the same validation/ownership/resource/environment constraints,
and persist the same actor-attributed audit records. A metadata-only project record
is a first step, not acceptance of a working provisioned service.

Project creation selects a GitHub repository (or verified repository identity) and
creates Development and Production records without automatically activating either.
Service creation explicitly selects project/environment, service role, image or
build source, command, ports/domain, secret references, persistence and RAM/CPU limits.
Validate naming/port/domain collisions, available host capacity, environment isolation
and database identity before provisioning. Never clone production credentials or
volumes into Development by default. Show operation progress and recovery on failure.

MCP discovery and tool schemas must describe real supported actions and return
structured operation IDs/statuses. Provide project list/get/create and service
list/get/create, plus operation status; reuse idempotency and optimistic revisions.
Authenticate remote MCP separately with revocable scoped credentials; never bypass
console authorization, expose an unauthenticated Docker interface, accept raw host
shell commands as service definitions, or return integration/environment secrets.
Test authenticated discovery, permission denial, invalid input, duplicate retries,
creation persistence, actual provisioned service ownership, and dashboard visibility
of MCP-created entities (and vice versa).
