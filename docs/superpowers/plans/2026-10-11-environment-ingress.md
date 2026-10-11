# Environment Ingress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Parallel workers may be used only under the owner's existing explicit delegation authorization, with non-overlapping file ownership. Do not create branches or worktrees for this plan.

**Goal:** A user can connect an existing running managed application to its environment-owned hostname, complete DNS/origin/TLS setup, open the verified application, and recover interrupted setup without duplicate DNS records or modifying adopted sites.

**Architecture:** Preserve the existing host Apache as the public HTTP/TLS edge. Each environment receives a managed HTTP gateway attached to that environment's private application network and its own gateway-only transport bridge, publishing one reserved loopback port. Apache proxies explicitly owned hostnames to that port. SQLite owns revisioned route definitions, provider identities, phase journals, port reservations and readiness evidence; restricted workers and a narrow host helper perform effects.

**Tech Stack:** Existing Python/SQLite worker and application operations, React/MUI, Docker Compose, pinned unprivileged Nginx gateway image, existing host Apache, Cloudflare DNS API, and a bounded ACME/certbot host adapter.

**Spec:** `docs/specs/dynamic-environment-platform.md`; `docs/superpowers/plans/2026-10-10-platform-completion.md`. Later owner corrections and the existing managed runtime contract take precedence over historical status paragraphs.

## Global constraints

- Work in the existing checkout/current branch; preserve unrelated changes. This implementation checkpoint makes no production changes; use the owner's existing deployment authorization, verified host identity and concrete reviewed configuration for the subsequent production rollout.
- Development and Production must serve different applications simultaneously. Do not provision or wake application containers while configuring a route.
- No CI/CD approval subsystem. Explicit user creation/adoption of a domain is a configuration action, not a pipeline approval gate.
- Preserve all existing adopted Apache sites, certificates, ports, Docker networks, volumes and databases. Do not repurpose `configure_domain.py` as a general remote shell.
- No database migrations/resets, public database ports, arbitrary upstream URLs, arbitrary host paths or command arguments are introduced.
- Secrets remain in the encrypted integration store/host certificate store, never in SQLite operation results, browser state, audit, logs, command lines or MCP results.
- RAM/CPU reservations include gateways; disk remains monitoring only. Use the actual product's Compose project name and distinguish infrastructure services from application services explicitly.
- DNS configured, origin verified and TLS verified are independent persisted facts. Only current evidence for all required phases may produce Ready.

## Review focus

- A lost Cloudflare response must trigger a read of the exact hostname/type and operation ownership, never blind creation of another record.
- A container replacement or changed application port must not silently redirect an existing route to another service/environment.
- A host/domain/loopback-port collision must leave existing adopted sites and listeners byte-for-byte unchanged.
- Gateway membership must not weaken the private application's no-published-port/no-cross-environment-network contract.
- A reload, process crash, certificate renewal or stopped application must preserve honest phase status and a usable recovery action.

## Existing integration boundaries

- `build_profiles.py`: strict v1 source/profile schemas contain command/healthcheck/user but no declared HTTP endpoint.
- `managed_services.py`: `_profile`, artifact publication/verification, service definitions and capacity accounting need the versioned endpoint metadata; no arbitrary image/command entry replaces trusted artifacts.
- `artifact_publication.py`: copies the trusted profile into immutable artifacts and must preserve endpoint identity too.
- `service_provisioning.py`: `service_config` emits no ports; `_verify` rejects all `PortBindings`; `_network` requires the application network to be internal. Keep these application invariants.
- `managed_rollout.py`: exact peer/manifest/network membership checks must recognize separately verified ingress infrastructure without treating it as an application replacement target.
- `managed_delivery.py`, `compose_reconciliation.py`, `ownership.py`, `workspace.py`: preserve target ownership and expose ingress state without counting a gateway as an application build target.
- `application_operations.py`: shared principal/scopes/idempotency/operation status; `server.py` and `mcp_server.py` must delegate to the same operations.
- `integrations.py`: `credential_for_use` supplies Cloudflare credentials; existing GET adapter verifies zones but implements no DNS write contract.
- `configure_domain.py`: existing Apache deployment utility is hardcoded to monitoring.shiftcodes.net. `deployment/apache-mcp.conf` demonstrates narrowly scoped additions. Both remain unchanged for existing sites.
- `compose.yml`: worker has Docker socket/project root access but lacks a bounded Apache/certificate control interface. Add a Unix-socket host helper; do not mount all `/etc` writable or accept raw commands.
- `frontend/src/main.jsx`: project tabs need Domains. Add a focused component rather than embedding the workflow in BuildDialog or ServiceCreation.

## Routing decision and ownership

The route chain is `client -> host Apache :443 -> 127.0.0.1:<reserved gateway port> -> environment gateway -> Compose service DNS name:<trusted HTTP port>`.

The application remains attached only to its existing internal network. The gateway alone joins that network and a dedicated transport bridge used for loopback publication; no shared gateway bridge connects Development and Production. Gateway config names the immutable selected service identity via its verified Compose service name. Network observation must verify the gateway's exact container/network IDs and ownership labels; never allow an arbitrary extra member by name prefix.

Use one gateway per project/environment, with multiple hostname routes permitted. The gateway runs from a pinned image digest, non-root UID, read-only root, dropped capabilities, no Docker socket, bounded tmpfs and explicit RAM/CPU limits. It has its own generated manifest under the managed environment directory and `<environment>-ingress` Compose service name under the product's actual Compose project. Every Compose invocation uses the explicit correct manifest; never use `--remove-orphans` across the application/gateway files. Registry ownership records classify the gateway as infrastructure so application builds do not replace it.

The host helper owns only files named by opaque route IDs under a dedicated managed Apache directory and certificate references it issued/adopted explicitly. It accepts structured operations over a permission-restricted Unix socket, validates callers and bounded schema, independently rechecks route IDs/digests, and executes fixed command arrays. Configure its socket mount into the worker only. The helper uses actual host `apachectl configtest` against the complete configuration before graceful reload; an error restores the prior managed candidate before any reload. Inspect effective vhosts/listeners before reserving a hostname. A conflict offers explicit adoption only where a separately reviewed import contract exists; initial release rejects foreign Apache vhost adoption and preserves it.

## Task 1 — Versioned trusted HTTP endpoint and route intent

Files: modify `build_profiles.py`, `managed_services.py`, `artifact_publication.py`, `frontend/src/buildProfile.js`, `frontend/src/BuildProfile.jsx`; create `ingress.py`, `tests/test_ingress.py`; extend profile/artifact tests.

- [ ] Add failing tests for a v2 profile containing optional `http_endpoint={port: integer 1..65535, health_path: normalized absolute path}`; v1 artifacts remain valid and have no routable endpoint. Reject URLs, credentials, control characters and query-bearing health paths. Do not infer a port from Docker EXPOSE or a command string.
- [ ] Implement closed versioned schemas and copy endpoint metadata through source recipes, fingerprints, artifacts and service definitions. Public profile UI allows an explicit port and health path; blank means no HTTP endpoint. Old stored recipes remain immutable.
- [ ] Add `ingress.preview(db, principal, project, environment, values)` and `ingress.accept(..., idempotency_key)` around revisioned immutable definitions. Fields: service ID, service definition ID/digest, hostname, zone ID, DNS type/target/proxy/TTL, expected environment/source/integration revisions and preview fingerprint. Target address must match configured host origin identity; no arbitrary origin SSRF.
- [ ] Add tables for route definitions, current routes, operation phases, DNS ownership, loopback port reservations and gateway ownership. Enforce normalized unique hostname and atomic port reservations across environments; same idempotency key/different payload rejects.
- [ ] Test authorization, no endpoint, stopped/unverified service, stale revisions, sibling environment service rejection and hostname collisions before side effects. Run focused profile/artifact/ingress tests.

## Task 2 — Actual private gateway to running application

Files: create `ingress_runtime.py`, gateway template, `tests/test_ingress_runtime.py`, `tests/live_ingress_runtime.py`; modify `managed_rollout.py`, `service_provisioning.py`, `ownership.py`, `workspace.py`, relevant capacity/retention paths.

- [ ] Write failing tests for exact gateway/network identity, immutable application port, loopback-only publication, resource reservation and application peer checks with one authorized infrastructure member.
- [ ] Implement `prepare_gateway`/`verify_gateway`/`apply_gateway` with journaled intent, local pinned image verification, manifest CAS and bounded health probes. Reserve a configurable host loopback-port range; first inspect listeners/Docker port ownership as well as SQLite. Do not assume an unused DB row means a free host port.
- [ ] Persist gateway membership separately from application definitions. Extend network guards to admit only its verified full identity; never skip checks for arbitrary non-application members.
- [ ] Configure exact hostname routing and a default rejection response. Forward stable selected service DNS+trusted port; reject arbitrary upstream hostnames/IPs. Sanitize forwarded headers and set trusted host/proto at Apache/gateway boundaries.
- [ ] Update delivery preflight: endpoint-compatible image replacement preserves routing; removal/change of a routed endpoint fails before runtime mutation with a concrete route-update action. No implicit old-port fallback.
- [ ] Run two real local application containers on simultaneous environment networks, gateway reachability via two loopback ports, rejection of unknown Host, stopped app behavior, resource limits, unauthorized peer rejection and one-environment image replacement while the sibling serves unchanged content. Record actual HTTP bodies identifying project/environment/revision.

## Task 3 — Restricted Apache and certificate host adapter

Files: create `ingress_host.py`, `deployment/deploy-monitor-ingress-host.service`, host helper config/socket packaging, `tests/test_ingress_host.py`, `tests/live_ingress_apache.py`; extend deployment documentation and opt-in Compose socket mount.

- [ ] Write failing tests for path traversal, foreign vhost collisions, arbitrary command/upstream requests, invalid host identity, stale config digests and syntax failure preserving the last working config.
- [ ] Implement structured `inspect`, `stage_http`, `activate_http`, `request_certificate`, `activate_tls`, `verify`, `remove_owned_route` operations. Persist intent before each effect and return bounded sanitized evidence. Caller cannot supply file paths, Apache snippets or shell strings.
- [ ] Inventory actual Apache include roots, enabled sites, listeners and executable identity during host setup; store the validated configuration explicitly. Setup is a packaged administrative action applied during the authorized production rollout after host identity and existing configuration are verified.
- [ ] Generate isolated owned vhosts with host allowlisting, proxy timeout/limits, HTTP challenge route and HTTPS redirect only after TLS is verified. Perform complete configtest, then graceful reload and reread/probe effective behavior. If reload outcome is uncertain, inspect before retrying.
- [ ] Use the existing certificate infrastructure through a bounded adapter: fixed certbot/ACME binary and webroot, exact normalized hostname, no arbitrary hooks/plugins. Do not overwrite another certificate. Persist issuance intent and inspect existing certificate/key identity, SAN/expiry and lineage on restart. Renewal runs through the same verification/reload flow and updates readiness evidence.
- [ ] Local acceptance runs a real Apache edge container and real gateway/app containers. Use a local ACME server/test CA for deterministic issuance and renewal; verify TLS with its trusted root and SNI, plus an unrelated sentinel vhost unchanged through successful reload and rejected invalid candidates. No public CA or live host changes in this task.

## Task 4 — Cloudflare DNS ownership and resumable orchestration

Files: create `cloudflare_dns.py`, `ingress_worker.py`, `tests/test_cloudflare_dns.py`, `tests/test_ingress_recovery.py`; modify `integrations.py`, `worker.py`, `application_operations.py`.

- [ ] Write provider-contract tests for preview, exact-record read, create/update, explicit adoption, duplicate/conflicting records, insufficient permission, integration replacement, timeout after successful write, and ambiguous reread. Use fixed API hosts, bounded response size and no credential redirects.
- [ ] Preview current zone/record state. A preexisting record is never automatically owned; require its record ID and preview digest in explicit adoption. Record ownership and provider response identity atomically after reread. Use record IDs for updates/deletion; deletion is a separate requested operation and applies only to owned records.
- [ ] Implement `ingress_worker.process_once(db)` under the existing host-control serialization without nested lock acquisition. Phases: preflight, gateway intent/verified, origin HTTP intent/verified, DNS intent/verified, certificate intent/verified, origin TLS intent/verified, external verification. Each stores current resource identities, timestamps and retryability.
- [ ] Before every mutation recheck principal grant/revocation, service/environment/integration revisions, route/port ownership and stopped state. Credential replacement never silently changes the captured zone/account; require revalidation.
- [ ] Persist separate DNS/origin/TLS statuses. External Ready requires HTTPS success for the intended hostname and a route-specific challenge reaching the selected gateway plus current backend health/identity verification. Do not treat a Cloudflare edge certificate or an arbitrary 200 response as application identity proof.
- [ ] Handle provider timeout by exact reread; ambiguous ownership remains reconciliation-required. Recover from a crash after DNS creation, Apache reload, certificate issuance or gateway creation without repeating uncertain writes. Do not delete working DNS merely because later TLS setup failed.
- [ ] On stop, observation drift, certificate expiry or lost backend health, downgrade current readiness while retaining configuration/history. Add bounded periodic revalidation and renewal observability; resume never starts a stopped app.
- [ ] Test all crash points and restart from a second DB connection, including lease expiration, stale targets, revocation and retry identity. Unrelated project/domain state remains unchanged.

## Task 5 — Dashboard and shared API/MCP operations

Files: create `frontend/src/Domains.jsx`, `frontend/src/domains.js`, frontend tests; modify `frontend/src/main.jsx`, `frontend/src/model.js`, `server.py`, `application_operations.py`, `mcp_server.py`, scoped credentials metadata.

- [ ] Add shared `domain:list/get/preview/create/adopt/resume` operations and project/environment-scoped permissions. All adapters reuse the same validation/idempotency; MCP never supplies raw proxy config or host commands. Return sanitized phase/resource references only.
- [ ] Add environment Domains navigation with list, Configure domain dialog, preview differences, explicit existing-DNS adoption, operation progress, separate DNS/Origin/TLS status and contextual failure recovery. Filter service choices to current authorized managed HTTP endpoints.
- [ ] Preserve request IDs and payloads after uncertain responses; poll the original operation. Refresh and SSE updates must not reset drafts, focus, selected environment or pending adoption preview.
- [ ] Show Open application only with current verified HTTPS readiness. A stopped service displays unavailable while preserving domain configuration, with an explicit link to its existing Start workflow rather than starting it from domain setup.
- [ ] Exercise authenticated browser desktop/mobile setup, reload, permission denial, API failure, stale preview, retry and navigation across both environments. Verify dashboard-created route through MCP and MCP-created route in the dashboard.

## Task 6 — End-to-end acceptance and production handoff

Files: create `tests/live_ingress_http.py` and `docs/operations/environment-ingress-acceptance-YYYYMMDD.md`; update platform plan only with verified evidence.

- [ ] Run authenticated HTTP/MCP -> application operations -> worker -> real Docker gateway/application -> real Apache TLS edge flow using isolated local fixtures, fake Cloudflare HTTP service and local ACME. Fake provider must retain state and simulate an actual accepted write followed by a lost response; do not stub the orchestration methods.
- [ ] Demonstrate simultaneous Development/Production hostnames, real certificate validation, DNS adoption/collision, immutable version replacement, old-image health restoration, sibling stability, stop/no-wake, response loss, process restart before metadata commit and certificate renewal.
- [ ] Record selected service identity, image digest, environment, gateway/port identity, DNS record ID, certificate fingerprint/SAN, verified HTTP outcome and persisted phase statuses after reload. Inspect browser screenshots and compare a sentinel adopted site before/after.
- [ ] Run affected existing runtime, delivery, reconciliation, resource, integration, MCP and frontend tests. No existing no-PortBindings application invariant is removed merely to make gateway tests pass.
- [ ] Produce opt-in host-helper/Apache deployment packaging and rollback instructions with concrete owned files/resources. Read-only production inventory must validate compatibility before the production rollout. Apply DNS writes, gateway creation, host helper installation, Apache reload and certificate issuance as a distinct reviewed deployment step within the owner's existing authorization; preserve exact target ownership and verify each resulting effect.
- [ ] Report local readiness separately from live Cloudflare/public ACME/client acceptance and production activation. Do not mark whole-platform completion: database inspector, database service provisioning, secrets/persistence onboarding, scheduler/queue instrumentation and broader migration adapters remain separate open work.

## Implementation ledger — 2026-10-11

Ruling: use the existing main checkout and explicitly authorized multiagents, overriding generic skill worktree/inline-only defaults. Endpoint metadata, UI and independent architecture review have non-overlapping ownership.

Task 1 in progress: versioned HTTP metadata and per-environment gateway port intent. `ingress_ports.py` reserves only local intent, not an active endpoint or OS lease. Eleven tests cover concurrent database connections, restart/replay, cross-environment/operation isolation, real occupied loopback socket, Docker-owned port exclusion, exhaustion and bound-identity release refusal. Creation intent is journaled before mutation; even without a recorded container ID, an interrupted create cannot release its port. It is not yet exposed through route APIs or worker runtime.

Review rulings carried into Tasks 2–4:
- Gateway must resolve replaced application addresses dynamically or explicitly reload on verified target transition; force an IP change in real acceptance. Verify unique network alias ownership, not just a matching service name.
- Route definition/artifact pins must advance atomically with successful managed rollout or verified rollback. Immutable definition IDs change across releases; a route cannot retain its old definition while claiming current readiness.
- Reserve the generated ingress service name and reject existing conflicting applications before side effects. Never admit infrastructure by a name prefix.
- Admit exact gateway identity in every provisioning, delivery and recovery network guard. Preserve application no-PortBindings/internal-network rules.
- One gateway per environment shares a single capacity reservation across its routes. Account it in the existing resource policy/managed-service transactions, not a disconnected table; enforce its real runtime caps.
- Host helper needs root-owned policy/state, peer-credential authorization, independent route/hostname/port ownership, and a helper-wide lock across configtest/reload/restore/renewal. Worker-provided digests alone are not authorization. Reject unsafe filesystem ownership/links and detect existing wildcard/alias/vhost collisions.
