# Environment-scoped database inspector

Status: reviewed-design draft for the approved next implementation direction. No inspector implementation or database acceptance is implied by this document.

## Product outcome and scope

An authorized user selects a project and its Development or Production environment, opens Database, chooses a verified database binding, and inspects schemas, tables, columns, primary/foreign keys, indexes, and bounded pages of permitted rows. PostgreSQL and MySQL/MariaDB are the target engines. PostgreSQL is the first adapter implementation, not completion of the whole feature. Engine support and unavailable capabilities must be visible. SQLite requires a separate file/database ownership design and is not implicitly supported.

The inspector does not provision a replacement database, move storage, start stopped services, inherit another environment's credentials, or execute application migration code. Database writes and arbitrary SQL execution are outside this initial interface. This is not an approval subsystem: preview and accept are ordinary identity review and optimistic-concurrency operations, followed by automatic authorized execution.

The user workflow must support legitimate empty databases, missing binding/credentials, permission denial, stopped services, stale identity, connection failure, and unsupported adapter capabilities. Metadata access and business-row access are distinct permissions. A rendered table or successful connection alone is not product acceptance.

## Existing integration seams and gaps

- `registry.py`: `registry_projects`, `registry_environments`, and immutable `registry_recipes` provide project/environment revisions and deployment ownership. They contain no database binding or database secret reference today.
- `ownership.py:resolve`: resolves exact registered ownership from Compose project, service, ordered configuration files, and provenance. Inspector binding requires an exact registered environment. Do not use the unclaimed legacy-prefix fallback as database authority.
- `adoption.py:collect`, `preview`, and `adopt`: existing worker observations include sanitized client endpoint configuration, resolved database container/port/name, and private persistent-storage evidence. Adoption rejects ambiguous resolution and cross-workload shared storage. Saved `workload_adoptions.evidence` is historical configuration evidence, not a live authenticated database identity or proof of data integrity.
- `managed_services.py`: immutable service definitions and current bindings supply managed application identity. They are not a database provisioning or credential contract. Do not invent a managed database service ID for an existing Compose database.
- `integrations.py:_cipher` and `credential_for_use`: demonstrate encrypted storage with an external key and internal-only decryption. The current provider-keyed token schema is not a general database secret store; introduce purpose-specific database credential records.
- `application_operations.py:_service_authorize` and `recheck_credential`: reuse the project/repository/environment authorization and fresh credential checks through shared database application operations.
- `control.py:execute`, `inspect_target`, `command`, and `LOCK`: existing worker inspection boundary. Database actions must independently require exact environment identity, current binding revisions, and current principal authority rather than relying only on project/container names.
- `server.py:Application` and `mcp_server.py`: adapters over shared operations. Add explicit database read actions/tools only when the corresponding worker capability exists. Credentials must never be accepted through model-visible tool arguments.
- `worker.py:migration_policy` and `run_migrations`: existing development-only Laravel execution inherits application configuration. It must not be reused for inspection. `docs/specs/production-migration-executor.md` separately requires a pinned database identity, secret reference, engine adapter, and actual migration-ledger evidence.

## Durable identity and records

Use revisioned bindings with immutable definitions, separate observations, and separate encrypted secrets. Names below define the implementation contract; final SQL constraints must preserve these invariants.

`database_bindings` contains an opaque ID, project, environment, revision, enabled state, immutable definition ID, and timestamps. Changing a binding creates a new immutable definition and advances the binding revision using CAS.

`database_binding_definitions` contains canonical document, digest, and creation time. The document includes:

- schema version, engine, and trusted adapter version;
- exact project/environment;
- ownership kind (`adopted-compose` or `managed-service`) and its real existing reference/revision;
- registered Compose service/configuration provenance or managed service definition identity;
- database name and port;
- secret ID and exact secret revision;
- permitted schemas and inspection-policy revision;
- accepted endpoint/storage identity evidence references.

`database_binding_observations` records the binding revision, actual container ID/image ID, network and storage identity digests, authenticated database identity, effective credential-privilege digest, observation timestamp, and verifier version. Host mount paths stay worker-private. A public response reports which identity components were verified; missing engine-level identity must never be fabricated or represented as a full match.

`database_credentials` contains opaque ID, project/environment, revision, enabled state, encrypted payload, and timestamps. Bind secret ID, scope, revision, and purpose inside authenticated ciphertext. Keep the encryption key outside the application database in a private administrator-configured file. Encryption/decryption errors are sanitized. Rotation advances the secret revision and invalidates observations/cursors until reverified. Credentials and connection secrets must not enter logs, audits, immutable public definitions, URLs, Docker labels/arguments/environment inspection, or response bodies.

A dedicated inspection account is configured explicitly. Do not silently extract an application password, reuse a database administrator account, or create grants as a side effect of browsing. Production and Development never inherit each other's secret reference.

## Binding current services without fabricated identity

1. Discover candidates from the selected registered environment's real recipe and saved adoption relationships. Discovery reports evidence age and supported engine candidates; image-name heuristics alone are not engine verification.
2. Refresh Docker observations through the worker. Resolve the application endpoint to exactly one database container using exact network identity, alias/address, and database port. Verify Compose provenance or managed binding plus actual image/container identity.
3. Compare persistent-storage identity against adoption/current ownership records. Ambiguity, another environment's ownership, and unexplained shared storage fail closed. Future shared-database support requires an explicit model; no automatic exception is inferred.
4. Authenticate with the explicitly selected inspection credential using a trusted engine adapter. Verify actual database name, engine/version, account identity/effective read capabilities, and available stable engine identity. Specify the exact supported engine identity queries and required grants in adapter tests. Do not create identity tables or request superuser credentials merely to make an inspector appear verified.
5. Return a sanitized preview that captures project/environment, recipe/service, container/network/storage, binding-policy, credential, and engine-observation revisions/digests.
6. Acceptance reauthorizes, refreshes authoritative observations, compares the preview fingerprint, and persists through a short CAS transaction. Do not hold SQLite transactions across Docker or database calls. Same idempotency key plus same payload replays; a conflicting payload fails.

Container replacement requires fresh authoritative verification. Stable service names and aliases alone do not authorize a replacement database. A verified replacement may advance the binding observation only through a deliberate revision-pinned rebind operation with matching storage/engine evidence; it is never an inspection-side silent mutation.

## Typed read-only driver boundary

Implement a trusted, pinned helper/driver owned by the product, not candidate application code. A practical local Docker connection mode is `network_mode=container:<verified database container ID>` with a loopback database connection. This avoids publishing database ports or adding an unexpected network endpoint to managed application networks. The implementation must prove this mode against actual Docker ownership and peer-isolation tests before selecting it as the supported runtime path.

The helper receives a closed structured request through a private input channel. It has no application/data mounts, Docker socket, shell command input, or application environment. Use a non-root user, dropped capabilities, read-only filesystem, bounded resources, fixed network mode, and a total execution deadline. Pin the target container before launch and recheck it before releasing results. Credentials must not be passed in Docker argv or environment variables. Output must be framed, size-bounded, sanitized, and never include raw driver errors or credentials. On timeout, kill/reap the actual helper, not merely a Docker attach client.

Each engine adapter exposes typed metadata and row-page operations. PostgreSQL and MySQL/MariaDB need separate implementations for catalogs, identifier quoting, permission checks, read-only transactions, statement/lock timeouts, cancellation, and stable pagination. Do not claim that one engine's SQL or privilege model applies to another.

For every operation:

- Check current principal, exact environment, enabled binding, secret revision, and policy revision before connection and before returning evidence.
- Use the dedicated account's real database read grants plus explicit read-only transactions. A `SELECT` prefix check is not a permission boundary.
- Accept no SQL, DSN, host, URL, driver option, expression, or arbitrary connection parameter from read requests.
- Resolve schema/table/column identifiers against permitted catalog results and quote through the driver identifier API; parameterize values.
- Enforce fixed bounded connect, statement, lock, and total deadlines; bound rows, cells, serialized bytes, and catalog pages.
- Avoid automatic whole-table counts. Use stable keyset pagination with a supported unique key; otherwise report the pagination capability limitation explicitly.
- Use opaque authenticated cursors bound to principal, binding/secret/policy revisions, table identity, schema fingerprint, and query shape. Cursors must not expose sensitive primary-key values. Changed schema or binding invalidates the cursor with a recoverable refresh action.
- Define unsupported executable views, foreign tables, custom types, and policy/function-dependent reads conservatively for each adapter. Arbitrary SELECT execution can invoke functions; do not represent general SQL access as harmless read-only inspection.
- Apply server-side row/column access policy and explicit secret-column exclusions. Name-based redaction is supplemental and cannot guarantee that arbitrary business data is secret-free.

External database endpoints, if later supported, require administrator-configured identity and TLS policy plus pinned address connection with verified hostname identity. They are not arbitrary user-entered network destinations.

## Shared API and transport paths

Proposed domain entry points:

- `database_options(db, principal, project, environment)`
- `preview_binding(db, principal, project, environment, candidate_id, secret_id, expected_revisions)`
- `accept_binding(db, principal, project, environment, preview_fingerprint, idempotency_key)`
- `list_databases(db, principal, project, environment)`
- `inspect_schemas(..., binding_id, expected_revision)`
- `inspect_tables(..., binding_id, expected_revision, schema, cursor, limit)`
- `inspect_table(..., binding_id, expected_revision, schema, table)`
- `inspect_rows(..., binding_id, expected_revision, schema, table, cursor, limit)`

HTTP actions follow the existing action dispatcher: `database-options`, `database-binding-preview`, `database-binding-accept`, `database-list`, `database-schemas`, `database-tables`, `database-table`, and `database-rows`. MCP read tools call the same domain entry points after their capabilities exist; they do not decrypt credentials or accept passwords. Binding/credential management is a distinct authorized configuration flow, not an implicit side effect of a read tool.

Introduce distinct scopes for binding management, metadata reads, and business-row reads; suggested names are `databases:manage`, `databases:read`, and `databases:data:read`. New scopes do not expand existing credentials. Require explicit environment bounds and existing project/repository grants. Console and MCP must converge on the same authorization rules, and the worker must re-resolve current credential/owner authority rather than trusting a retained principal sent by a transport.

Return structured errors such as `binding_required`, `credential_required`, `permission_denied`, `target_stopped`, `identity_changed`, `cursor_stale`, `timeout`, and `unsupported_capability`, without raw backend messages. Read responses include binding revision, observation time, engine/capabilities, bounded data, and pagination state. Audit action/target/outcome and counts, not passwords, raw queries, or row contents.

## Phases and evidence boundaries

Discovery is a candidate inventory. Binding preview is recent verified evidence awaiting CAS acceptance. Accepted binding is durable authorization/configuration, not a permanent health claim. A successful inspection is a bounded observation at a stated time. Stopped/unreachable/replaced targets must not be represented as empty data. Stored metadata alone must not produce a fresh verified indicator.

The first implementation slice is PostgreSQL binding/credential storage, authoritative verification, metadata inspection, bounded permitted row pages, and the Environment Database workflow through real HTTP and worker execution. MySQL/MariaDB remains an explicit pending adapter until implemented and accepted. Neither slice completes the production migration executor: that additionally needs framework ledger/graph reconciliation, parsed additive operations, engine locks, and durable migration outcome evidence.

## Required acceptance

Use disposable, clearly labeled databases in the actual product's local Compose project, with exact tracked cleanup and unchanged unrelated peers. Do not clone production databases or modify production grants.

Exercise both environment flows through authenticated HTTP and the real worker/helper:

- Bind existing PostgreSQL services using real registered ownership, read-only credentials, actual database identity, and distinct business sentinels.
- Inspect schemas/tables/columns/keys/indexes; read multiple bounded row pages, including Unicode, nulls, binary/large cells, empty tables, and tables without supported pagination keys.
- Confirm reload/persistence, cursor continuity, permitted empty states, and explicit permission/stopped/unsupported errors.
- Reject cross-environment IDs, wrong database/account, ambiguous alias, changed container/network/storage, secret rotation/revocation, stale schema/cursor, and mismatched preview revisions.
- Verify direct write denial by the inspection role, fixed-query enforcement, timeouts/cancellation, bounded output, no credential leakage, and no data/volume/network-peer changes.
- Confirm timeout cleanup kills/reaps the exact helper and that the normal inspector never starts stopped services.
- Verify UI metadata-vs-row permissions, keyboard/touch navigation, clear loading/recovery states, desktop/mobile layout, and accurate supported-engine labels.

Repeat engine-specific acceptance for MySQL/MariaDB before advertising those adapters. Report unit tests, image packaging, actual engine acceptance, browser acceptance, and any remaining production-migration gaps separately.
