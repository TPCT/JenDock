# Project creation and environment ownership acceptance — 2026-10-11

## Implemented

Authenticated project creation verifies canonical GitHub repository identity using the encrypted saved connection. A shared typed operation boundary authorizes the caller before provider access and atomically records the project, two inactive environments, operation result and audit event. Stable request IDs replay the persisted result without duplicate creation, including concurrent requests and retries after the provider connection is disabled. Automatic deployment preference remains enabled; creation does not provision or wake containers.

The React form retains its original request across uncertain outcomes and browser reloads using an allowlisted, username-scoped session cache. No credential or provider error is cached. Missing GitHub connection offers a contextual path to Integrations; drafts survive that navigation. Explicit sign-out and expired sessions clear creation recovery state.

Container control resolves the exact registered Development or Production recipe from observed Compose group, service and ordered configuration files. Conflicting, malformed, foreign or ambiguous claims fail closed; verified independent environments do not need fabricated legacy bindings.

## Verification

- Final combined backend suite: 289 tests passed.
- Frontend: 32 tests passed; production Vite build passed. Existing large-chunk warning remains (main bundle approximately 692 kB minified).
- Local Docker receiver and worker images built; actual isolated image executions imported their entrypoints and new modules successfully.
- Real GitHub metadata read and local authenticated HTTP creation passed via `tests/live_project_creation.py`: CSRF rejection, trusted actor attribution, replay, changed-payload conflict, persisted operation status, exactly two inactive environments and zero deployment jobs. Temporary encrypted credentials/key/database were removed. No upstream write occurred.
- Real Docker ownership acceptance via `tests/live_ownership.py`: two independent temporary services in the existing deploy-monitor Compose project were inspected, started and stopped against immutable container IDs. Both environments exposed management; legacy_binding stayed null; fixtures were removed and peers were unchanged.
- Browser: missing-connection error, contextual Integrations navigation, reload and reopened form preserved all three entered fields. Screenshot: `/tmp/monitor-creation-recovery-20261011.png`.
- Uncertain-response reload is covered by frontend state tests and API replay acceptance, but full network-interruption browser acceptance is still pending.

## Remaining scope

This checkpoint does not deliver service provisioning, MCP transport/authentication, Cloudflare domain provisioning, the complete database/file/scheduler tooling, or final production migration execution. Full project/environment onboarding remains incomplete until service provisioning and its recovery workflow are implemented and exercised. No production adoption, push or deployment was performed for this checkpoint.
