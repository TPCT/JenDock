# Adoption recovery browser acceptance — 2026-10-11

Boundary: actual React frontend, HTTP authentication, Unix control transport and
SQLite persistence. Docker inventory is synthetic and each environment's first
successful save response is deliberately replaced by HTTP 503. This is not a
Docker, production or dropped-TCP-connection acceptance result.

Harness: `tests/live_adoption_recovery_console.py`, existing project Python runtime,
localhost port 19203, ephemeral registry and synthetic fixture-owner credentials.
The harness never connects to external services. Frontend exercised through the
Codex browser, not direct API calls or scripted frontend state insertion.

Verified after fixes:

1. Selected only production-db for Production and accepted the review.
2. Backend saved; UI received injected 503 and locked selection to original request.
3. Full page reload retained Check linking request despite already-linked state.
4. Without resolving Production, selected development-db and accepted Development.
   Selecting the default overlapping pair was correctly rejected until narrowed.
5. Development also received 503, followed by full reload. Both requests remained
   independently recoverable while switching environment tabs.
6. Recovered Production, then Development. Both displayed success; database held
   exactly two distinct accepted fingerprints, each for its intended service.
7. Request journal proved each save repeated exactly once with identical payload,
   and no replacement adoption-preview occurred between acceptance and recovery.
8. Final Development reload showed persisted Active and no recovery action.

The attached JSON records sanitized fixture requests and database receipt proofs.
Screenshots in this session: `/tmp/adoption-production-recovered.png` and
`/tmp/adoption-development-recovered.png`. The dashboard intentionally showed stale
runtime observations because no Docker collector was attached; Active here is the
persisted registry state, not proof of healthy running containers.

Defects discovered and fixed:

- Uncertain requests were marked unsaved despite a durable checkpoint, triggering
  beforeunload on reload. Only editable unsaved selections now mark the form dirty;
  in-flight operations retain their pending protection.
- Recovery-only section incorrectly said Environment unassigned, and another
  environment's draft could label a fresh link action as request recovery.
- Concurrent observation writes caused SQLite snapshot upgrade failure when build
  profile DDL ran inside workspace reads. Initialize before the read transaction,
  skip repeated DDL on an existing table. The deterministic two-connection test
  reproduced the failure and passed after the fix.

Remaining untested recovery branches include browser storage denial/corruption,
logout with another authenticated owner, and real transport disconnection. Helper
and backend checks are separate evidence for scope validation and storage errors.

Final checks: 1,336 backend tests and 123 frontend tests passed; frontend build
succeeded. Independent review passed 18 workspace tests and 4 adoption helper
tests with no blocking finding. Temporary server stopped, browser tab closed,
and temporary registry/credentials removed after preserving sanitized evidence.
