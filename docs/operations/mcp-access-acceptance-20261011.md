# MCP access checkpoint — 2026-10-11

Implemented in the existing main checkout; local Docker only. This does not close the full platform goal or establish production acceptance.

## Implemented contract

The official pinned MCP SDK serves a sessionless Streamable HTTP endpoint at `/_deploy/mcp`. Eight tools share the console application boundary: project list/get/create, service list/get/templates/create, and operation status. Creation uses stable idempotency keys and returns queued state without claiming successful provisioning. Template discovery returns immutable artifact identity and required revision fields, without commands, private bindings or environment values.

Console sessions manage bounded credentials under Integrations. Tokens are shown once; only digests are stored. Grants name explicit projects, repository IDs, environments and permissions, with expiry and revocation. Management and issuance are audited without tokens. Runtime authentication and worker execution recheck revocation and the configured current console owner. `CREDENTIALS_FILE` must be explicit for MCP startup; changing or removing its username invalidates that owner's token access. Production receiver/worker Compose configuration now supplies that path; this source change is not a production deployment.

Artifact source-environment authorization covers creation, replay, recovery and worker execution. Same-owner console recovery may replace a revoked credential's execution authority only after current authorization, retaining an audit event. Host and Origin allowlists are exact; forwarded headers are rejected. Authentication, discovery and tool execution use a bounded eight-thread limiter so blocking provider work does not block the event loop.

## Evidence

- 456 backend tests passed, including SDK transport, revocation, owner removal, scope boundaries, source-environment replay and worker/recovery fences.
- 58 frontend tests passed; Vite production build passed. Existing bundle-size and SQLite ResourceWarnings remain.
- Receiver, worker and MCP Docker targets built successfully. Local MCP runs read-only with dropped capabilities, no Docker socket and a loopback-only port.
- The worker image import smoke check caught missing `cryptography`; the dependency was added, the image rebuilt and worker/credential/application imports then passed.
- Actual official SDK client against the Docker endpoint verified scoped project reads, hidden Production environment, foreign-project rejection and HTTP 401 after revocation. The temporary credential was revoked in a finally block. Report: `.runtime/mcp-transport-last.json`.
- In-process SDK tests exercise all eight tools, project/service persistence and idempotency. A deliberately blocked provider verification does not prevent a concurrent authenticated read. Provider/build evidence in those tests is controlled fixture data.
- Browser reviewed the real Integrations action and persisted revoked credential metadata, searchable project selection, explicit environment controls and modal layout. Mobile 390x844: dialog client/scroll widths both 358px, footer visible. Screenshots: `/tmp/monitor-mcp-form-desktop-20261011.png` and `/tmp/monitor-mcp-form-mobile-20261011.png`. Form was cancelled; no persistent access was created through browser testing.

## Still open

The local visual preview has no worker and uses captured production observations. MCP creation is disabled in that preview until a worker-backed acceptance environment is connected; implementation and controlled SDK creation tests remain available. Actual MCP create → worker provision → dashboard visibility, and the browser one-time-token/revoke flow, are not yet full-flow accepted.

Production MCP Compose/proxy/TLS integration and intended remote-client acceptance remain open. Bearer credentials are supported; OAuth is not implemented. Proxy deployment must strip forwarded headers, retain the exact public Host and use configured Origin values. The per-owner limiter is process-local, so multi-process deployment requires a shared limiter.

Operation status currently exposes bounded public identity/state/revision, but lacks sanitized stage/event pagination and recovery guidance. Database/file/scheduler tools, domains, migration safety, complete legacy environment adoption and whole-product production acceptance remain separate unfinished requirements.
