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

## PostgreSQL driver and private image

`database_postgres.py`, `database_inspector_entry.py` and `Dockerfile.database`
now implement fixed catalog inspection and bounded row previews through the actual
namespace-pinned helper. Secrets enter on stdin, never Docker environment/argv.
The driver verifies database/user identity and rejects writable inspection roles,
including column-only grants. Row policy is a nested schema/table column allowlist;
when supplied to saved binding definitions it is included in their immutable digest.
An omitted row policy is metadata-only, not implicit access to all columns.

Rows currently require an ordinary heap table, built-in supported scalar columns,
and a single integer primary key. RLS, inherited/partitioned/foreign/view relations,
custom types and unsupported pagination fail explicitly. Encrypted keyset cursors
bind policy/principal context supplied by the trusted caller and relation schema.
Text previews preserve NULL/empty differences and truncate at 256 characters.
Page budgets, statement and lock deadlines, response size, and helper wall-time
bounds apply. Pages are separate live observations, not a cross-page snapshot.

The driver takes ACCESS SHARE on ONLY the selected table, then refreshes metadata
under READ COMMITTED and checks the same OID before rows. The retained lock prevents
concurrent ordinary relation DDL from invalidating capability checks. SQL table
locks do not prevent schema rename. Rows therefore additionally require schema
administration owned by a cluster superuser, with no non-superuser members of that
owner role; pg_database_owner resolves to the actual database owner. This initial
adapter trusts cluster administrators and does not claim safety against concurrent
administrator namespace substitution. Other schema ownership arrangements receive
`unsupported_schema_owner`; the tool never changes ownership or privileges.

Actual local Docker/PostgreSQL fixtures verify different Development/Production
sentinels, catalog/keys/FK data, two row pages, NULL/empty/truncation, encrypted cursor
scope, quoted/dotted identifiers, column-grant denial, wrong database, lock timeout,
cleanup and preservation of pre-existing Docker peers. Two-connection regressions
verify RLS committed before the lock is rejected, and post-lock RLS/rename remain
blocked through the row read and complete after transaction release.

This driver evidence is not end-to-end row-read or UI acceptance. A subsequent
console configuration slice now adds live worker authentication, save replay and
HTTP/Unix configuration acceptance; see `database-console-20261011.md`. A private
registered candidate resolver is also implemented. Live candidate collection,
principal-owned preview acceptance, real policy resolution and authenticated
end-to-end database reads remain required. Database UI and MCP reads remain open. MySQL/MariaDB adapters and general row type/key
support remain part of the full product scope. No production deployment occurred.

Local validation after this driver slice: 27 focused database tests and 955 backend
tests passed. The full suite emits existing ResourceWarning diagnostics for some
test-owned resources; this is not a warning-free result. The helper image builds
successfully with its pinned Python base and PostgreSQL client dependencies.

Final actual fixture passed in both environments with helper image
`sha256:50052ff65971e0bb9dfbc2e940f5549e5caa12915b6d06d27d847b3e6b52ec81`.
Non-superuser schema ownership and non-superuser membership in the trusted owner
role were rejected; after restoring each fixture privilege, inspection recovered.
All exact fixture containers/helpers were removed and existing peers preserved.
Independent source review found no remaining component-commit blocker under the
explicit trusted-administrator boundary; it did not certify public integration.
