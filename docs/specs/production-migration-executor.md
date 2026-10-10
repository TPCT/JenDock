# Automatic production migration executor

Status: reviewed implementation design; current development-only runner is not production support.

Owner contract: automatic pipelines without approvals; production destructive database changes prohibited. Additive production migrations must have a supported automatic path. Development reset behavior must never resolve to production storage.

Current gaps: changed Git files are not the database pending set; the existing Laravel development runner executes arbitrary project code with configured network/environment/mount access. Existing tests do not prove production safety. No Alembic executor exists.

Required execution boundary:

1. Pin database binding, framework/adapter version, migration paths, ledger schema and secret reference in the job target.
2. Read the actual migration ledger using a trusted connection. Laravel requires migration name/batch reconciliation; Alembic requires all current heads and revision graph resolution. Do not import candidate code with live credentials.
3. Parse a strict useful additive subset into typed operations using real PHP/Python syntax parsers. Create tables, nullable columns without executable defaults and nonunique indexes can be supported. Unsupported/raw/dynamic SQL, connection changes and destructive operations fail with concrete filenames/reasons. Classify executed up operations, not destructive rollback methods alone.
4. Trusted engine adapters receive credentials and execute typed operations. Candidate code gets neither migration credentials nor writable database files. A PostgreSQL object owner cannot be restricted to alter-but-never-drop using ordinary grants.
5. Acquire engine-specific locks, recheck ledger/hash/schema/database identity and then apply. Persist sanitized per-migration evidence and explicit uncertain recovery for nontransactional DDL.
6. Verify actual ledger and schema after execution. Process exit status alone does not prove changes ran.

`--pretend` and Alembic offline SQL execute candidate code and are not credential isolation boundaries. Safe migration execution also does not prevent arbitrary application startup code using a privileged application account; deployment templates must address that distinct capability.

Required acceptance: real Laravel and Alembic additive migrations against disposable databases with preserved business sentinels and ledger proof; destructive/raw-SQL indirection, alternate connection, stale ledger, changed hash and wrong binding rejection. No production mutation until implementation has this evidence.
