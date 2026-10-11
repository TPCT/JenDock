# MySQL-family migration catalog acceptance — 2026-10-11

This is real-engine compiler/catalog acceptance, not a completed migration
pipeline. `migration_mysql_catalog.py` remains unintegrated with the public
migration bindings, helper protocol and deployment coordinator.

## Actual local execution

`tests/live_migration_mysql_catalog.py` ran against existing immutable local
MySQL 8.4.11 and MariaDB 11.4.13 images. Each engine passed eight checks. The
machine-readable results include image IDs, source hashes and exact cleanup:

- `migration-catalog-mysql-20261011.json`
- `migration-catalog-mariadb-20261011.json`

The fixture authenticated a real migration account, created a table containing
all current compiler-supported scalar types, appended a nullable column, and
created a nonunique index. Each operation compared its baseline and resulting
metadata through `verify_transition`. Actual inserted records proved identity,
boolean, temporal and nullable behavior. A deliberately altered boolean column
was rejected. A SELECT-only account could not claim hidden triggers were absent;
the authorized account detected and rejected the fixture trigger.

All resources belonged to the `deploy-monitor` Compose project with unique
fixture labels. Tests used an internal network, no published ports and no image
pulls. Frozen source copies ran in an existing immutable helper dependency image;
this is not a canonical release-image build. Exact fixture containers, network
and volume were removed, and source hashes were checked against the checkout.
No production data, provider operation or server deployment was involved.

The complete backend suite passed after the final source changes: 1,371 tests,
zero failures (`.runtime/mcp-venv/bin/python -m unittest discover -s tests -q`).
Independent review of the final catalog and transition checks reported no further
actionable findings. This does not substitute for pending product integration.

## Review corrections and boundaries

Independent review found and verified corrections for case-folded table-name
lookup and trigger metadata visibility. Binary catalog lookup currently requires
`lower_case_table_names=0`. Trigger visibility needs a proven direct account
grant: unrestricted global, exact database, or exact table. Role-only and wildcard
database-grant proofs are not implemented. MySQL partial revokes disable the
global-grant shortcut. Missing visibility is an error, not an empty result.

Identity pins the authenticated account, engine/version, strict session mode,
timestamp defaults, InnoDB default, character sets/collations and enabled foreign
key checks. Incoming-reference coverage is explicitly `visible_only`: privilege-
filtered metadata cannot establish global absence of incoming foreign keys.
Observed foreign keys, triggers, views and unsupported constraints remain outside
this initial catalog boundary; these restrictions do not establish full support
for ordinary application migration histories.

Transition checking preserves previously observed columns, indexes and
constraints. Metadata observations are not atomic and do not serialize competing
DDL. The executor still needs a session lock, durable per-step intent, exact
source/ledger proof, bounded execution and reconciliation after implicit commits.
It must not infer rollback, issue compensating DROP, or blindly replay ambiguous
DDL. No MySQL migration capability was advertised in the UI or bindings here.

## Remaining delivery

The operation journal, inspect/apply/reconcile integration, recovery after partial
DDL, broader migration grammar/history support and actual repository-triggered
deployment acceptance remain outstanding. Production destructive operations
remain prohibited. See `completion-audit-20261011.md` for the full product scope.
