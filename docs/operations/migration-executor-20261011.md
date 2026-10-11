# Alembic/PostgreSQL executor acceptance

This is component acceptance on a disposable database, not enabled pipeline or
production migration support.

`migration_plan.py` parses Python AST without importing candidate modules. It
reconciles actual heads, graph dependencies and applied source hashes before
compiling pending upgrades. Executable annotations and unsupported source
indirection are rejected. Hash-pinned historical upgrades are not executed or
reclassified as pending. Supported operations are explicit table creation,
nullable column addition and nonunique index creation.

`migration_postgres.py` owns a transaction and database advisory lock, pins
database/account identity, reads heads and trusted hashes under lock, reparses
the sources, executes validated typed operations, and verifies catalog/ledger
effects. Candidate code never receives credentials or runs. The `public` schema
and reserved ledger names are explicit. A lost COMMIT reply is uncertain, not
success. A pre-existing Alembic history without trusted hashes needs onboarding;
it is never silently accepted.

Independent review found incoming foreign-key cascades could make replacing
Alembic heads delete referencing business rows. The retained ledger table lock
now encloses an inbound-FK rejection before ledger mutation. The actual engine
regression proves preservation of both child rows and ledger contents.

Final real fixture session 51704 exited zero against PostgreSQL 16.15, pinned
image `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`.
`tests/live_migration_postgres.py` verified serial primary-key behavior, nullable
addition, indexes, actual heads/hashes, no-op replanning, stale heads/source hash
and wrong database rejection, destructive pending rejection, transactional
rollback after a later DDL failure, sentinel preservation and inbound cascade
rejection. Exact fixture cleanup and original peer identity/image/running-state
preservation passed. Log: `/tmp/deploy-monitor-migration-postgres-final.log`.

Nine parser tests and ten executor tests passed again after the final fix.
Executor SHA256: `9111ab43fd48d4f025ef48da44caa498ad7b6d105a0bf28e286dbad8b2dfaec4`.

Remaining: purpose-specific credential/binding and immutable job-target wiring,
durable uncertain-commit reconciliation, onboarding existing history, trusted
helper packaging, Laravel execution and end-to-end deployment integration.
