# Database inspector foundation — 2026-10-11

Implemented private infrastructure, not an exposed inspector feature.

`database_bindings.py` stores immutable definition digests and revisioned,
environment-scoped configuration. Inspection secrets use an external private key;
the authenticated ciphertext contains purpose, ID, project, environment and
revision. Rotation/revocation invalidates exact secret references. Public metadata
and audit records exclude username/password/ciphertext. Saving a reference always
remains unverified: no caller-provided ownership or engine name grants access.

`database_helper.py` provides bounded subprocess stdin/stdout/stderr and a trusted
Docker helper launcher. It pins the helper image and target container, sends
credentials only through stdin, requests no retained Docker logs, and checks actual
network/security/execution configuration before sending input. It requires an
exited successful non-OOM helper and revalidates caller authority/target before
releasing output. It rejects duplicate JSON keys and non-finite constants.

A private durable intent is written before create. Cleanup verifies exact
name/nonce/image/ID, removes only that helper, and checks absence before clearing
intent. Unknown creation/removal outcomes retain the intent and block new launches.
`reconcile_pending` only removes an observable exact owned helper. A missing helper
after ambiguous create remains unresolved, because a delayed daemon operation is
not proven absent by one lookup. These records never contain credentials/rows.

## Evidence and remaining work

Twenty-one focused tests cover real SQLite persistence/reconnect and encryption,
CAS conflicts, scope and transaction-time revocation, immutable definitions,
ciphertext transplant/wrong-key denial, sanitized audits, actual subprocess byte
limits and timeout/reaping paths, plus mocked Docker namespace/configuration,
private input, exact cleanup, malformed output and retained cleanup recovery.

No real PostgreSQL adapter, helper image, HTTP/MCP database actions or Database UI
are delivered by these modules. They are intentionally not wired into public scope
catalogs or deployment images yet. The selected PostgreSQL adapter agent exhausted
its usage allocation before producing a module; its proposed contract was reviewed
but is not implementation evidence.

Next required implementation is the fixed-query PostgreSQL driver and trusted helper
entrypoint, followed by worker-side live session/principal resolution, actual
registered database ownership verification, bounded HTTP reads and the frontend.
Driver identity/privilege checks, real Docker inspected configuration, two-database
sentinel isolation, engine timeouts, full end-to-end credentials/rows protection,
and cleanup under actual daemon failures still require live fixture evidence.
MySQL/MariaDB and the separate trusted migration executor remain in the full scope.

The full backend suite passed 947 tests before the final two additional helper
regressions; all 21 focused database tests passed afterward. No claim of real
PostgreSQL acceptance follows from these tests.

No production service, data, grants or deployment configuration changed here.
