# Managed runtime and PostgreSQL application acceptance

## Actual Docker result

`tests/live_application_database.py` completed with exit 0. The log at
`/tmp/live-application-database.log` records all assertions and cleanup. This used
local Docker, real PostgreSQL, a separately created limited application role,
actual managed provisioning, immutable application credentials/attachment and
runtime settings operations. It did not modify production.

Verified outcomes:

- PostgreSQL was adopted and its ownership retained after a managed environment
  recipe replaced the application's legacy recipe.
- The application used its dedicated database bridge, retaining its own environment
  network and never joining the database's broad legacy network.
- A real HTTP handler wrote and read the application sentinel; an independent SQL
  query confirmed persistence.
- Environment settings persisted through same-image recreation and a distinct-image
  delivery. Deliberately uncommitted delivery evidence reconciled without a duplicate
  replacement.
- A failed-health candidate rolled back to the previous working application,
  preserving its settings, database access and stored sentinel.
- SQLite reconnect preserved the registered runtime configuration.
- The production peer remained running. Exact fixture cleanup restored the original
  peer identities and running states.

Two applications sharing one database are covered by a dedicated network regression,
not by this live run. Browser Apply, account rotation across actual rollback, endpoint
replacement, MySQL/MariaDB application connections and complete lifecycle acceptance
remain open. Direct backend operations in this fixture are not an end-to-end GitHub
webhook or browser test.

## HTTP, browser and resources

Sixteen actual HTTP/Unix transport tests verify encrypted credential and attachment
configuration, scoped reads, durable save replay, rotation/revocation isolation and
non-disclosure. A separate browser fixture exercised account create, binding save,
reload, mapping edits, tab draft retention, navigation guard, rotation/rebind/revoke,
and disable with actual HTTP/Unix/SQLite. Docker observations for that browser
fixture were synthetic; it did not establish runtime application.

The managed resource control regression uses real SQLite policy, definition,
binding and receipt persistence with scripted Docker replacement. Successful
RAM/CPU changes commit the new capacity reservation with the new definition and
binding. Failed candidate health preserves the exact previous resource policy
while registering the restored container identity. Generic YAML rewriting and
recreation paths are not used for managed settings/resource changes.

Independent review found and fixed acceptance atomicity, stale control receipts and
shared-database bridge coexistence. The known direct Start/Stop/Restart state
projection regression remains under implementation and has an explicit failing test.
No whole-system or production completion claim follows from this document.

## Actual browser-to-Docker application acceptance

The separate `tests/live_application_database_console.py` process completed with
exit 0 after the browser applied the connection through the actual authenticated
HTTP/Unix endpoint. The UI showed Applied to container, preserved that state after
reload, inspected the replacement container ID, and made DATABASE_URL read-only
with its managed-connection label. Desktop and 390x844 mobile were inspected;
evidence is under `/tmp/application-runtime-ui-evidence/applied-{desktop,mobile}.png`.

After browser completion, the fixture independently performed real application
HTTP write/read, queried PostgreSQL for the application sentinel and untouched
business sentinel, reconnected SQLite, verified the dedicated bridge and preserved
production peer, then completed exact cleanup with peer snapshot equality. No
Docker observations were fabricated in this run. This supersedes the earlier
browser-Apply gap for this PostgreSQL flow, not the separate lifecycle or broader
engine requirements. No initial whole-source hash snapshot was captured for this
browser run; it proves the exercised behavior, not an immutable final-build identity.


## Durable lifecycle review and stopped database proof

The direct-control state regression now uses a durable same-container lifecycle
journal and a separate desired-state projection. Exact request replay validates
actor, original container identity, revision and scope; conflicting reuse is rejected.
The journal now guards updates, deletes and duplicate-ID replacement of its capture.
Runtime environment requests retain encrypted original payload receipts.

Actual stopped-database acceptance completed in session 71026 with exit 0
(`/tmp/live-application-database-stop.log`). Real Docker endpoint IDs cleared after
DB stop, then revoked credential/disabled attachment still allowed stopping the
accepted app. New start was rejected and the app remained stopped. Nine runtime
source hashes were unchanged and exact cleanup preserved peers.

A subsequent extended run (1248) failed on first attachment application with
recovery-required, before reaching newly added normal lifecycle/resource checks.
The cleanup assertion also detected changing peer state. Fixture resources were
removed, but peer equality is not claimed for that failed run. Diagnostic rerun
is in progress with persisted nonsecret inventory differences and sanitized rollout
errors; neither the cause nor success is assumed from the earlier passing run.


## Extended final local runtime acceptance

Session 15362 exited 0 on the frozen runtime sources. Real stop/start/restart kept
the exact container ID and persisted state; restart changed StartedAt. SQL sentinel
reads succeeded after start/restart. Supported capacity admission plus runtime
replacement applied 80 MiB RAM and 0.3 CPU, verified against actual Docker Memory
and NanoCpus, policy and immutable definition. SQL access and environment values
survived. The stopped-DB/revoked-binding stop/start-rejection checks also passed.
Exact cleanup, strict pre-existing peer equality and eleven unchanged source hashes
were verified. Evidence: /tmp/live-application-database-lifecycle-diagnostic.log.
The earlier failed run remains unexplained, not relabeled as fixed.

Final complete backend suite: 1,210 tests passed in 46.482 seconds. Frontend: 106
tests passed and production build passed (existing bundle-size warning). Five
Docker targets built and network-disabled read-only import checks passed; every
packaged Python module and migration PHP parser was hashed against current source.
177 inventoried build source files were unchanged through final build verification.

- receiver: `sha256:2d9b1665b3206fa3c2120d16e7996b9b4d702124b39dd6746f441a5c22aceb82`; 43 source files verified.
- worker: `sha256:61428a45cb46e94a2b08097cab1f0a60c38f529bf2369cc38ab521865c2f3bd6`; 70 source files verified.
- mcp: `sha256:bc04f9853e4800c0ec83a6d9ebdb0f0634bf49a03be5001f2d968896c66932f8`; 44 source files verified.
- database: `sha256:5b339f180f0662f82a45b053e2c2211edce3cb8202ff87ced08b8e6ff2c87886`; 3 source files verified.
- migration: `sha256:181aa258b69f0be25f34a6c3a22ebac7d6dd76aab71d1c43df3994bfb02360b0`; 7 source files verified.

These are local images and runtime fixtures. No production release occurred.
