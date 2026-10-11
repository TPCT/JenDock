# Database product review — 2026-10-11

This is local acceptance, not deployment or whole-platform completion.

## Browser workflow against actual PostgreSQL

`tests/live_database_console.py --browser-ready ...` served the built React app,
the actual authenticated HTTP application, Unix worker, Docker observations and
PostgreSQL helper. The Development and Production fixture databases were separate
disposable services in the local deploy-monitor Compose project. No production
database or existing user's credential was used.

Browser acceptance performed:

- Created a dedicated inspection credential and an explicit binding/row policy
  in each environment using the actual forms. Password fields cleared after save.
- Verified container/storage/account identity, accepted the bounded observation,
  selected public.sentinel and read its columns, primary key, index and rows.
- Development rows contained the development sentinel; Production rows contained
  the production sentinel. The Production configuration started empty rather than
  inheriting Development credentials or bindings.
- Reloaded the entire page and selected the persisted Development binding.
  Historical acceptance remained visible while fresh verification was required.
- Revoked the disposable Development credential through the UI. The binding became
  credential-stale, prior data disappeared, and verification was disabled.
- Checked desktop and 390px mobile rendering, modal scrolling, fixed header/footer
  and no document/dialog horizontal overflow (390px document, 358px dialog).

The browser pass found and fixed two implementation defects: external select labels
were not connected to MUI's non-native combobox, and configuration reload published
its parent catalog refresh before its own ownership request finished, causing worker
lock contention. External labels now identify the select/listbox; ownership loads
before the parent refresh. Reopening the existing configuration succeeded afterward.

Screenshot artifacts are local temporary files:

- `/tmp/database-inspector-development-desktop.png`
- `/tmp/database-inspector-development-revoked-mobile.png`
- `/tmp/database-inspector-production-mobile.png`

Live fixture session 76509 exited 0 and removed its exact containers, volumes,
networks and private readiness document. Its automatic continuation additionally
verified page sizes 2/1, response scope, persisted acceptance, revocation and replay
in both environments. Existing peer IDs/images and running peers were preserved;
pre-existing Orbit restart loops continued and are not claimed fixed here.

## Recovery and protocol review

Independent review found stale-binding reload and ambiguous-save recovery gaps.
The configuration view now reloads current binding metadata without silently
rebasing its unsaved draft. Explicit discard loads the current revision.
Uncertain saves retain the original scoped payload/request ID. A definitive
`not_committed` outcome is emitted only for a durable rejection receipt after
transaction commit and fresh authorization. Same-ID concurrent requests cannot
turn that rejection into a later mutation. Prior success conflicts and postcommit
authorization failures remain ambiguous. Actual HTTP/Unix and two-connection
SQLite regressions cover those boundaries.

MCP database reads use explicit metadata/business-row scopes, current owner and
credential revalidation, environment grants and an independently configured Unix
peer UID. New scopes do not expand existing tokens. The official SDK now also exercises console token issuance, real Unix dispatch,
Docker helper and actual PostgreSQL in both environments. Schemas, tables and
2/1 row pages, foreign-environment rejection and revoked-token rejection passed.
The MCP HTTP protocol uses ASGITransport with application lifespan, not a separate
TCP MCP listener. Final live run 94326 exited zero with exact fixture cleanup.

## Remaining boundary

MySQL 8.4.11 and MariaDB 11.4.13 passed actual direct-adapter acceptance in both
fixture environments, including privilege checks and concurrent DDL. See
`database-mysql-adapter-acceptance-20261011.md`. The private helper and authenticated HTTP/Unix API now support all three engines.
MySQL/MariaDB live run 63825 passed both environments, wrong-dialect rejection,
metadata, rows, persisted acceptance, revocation and exact cleanup. The React
configuration requires an explicit MySQL versus MariaDB choice for an adopted
generic mysql candidate and retains the chosen dialect across draft reloads.
A browser hold was released without executing the full MariaDB browser flow;
that does not establish engine-specific browser acceptance.
Managed database provisioning, broader row types, migration execution, production
rollout and all-screen operational acceptance remain open in the platform audit.

## Build verification

The backend suite passed 1035 tests; the final frontend suite passed 84 tests.
The frontend production build passed with its existing bundle-size warning.
Receiver, worker and MCP Docker builds succeeded from the current local source:

- receiver: `64d3340ad450`, `deploy-monitor-receiver:database-product-review`
- worker: `278f0c72bf32`, `deploy-monitor-worker:database-product-review`
- MCP: `c079a2c790a5`, `deploy-monitor-mcp:database-product-review`

Each image passed a network-disabled, read-only-filesystem import smoke check;
the receiver also contains the built UI. These checks prove packaging/imports,
not production service health. No production deployment was performed.

MCP database reads are deliberately opt-in: configure `MCP_DATABASE_SOCKET` in
the MCP service and `DATABASE_MCP_UID` to the actual MCP UID in the worker.
Existing Compose packaging shares `/state/control.sock` through the state volume;
the MCP image inherits `database_rpc.py` from the receiver. Neither opt-in nor
existing credentials is silently expanded by this implementation.


## Three-engine SDK follow-through

The new MySQL/MariaDB console fixture also passed official MCP SDK reads against
all four engine/environment combinations (session 19031, exit 0). Console-issued
scope-restricted credentials traversed the real MCP ASGI app, Unix worker,
Docker helper and actual engine. Schemas, tables, structure, 2/1 row pages,
foreign-environment denial and revoked-bearer HTTP 401 passed. Cleanup errors were
empty and all original peer IDs/images/running membership were preserved.
Pre-existing Orbit restart counters advanced, as recorded in the fixture log.

Evidence: `/tmp/database-mysql-mcp-console.log`. This is actual protocol/engine
acceptance, not a separately listening MCP service or engine-specific browser
acceptance. The helper image is
`sha256:5b339f180f0662f82a45b053e2c2211edce3cb8202ff87ced08b8e6ff2c87886`.


## Current packaging follow-through

Full backend suite in the project's MCP-capable virtualenv: 1066 tests passed.
The system Python invocation lacked MCP dependencies; it is not the accepted
suite environment. The frontend suite passed 86 tests and its production build
passed. Receiver, worker and MCP targets built with current database integration
and the shared spacing changes. Network-disabled, read-only image import smoke
checks passed for all three targets.

- Receiver: `sha256:1fa178ef2449313d13c7e41d34f652ffdd911de96074b739ce0800e7a7cdd26f`
- Worker: `sha256:4ca667a6f34223ff9c344edc3b30e7b99e5b7abbbd55940d283afb184eca2375`
- MCP: `sha256:c079a2c790a5f1b178a930a92b7e4896da6bc545397e439117182367a909e0b2`

The migration parser/executor work is separate and is not enabled in these
images. Production SSH identity was freshly confirmed as root/UID0 with eth0
176.118.198.49. Live receiver and worker remain healthy on `0ac2162`; no production
mutation or deployment occurred during this acceptance pass.
