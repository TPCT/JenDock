# Dynamic delivery platform completion plan

> Execute in the existing checkout with explicitly authorized parallel agents. No new worktrees or feature branches. Review each independent slice and integrate sequentially.

**Goal:** Complete the approved monitoring and CI/CD product, with two independent environments, dynamic configuration, usable container tools and verified delivery.
**Architecture:** SQLite owns projects, environment bindings and versioned recipes. The authenticated receiver delegates restricted operations to the worker. React consumes real persisted state; Docker identity and database identity remain explicit.
**Spec:** docs/specs/dynamic-environment-platform.md; later owner corrections supersede the spec.
**Stack:** Python, SQLite, Docker Compose, React, Vite, GitHub Octicons.

## Constraints
- Development and Production can run together; no automatic provisioning/waking of inactive environments by default.
- Preserve existing live deployments and data during adoption. Existing branches and histories are not discarded.
- RAM and CPU limits only; disk use is monitoring, no quotas.
- No CI/CD approvals. Production destructive database deployment operations are prohibited; explicitly configured development resets must never reach production databases.
- No secrets in source, logs or browser state; don't replace working SSH auth silently.
- Existing migration draft is not production protection until actual capability/identity boundaries are verified.

## Review focus
- Shared Compose groups and YAML aliases must not mutate sibling services.
- Concurrent saves/runs must pin revisions and reject stale target identities.
- Lost responses must not duplicate deployments or external writes.
- Realtime updates and tab navigation preserve form drafts and focus.
- Uncertain migration/domain recovery remains visible and does not masquerade as successful rollback.

## Work sequence and evidence
- [ ] Resource policies: resource_policy.py, control.py, server.py, Resources.jsx. Verify invalid input, aggregate budgets, immutable identity, rollback, swap enforcement, actual Docker limits, reload and mobile flow.
- [ ] Registry: registry.py, store.py, server.py plus tests/test_registry.py. One-time legacy import, persisted projects and exactly two environments, revision guards. Subsequent JSON changes cannot override the database. No side effect on live containers.
- [ ] Environment routing: store.enqueue, server.webhook/actions, worker selection, React environment navigation. Persist environment and immutable recipe revision on every new run, webhook idempotency and inactive environment policy, distinct service ownership.
- [ ] Pipeline stages/migrations: versioned recipes, Laravel and Alembic adapters, verified DB identities/capabilities, pending file evidence, isolated validation, per-environment uncertain recovery, visible retryable cleanup.
- [ ] Integrations: encrypted credential store with external key, GitHub and Cloudflare replace/test/disconnect UI and APIs. Failure preserves previous credential. Cloudflare account and user tokens supported. No secret appears in SSE/audit.
- [ ] Domains: current-state preview, explicit adoption, idempotent Cloudflare writes and reread after ambiguous outcomes, separate routing/TLS readiness and resumable steps.
- [ ] Container tools: explicit service bindings, terminal/logs/events, bounded file browsing with secret/path protections, paginated database inspector, scheduler/queue instrumentation.
- [ ] Whole product acceptance: actual authenticated flows, persistence after reload, permissions, failure/recovery, desktop/mobile screenshots, no misleading missing-capability placeholders.
- [ ] Production adoption: verify server identity and live inventory, commit reviewed source, build images, activate only with no conflicting work, verify running revisions/API/browser, targeted unused cache/image cleanup.
- [ ] Completion audit: each requirement has current evidence; gaps remain open rather than counting passing builds as acceptance.

## Current review baseline
2026-10-10: backend audit confirms registry/integrations and environment-aware jobs are missing. Current resource allocations alone do not implement independent environments or project-wide cgroup limits. Existing filename-based migration detection is not a verified database safety boundary. These are implementation gaps, not documentation exceptions.

## Added owner scope: creation through dashboard and MCP
- [ ] Shared project/service application operations: strict validated definitions, explicit environment/role, repository or image identity, ports/domains, secret references, data persistence and RAM/CPU budgets; idempotent operation progress/recovery.
- [ ] Dashboard project creation: current metadata dialog is a first slice; complete repository verification and environment onboarding, then service creation/provisioning flow.
- [ ] Authenticated MCP adapter: project list/get/create, service list/get/create and operation status; revocable scoped credentials, reuse application permissions/audit/revisions/idempotency. No raw host-shell tool or secret-bearing results.
- [ ] Cross-surface acceptance: MCP-created project/service visible and operable in dashboard; dashboard-created entities discoverable by MCP; invalid/unauthorized/duplicate/conflicting requests rejected without orphan services.

## Active service-creation implementation — 2026-10-11

Previous goal turn made verified progress: commit `33632c7` added real repository verification, durable project creation/reload recovery and independent runtime ownership. Full objective remains open.

Current coordinated slice (same checkout, no production mutations):
- Backend agent: immutable trusted build artifacts, service definitions, scoped creation authorization, idempotent queued operations and transactional RAM/CPU reservations.
- Provisioning agent: journaled isolated environment Compose provisioning with exact Docker identity, stopped/running outcomes and recovery evidence.
- Frontend agent: selected-environment service dialog, trusted artifact selection, captured request identity and actual operation status.
- Root: HTTP/worker integration, artifact publication and retention, shared capacity enforcement and end-to-end acceptance.

Product decision: a Python HTTP fixture is test data only. The product must offer real project build artifacts, not substitute a dummy application for service provisioning. User-supplied arbitrary images/commands are not the trusted build-source contract. A missing verified artifact remains visible until the real build publisher supplies one. Mixed adopted/managed environments require a verified ownership transition; no legacy manifest is modified by inference.

## Managed delivery integration — 2026-10-11

Source onboarding checkpoint dd0cf02 is committed and verified locally. Active slice adds an immutable managed-build selection with exact service definitions/bindings/resources, successful-build evidence separate from final deployment, worker runtime verification and bounded old-image restoration. Source-only validation remains isolated. No implicit provisioning or waking stopped peers; both environments may run concurrently.

Review findings already addressed: nested non-reentrant worker lock, managed dispatch falling into legacy migration path, source artifact validity after rollout failure, image catalog masking across environments, Compose create unsupported flag, and binding CAS requiring replacement image/digest. Actual Docker replacement/health rollback/direct resource caps/peer isolation passed. Full build-to-deploy worker fixture and integration review are ongoing. Interrupted ambiguous mutations require explicit reconciliation; automatic journal reconciliation and its UI remain open. No production rollout or completion claim.

## Interrupted managed delivery — 2026-10-11

Checkpoint 8de260a implements local managed build-to-deploy. Read-only runtime reconciliation and durable recovery leases now verified via an actual Docker crash-before-CAS fixture, including multi-pass peer convergence without replaying create/start. Per-container outcomes and reconciliation-aware rerun state reviewed on desktop/mobile. Ambiguous records lacking exact identity remain unresolved by design; broader platform completion and production activation are still open.

## Artifact history and shared spacing — 2026-10-11

Added scoped immutable artifact history with stable pagination and explicit integrity failures; recorded service associations are distinguished from live runtime proof. Real local HTTP/SQLite browser pagination and environment separation passed on synthetic test records. Owner's global padding/margin correction is implemented through shared tokens, Empty, forms, dialogs, page gutters, lists and run styles; representative desktop/mobile rendered evidence recorded in `docs/operations/versions-spacing-acceptance-20261011.md`. Whole-product and production acceptance remain open.

## Authenticated MCP checkpoint — 2026-10-11

Official SDK adapter and dashboard credential management implemented; scoped creation reuses durable application operations. Independent reviews fixed forbidden source-artifact replay, blocking async transport and stale console-owner authority. Local Docker SDK read/revoke acceptance and 456 backend/58 frontend tests pass. `docs/operations/mcp-access-acceptance-20261011.md` records exact evidence and remaining work. Next required slice: real worker-backed cross-surface creation, sanitized operation progress/recovery, then deployment packaging/TLS/client acceptance. Do not count preview queue acceptance as container provisioning or mark the full MCP/whole-product items complete.

Follow-up: actual local SDK HTTP → worker → Docker → authenticated console HTTP creation passed for both isolated environments, including health, resource limits, replay and persistence. Sanitized operation progress and opt-in MCP Compose/Apache packaging implemented. Independent review and 459 backend tests pass. See `docs/operations/mcp-creation-acceptance-20261011.md`. Production identity was reverified read-only and live version remains 0ac2162; deployment, actual TLS/client acceptance and broader unfinished product scope remain open.
