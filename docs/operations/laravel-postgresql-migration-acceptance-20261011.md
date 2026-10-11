# Laravel PostgreSQL migration foundation acceptance — 2026-10-11

The syntax-only Laravel planner and trusted PostgreSQL executor have passed the
local acceptance described below. **They are not wired into the worker, database
bindings, or deployment pipeline.** This is foundation acceptance, not automatic
production migration support or permission to run against an existing database.

## Contract and implementation

`migration_laravel.py` uses the pinned trusted PHP syntax parser without importing
or executing candidate PHP. The immutable Laravel plan includes source hashes,
actual ledger names/batches, pending operations, and mandatory column semantics.
Those semantics distinguish identity columns from plain integer primary keys and
retain source temporal types. They are included in the plan digest. Canonical
fully qualified Laravel names are checked; unsupported dynamic/raw/destructive
upgrade statements fail with filename/line errors. Destructive rollback bodies
alone do not make an additive upgrade unsupported.

`migration_laravel_postgres.Executor` requires the explicit grammar identity
`laravel12-stock-postgresql-v1`. The execution digest binds that identity to the
plan digest. This supports stock defaults: no custom Blueprint macros or table
prefix, string length 255, and temporal precision zero. Custom application-wide
schema defaults require a future pinned contract, not an inferred match.

The executor owns a fresh connection, transaction, advisory lock, bounded SQL
execution, current-authority callbacks, and actual ledger/hash reconciliation.
It supports the parsed additive subset in `public`: table creation, nullable
column addition, and nonunique indexes. It verifies actual column, primary-key,
index, sequence, and ledger postconditions before commit. Laravel ledger entries
and source hashes are appended atomically with DDL. Unknown historical hashes
are rejected; there is no silent baseline adoption. A lost commit reply is
reported as uncertain rather than retried blindly or reported successful.

The shared PostgreSQL primitive retains Alembic's implicit single-integer-PK
serial behavior by default. Laravel explicitly supplies its identity choices.
Laravel PostgreSQL unsigned behavior does not add a nonnegative constraint;
`dateTime`, `timestamp`, and generated timestamps use `timestamp(0)` under this
stock grammar contract. This mapping follows the official Laravel 12 PostgreSQL
grammar and schema builder:

- https://github.com/laravel/framework/blob/12.x/src/Illuminate/Database/Schema/Grammars/PostgresGrammar.php
- https://github.com/laravel/framework/blob/12.x/src/Illuminate/Database/Schema/Builder.php

## Verified evidence

- Nine actual-PHP parser tests passed after the final parser corrections.
- Fifteen focused Laravel/PostgreSQL executor tests passed after the reviewed
  default-expression correction.
- `tests/live_migration_laravel_postgres.py`: process **90632**, exit **0**;
  log `/tmp/deploy-monitor-laravel-postgres.log`.
- `tests/live_migration_postgres.py`: compatibility process **10214**, exit **0**;
  log `/tmp/deploy-monitor-alembic-shared-primitives.log`.
- Actual engine: **PostgreSQL 16.15**; immutable fixture image
  `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`.
- Root subsequently reported the complete current backend suite:
  **1,089 tests passed**. That broad suite is separate from the live engine proof.

The Laravel live fixture passed source through the real PHP parser and executed
the resulting typed plan against disposable PostgreSQL. It verified identity
versus plain-primary-key behavior, fractional-second rounding, PostgreSQL
unsigned compatibility, nullable additions, indexes, exact migration batches and
hashes, reconciled no-op replay, stale-state/hash/wrong-database rejection,
destructive-plan rejection, and rollback after a mid-migration failure. Existing
business sentinel rows remained unchanged.

Review added two ledger protections with real engine regressions:

1. Incoming foreign keys to ledger tables are rejected under the retained table
   lock, preventing internal cascade actions from affecting business rows.
2. Serial defaults must match builtin `nextval` on the exact owned sequence and
   carry the expected catalog dependency. Merely retaining an owned sequence is
   insufficient. A malicious replacement default that would delete a business
   sentinel was rejected before candidate schema changes or ledger insertion;
   the function did not run and the sentinel remained intact.

Frozen executable hashes used by the final runs:

```text
migration_laravel_postgres.py
 d1e0e0eb2f2163b165bc6eed2fc5a3211bd1ff59e53ecce8156bc44573f52cb1
migration_postgres.py
 73a85daf94b8ccfd9aadc49cc243c4d81cf60de44ac3eca1f9e14b1f98cd42c5
```

## Isolation and remaining work

Both live runs used disposable synthetic containers labeled under Compose project
`deploy-monitor`. Cleanup checked the exact captured container ID and fixture
ownership label, removed that fixture and its disposable volumes, verified no
owned container remained, and preserved all pre-existing container identities,
images, and running states. No existing database or production service was
changed.

Remaining work includes immutable migration binding/credential/grammar pins in
job targets, worker orchestration and recovery, explicit historical-ledger
onboarding, and full deployment-pipeline acceptance. Laravel MySQL/MariaDB DDL
execution is not supplied by this PostgreSQL module. Cluster/schema
administrators and the privileged migration role remain trusted; the executor
does not isolate arbitrary application startup code that separately holds
privileged credentials.
