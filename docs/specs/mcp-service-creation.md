# MCP and dashboard project and service creation

Status: implementation design, not a delivered MCP endpoint or service provisioner.
Date: 2026-10-10.

This extends [the dynamic environment platform specification](dynamic-environment-platform.md).
The same authorized application operations must create projects and services from
the dashboard and an authenticated remote MCP client. Creating a project records
Development and Production without starting either. Creating a service can provision
an explicitly selected environment, with an explicit stopped or running outcome.
Ordinary authorized creation and deployment have no approval queue.

## Existing implementation and required work

The following is the local source boundary inspected for this design. It is not a
claim about the deployed server.

| Existing code | Current behavior | Required extension |
| --- | --- | --- |
| `registry.py` | SQLite projects, two environment slots, revisions, immutable imported recipe snapshots, unresolved legacy binding and private compatibility projection | Service ownership records and a verified environment provisioning operation |
| `server.py` registry actions | Session and CSRF protected project metadata creation, display name editing and environment branch/automatic settings | Thin dashboard adapter over the shared creation operations |
| `integrations.py` | Encrypted GitHub/Cloudflare credentials, verified replacement, local disable and sanitized capability metadata | Internal references from authorized repository/domain operations; never provider token passthrough to MCP |
| `store.py` | Legacy jobs, control runs and audit records; database-authoritative project projection | Durable creation operations, step journals, idempotency and environment-specific ownership |
| `control.py` | Existing container inspection and guarded control; immutable container-ID check | Operate newly provisioned services using persisted project/environment bindings |
| `resource_policy.py` | Revisioned RAM and CPU allocations | Transactional capacity reservations during concurrent service creation |

There is no implemented MCP server, MCP credential store, managed service creation
operation, role-template catalog or provisioning recovery flow in these modules.
The registry's `legacy-bound` state is not observed runtime readiness. Its
`create_project` currently stores a supplied repository identity; the new operation
must verify repository identity and accessible metadata before promising a connected
project. The GitHub adapter currently verifies user-token API and repository metadata;
clone and webhook management rights remain unverified.

## One application operation boundary

Introduce `application_operations.py` as the cohesive entry point for both adapters.
It accepts an immutable authenticated principal, typed input, expected revisions and
an idempotency key. The adapters do not execute Docker, construct Compose, inspect
private legacy JSON or decrypt provider credentials themselves.

The principal contains `subject_id`, `actor_kind`, optional `credential_id`, allowed
project/environment targets and granted scopes. Derive these from a verified console
session or MCP access token. Ignore any caller-supplied actor, owner, administrative
flag or credential scope. Record adapter/client identity separately from the actor.
Current console owner permissions must be represented by the same authorization
policy; a new MCP credential can grant a subset but cannot increase its owner's rights.

Proposed application methods:

```text
list_projects(principal, cursor, limit)
get_project(principal, project_id)
create_project(principal, input, idempotency_key)
list_services(principal, project_id, environment, cursor, limit)
get_service(principal, service_id)
create_service(principal, input, idempotency_key)
get_operation(principal, operation_id, after_event, limit)
```

All methods apply target authorization before exposing records, including list filters,
operation status and duplicate-request results. Paginated lists have a default limit
of 25 and maximum of 100. Immutable service IDs identify records; user-visible names
are unique within their project/environment. Actual container IDs and Compose ownership
are runtime bindings, not user-editable names.

Creation returns `{operation_id, status, resource_id, revision, detail, links}`.
Project metadata creation may finish in the initial transaction; it still records an
operation. Service creation returns a queued operation quickly, and the worker owns
its lifecycle. Dashboard progress and MCP status read the same durable event stream.
No request waits for a full image build, database provisioning or TLS issuance.

## Minimal MCP tools

Names and schemas are proposed application contracts. Advertise a mutating tool only
after its operation is implemented and validated end to end. Do not implement a tool
that returns success after merely recording an unsupported service request.

| Tool | Required input | Scope | Structured result |
| --- | --- | --- | --- |
| `project_list` | Optional cursor and bounded limit | `projects:read` | Authorized public projects and next cursor |
| `project_get` | `project_id` | `projects:read` | Public project, both environment slots, revisions and supported capabilities |
| `project_create` | ID, display name, GitHub repository ID/name or verified reference, integration reference, idempotency key | `projects:create` | Creation operation and inactive project identity |
| `service_list` | `project_id`, environment, optional cursor and limit | `services:read` | Public services, desired/observed state and next cursor |
| `service_get` | `service_id` | `services:read` | Definition revision, role, public bindings, runtime evidence and supported actions |
| `service_create` | Project, environment, expected environment revision, template/source/configuration fields and idempotency key | `services:create` | Durable provisioning operation |
| `operation_status` | `operation_id`, optional after-event cursor and limit | `operations:read` plus target access | Stage results, sanitized failure/recovery information and bounded events |

Use JSON Schemas with explicit types, bounded strings/arrays and
`additionalProperties: false`. Expose structured validation errors with a field path,
stable code and useful corrective message. Distinguish unavailable integration,
missing capability, stale revision, name/port/domain conflict, insufficient capacity,
forbidden production operation and uncertain external outcome. Return tool execution
errors using the SDK's supported error result; malformed protocol requests remain
protocol errors. A returned operation ID does not imply provisioning success.

Read tools are annotated read-only. Creation tools mutate infrastructure or metadata
and must not be annotated read-only. Their descriptions explain idempotency and
that `desired_state=running` starts the selected service. Tool annotations are hints,
not authorization. No credential reveal, arbitrary file access, host shell, Docker
socket, raw SQL, provider token replacement or upstream revocation tool is included.

## Shared service definition and validated templates

Both the dashboard form and `service_create` send the same versioned definition:

| Field | Validation and ownership |
| --- | --- |
| Project/environment | Existing authorized project; exactly `development` or `production`; expected environment revision required |
| Name and role | DNS-compatible bounded slug; role selected from application, database, queue or scheduler templates |
| Template | Approved template ID and immutable template revision; unsupported role is unavailable with a reason |
| Source | Either an approved image repository pinned to its digest, or a verified project repository/commit and approved build recipe revision |
| Command | Template-defined command profile plus validated typed arguments; resolved to container argv without host-shell interpolation |
| Ports | Template-approved container ports and protocols; no database port published publicly by default; host bindings reserved transactionally |
| Domain | Optional hostname plus authorized integration/zone reference; explicit adoption reference required for an unmanaged existing record |
| Secrets | Opaque, environment-scoped references; no literal credentials or automatic production-secret copying |
| Persistence | Template-defined mount destinations and new named volumes or explicit authorized same-environment volume references |
| Resources | Positive RAM MiB and CPU limits under environment/project budgets and current host capacity; no disk quota input |
| Desired state | Explicit `stopped` or `running`; default `stopped`; storing a project or definition alone cannot implicitly activate other services |

Initial templates should be narrow, versioned repository assets reviewed alongside
their provisioning adapters: an application image with health verification, a
PostgreSQL database, and only those queue/scheduler adapters for which real readiness
and ownership checks exist. A generic role label is not evidence that a queue or
scheduler is configured. Each template identifies supported health checks, ports,
data mounts, command profiles, secret types and recovery operations.

Do not accept arbitrary Compose YAML, host bind paths, host networking, privileged
mode, device mappings, added host capabilities, Docker socket mounts or an executable
host command. Source builds reuse isolated build/test stages; secrets are resolved only
at the execution stage that requires them. Database provisioning uses a bounded adapter
with explicit database identity. Application templates cannot bypass production
migration safety by inserting a reset command in an entrypoint. Where a safe execution
boundary cannot be established, creation/deployment fails before that operation.

For application commands, the UI can show the resolved argv and allow only fields
declared by the selected command profile. Repository-defined commands require a
verified recipe/template revision; MCP cannot supply arbitrary shell text as a shortcut.
Development fresh/refresh commands remain an explicitly configured migration policy,
never a creation default or retry fallback. Production destructive database deployment
operations remain forbidden regardless of adapter or calling client.

## Isolation and persistence model

Add `managed_services`, immutable `service_definitions`, `service_bindings`,
`creation_operations`, `operation_steps`, `operation_events`, `resource_reservations`
and `mcp_credentials` through versioned SQLite schema migrations. Each service and
operation records project/environment explicitly. Recipe and template revisions,
source SHA/image digest, principal ID and request fingerprint are pinned when accepted.

Service bindings record immutable container ID, Compose project/service, volume IDs,
network IDs, image digest and database identity where applicable. New environments
receive distinct Compose ownership, networks, volumes and database credentials.
Preserve one parent project grouping in the dashboard. Do not reuse or split an
existing shared Compose group by inference; legacy workloads require explicit verified
adoption. Every host resource mutation must recheck the expected owner and identity.

Store desired state separately from observed state and its timestamp. `not-provisioned`,
`provisioning`, `stopped`, `starting`, `running`, `failed` and `recovery-required` are
distinct service states. Environment overview derives its summary from real bindings;
partial or stale observations cannot produce a blanket Running/Ready result.

Reservations must include other accepted in-flight operations. RAM budgets are hard
ceilings; CPU allocations follow the declared host policy, with any oversubscription
explicit rather than inferred. Disk usage can block a build when available storage is
insufficient, but the product must not introduce disk quotas or move existing storage.

## Durable operations and idempotency

Use an opaque idempotency key of 16–80 permitted ASCII characters. The uniqueness
scope is principal plus operation type plus target project/environment, independent
of dashboard/MCP adapter. Store a canonical input fingerprint. Repeating the same key
and payload returns the existing authorized operation; reusing it for different input
returns `IDEMPOTENCY_CONFLICT`. A principal using both adapters can recover the same
operation after losing a response. Never resolve a guessed key into another actor's
records. Keep deduplication records at least as long as their operation and retained
resources; expiry cannot silently enable replay of a previously successful creation.

Acceptance is one transaction: authorize, validate revisions, reserve names/ports and
capacity, insert the resource definition, operation and actor-attributed event. External
calls occur outside SQLite transactions. A worker claims an operation using an atomic
state change and a renewable lease; only one worker can mutate the same deployment or
database identity. Bound host-wide build/provisioning concurrency.

Persist before-and-after evidence and resource identifiers for every step:

1. Resolve source/template and recheck revision, permissions and integration status.
2. Verify capacity, port/domain collisions, secret scope and database isolation.
3. Build/pull the pinned image, run isolated tests and record immutable artifact IDs.
4. Create owned network, volume and database resources as needed; journal each result.
5. Render the validated environment Compose configuration and create the stopped service.
6. If requested, start only the selected service and its explicitly declared new dependencies.
7. Verify image/ownership, health and the intended application/database behavior.
8. Apply requested DNS/routing/TLS steps through their own idempotent adapters and verify them.
9. Release temporary reservations, retain owned resource bindings and finish with evidence.

Service progress and domain readiness are separate. If the service runs but a requested
domain fails, record `recovery-required` or a clearly defined partial outcome and show
which dependency failed; do not mark the requested end-to-end operation succeeded.
No DNS write follows merely from connecting Cloudflare or creating a project.

An interrupted external write enters `outcome-unknown` for that step. Reconcile by
stored resource identity/ownership before any retry. A reused Docker name or unmanaged
DNS record is not proof that this operation owns the resource. Recovery may resume a
verified step or stop the newly created workload; it must not repeat ambiguous database
creation/migration writes blindly. Expose a later authenticated resume operation through
the same application boundary; do not invent an unimplemented retry button.

Rollback restores the prior service configuration only when the previous ownership
and revision still match. Never delete persistent database or application volumes to
make rollback look clean. Retain partially initialized data with its operation record,
show what remains and block conflicting reuse. Release only reservations proven unused;
retain bindings for existing resources. Cleanup failure has its own visible retryable
stage. A container rollback never implies database rollback.

## Remote MCP transport and authentication

Expose the SDK ASGI application behind the existing trusted TLS proxy at a dedicated
`/_deploy/mcp` route. The dashboard can remain on its current HTTP server while the
proxy routes MCP to a separate non-Docker process in the same product deployment.
Both processes import the shared application operations and use the same state store.
Do not hand-implement JSON-RPC framing inside `BaseHTTPRequestHandler` or give the MCP
transport process a Docker socket. The worker remains the only provisioning executor.

Pin an exact supported official SDK release and its tested protocol versions during
implementation. The current stable transport specification is revision 2026-07-28:
it uses POST requests, removes protocol sessions and the standalone GET stream, and
requires matching request metadata headers. Do not copy older FastMCP session-based
examples and claim current compliance. Use SDK negotiation/validation and test
`MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` mismatches. Application operation
IDs remain durable independently of transport connections. These protocol changes are
specified by the [current Streamable HTTP specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http).

Configure exact allowed Host and Origin values for the actual endpoint. Reject a
present invalid Origin with 403; reject `null` unless an explicit supported client
requires and constrains it. Non-browser clients may omit Origin but still require a
valid bearer credential. Browser clients receive only explicitly allowed CORS origins;
never wildcard credentials. Trust forwarded host/protocol headers only from the local
proxy, which replaces them. Local listeners bind to loopback or a private deployment
network without a public host port. Keep rebinding checks enabled at the adapter even
behind the proxy. The official [Python SDK deployment guide](https://py.sdk.modelcontextprotocol.io/run/deploy/)
documents the explicit transport-security allowlists required behind a real hostname.

Add a console-owned MCP access-credential management flow. Issue at least 256 random
bits, return the credential once through the authenticated HTTPS console response and
store only its SHA-256 digest, non-secret credential ID, owner, label, scopes, target
allowlist, expiry, created/revoked timestamps and bounded last-use information. High
entropy tokens do not need password-style hashing. Do not store an encrypted reversible
copy. Compare digests in constant time. Logs, audit, tracing, proxy access logs and
exception reporting must exclude Authorization headers, credential inputs and hashes;
audit by credential ID and principal only. The browser should not retain credentials
in localStorage. Revocation takes effect on the next tool request and before a queued
operation starts; a running mutation reaches a safe journaled boundary before stopping.

Suggested scopes are `projects:read`, `projects:create`, `services:read`,
`services:create`, `operations:read` and a separate `services:start` when creation
requests Running. Target allowlists constrain each scope by project/environment.
`projects:create` separately constrains repository owners/IDs and new project creation;
it does not grant control of arbitrary existing projects. No token inherits provider
credentials or console cookies. Recheck grants when executing accepted work so revocation
or reduced permissions cannot be bypassed by a long-lived queue.

Preconfigured opaque bearer credentials are a supported private-client bootstrap,
not a claim of complete OAuth onboarding compatibility. Full remote-client acceptance
also requires OAuth 2.1 resource-server integration: protected-resource metadata,
authorization-server discovery, resource/audience validation, expiration and scope
checks, and correct 401/403 challenges. Reuse a vetted SDK/provider authorization
implementation rather than inventing an OAuth server. Browser OAuth flows use PKCE;
provider GitHub/Cloudflare credentials never become MCP access tokens. The MCP
[authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
defines that interoperable contract. Test the chosen actual MCP client; a manually
configured bearer client alone does not prove an OAuth-only connector can connect.

Authenticate discovery and execution consistently. If protected-resource metadata is
public for discovery, it contains endpoint/scopes only, never projects or secrets.
MCP tool lists expose only supported authorized capabilities, and tools still enforce
authorization on invocation. Apply bounded body/result sizes, per-principal request
limits and pending-operation limits. Sanitized errors must not leak whether an
unauthorized project or service exists.

## Dashboard behavior

Project creation offers verified repository selection and makes the two inactive
environment slots visible immediately. The existing metadata-only creation remains
truthful while verification/provisioning is introduced; do not relabel it as a deployed
application. Each environment has Add service with a template-driven form, external
labels, contextual explanations, validation and a clear desired-state selector.

Show template/source revision, resolved command, secret-reference names, persistence,
RAM/CPU allocation, capacity observation time, domain adoption state and planned service
ownership before submission. Submission itself starts the authorized operation; there
is no additional deployment approval. Preserve drafts on live updates and errors.
After submission show durable progress with links to the service and operation.
Reloading or opening another device recovers the same result. MCP-created entities
appear here from the database without an adapter-specific refresh/import workflow.

## Implementation sequence

1. **Shared operations and authorization:** extract current project creation into the
   common boundary, add typed principals/scopes, repository identity verification and
   operation idempotency. Wire the existing dashboard first without changing its
   inactive-environment behavior.
2. **Service data and templates:** add owned service/definition/binding tables,
   template schemas and concurrent name/port/capacity reservations. Add sanitized
   service list/get APIs and dashboard form rendering from the same schemas.
3. **Provisioning worker:** implement one narrow application-image template and one
   database template with explicit isolation, durable step journals and non-destructive
   recovery. Add queue/scheduler templates only with their real adapters. Exercise
   stopped creation and explicitly requested startup on a local fixture workload.
4. **Dashboard end to end:** connect creation, progress, persistence/reload, errors,
   failed recovery and desktop/mobile behavior to the real worker. Verify that a new
   development workload cannot access the production database or volumes.
5. **MCP credentials and transport:** add scoped revocable credentials, SDK adapter,
   proxy route and Host/Origin protection. Pin SDK/protocol versions and exercise
   authenticated discovery and all seven tools with a real SDK client.
6. **OAuth interoperability:** integrate protected-resource discovery and the chosen
   authorization provider, then connect the actual intended remote MCP client.
7. **Cross-adapter acceptance and deployment:** run the matrix below locally, review
   packaging/rollback and then adopt the intended server deployment within its existing
   release process. Record local, published, running and live acceptance separately.

## Required acceptance evidence

| Scenario | Required observable result |
| --- | --- |
| Authenticated discovery | Actual SDK client discovers only the supported tools; missing, expired and revoked credentials receive correct denials |
| Origin and host protection | Spoofed Host, hostile/null Origin, untrusted forwarded headers and protocol metadata mismatches fail before an operation is inserted |
| Scope and target isolation | Read-only client cannot create; Development-only client cannot inspect/mutate Production; guessed operation IDs reveal no foreign state |
| Project creation | Verified repository identity, exactly two inactive slots, persistence after process restart, dashboard/MCP agreement |
| Duplicate and ambiguous response | Lost creation response plus retry returns one operation/service; different payload with the same key is rejected; no duplicate external resource |
| Concurrent provisioning | Competing names/ports/resources reserve atomically; stale environment/template revisions fail without Docker mutation |
| Real service creation | Actual container with recorded immutable ownership/image identity, requested stopped/running state, verified application health and persisted data |
| Environment isolation | Separate network/volume/database credentials and a real attempted cross-environment access denied; no production data copying |
| Production safety | Destructive/unclassifiable deployment migration rejected before execution; explicit development policy tested separately |
| Failure and recovery | Worker termination between each external step, uncertain outcomes, health failure and DNS collision reconcile without data loss or blind retries |
| Secrets | Token absent from SQLite plaintext, tool results, SSE, browser storage, traces and logs; MCP token digests cannot be used as bearer credentials |
| Cross-adapter consistency | MCP-created project/service visible and operable in dashboard; dashboard-created entity visible through authorized MCP list/get/status |
| Device and usability | Desktop/mobile rendered creation, pending, empty, failed, stale and recovery states; keyboard navigation and live updates preserve form drafts |

Unit tests and controlled provider fixtures establish internal behavior only. Working
service creation requires real local provisioning and persistence evidence. Remote
MCP acceptance requires a real authenticated client connection, and production
acceptance requires evidence from the actual running server after authorized delivery.
