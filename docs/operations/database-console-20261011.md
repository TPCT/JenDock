# Database console configuration and candidate identity — 2026-10-11

This slice exposes authenticated configuration, not database query access.
Production deployment and browser Database-tab acceptance remain outstanding.

## Console to worker

The existing login, Origin and CSRF checks protect `/api/action`. Database actions
reject browser-supplied actor/authentication fields. The receiver injects its actual
session digest. The private Unix client verifies the worker kernel UID before
sending credentials; the worker verifies the receiver kernel UID before dispatch.
Defaults match the shipped worker UID 0 and receiver UID 65534; explicit local UID
settings are deployment configuration, never request parameters.

`database_auth.ConsoleAuthority` rereads the actual SQLite session and configured
owner before work, inside binding write transactions, and before releasing output.
It enforces absolute/idle expiry and timestamp chronology. A supplied actor or MCP
credential ID is not authentication. Processes with the configured receiver UID
and access to the shared socket remain within the installation trust boundary.

Configuration actions cover credential save/list/revoke and binding save/list/get/
disable. Saves require a request ID. Receipts, object changes and audit events are
atomic. Same-key matching requests return the original receipt, conflicting requests
fail. Credential replay requests are encrypted with purpose and context; plaintext
passwords and unkeyed password hashes are not persisted. Save responses carry
`state_requires_reload: true`: a replayed historical receipt does not describe the
current object after later rotation/revocation. Reload the relevant catalog/get
before showing current status. Saving remains explicitly unverified.

The worker requires `DATABASE_INSPECTION_KEY_FILE` referencing a private existing
key to save credentials. It does not generate keys or alter database permissions.
Missing configuration fails closed. No passwords become MCP tool arguments.

Catalogs accept limit 1–100 (default 25) and a scoped-query ID cursor. Clients follow
`next_cursor` until null. A full last page may lead to one empty terminal page.
Requests are bounded to 64 KiB, require complete newline frames, and use strict
JSON. Database responses are capped at 256 KiB. Oversized catalogs return a bounded
413 error; selecting a smaller page recovers. No automatic mutation retry occurs.

## Candidate resolver

`database_candidates.resolve` uses immutable registered ownership, saved adoption
integrity and newly supplied bounded Docker observations. It pins project,
environment and recipe revisions, exact container/image/start identity, network
endpoints and persistent storage. Volume options are represented by digest only.
It rejects missing/ambiguous ownership, unclaimed legacy names, stopped/replaced
targets, shared/overlapping storage, stale evidence, host/shared namespaces, and
unsupported managed database roles. Public projection omits private storage paths.

Candidate tests use real SQLite registry records with supplied Docker observations;
they are not live Docker discovery evidence. A trusted bounded Docker collector,
candidate-to-credential preview/acceptance, accepted binding revalidation, typed
reads, MCP scopes and the actual Database UI remain the next required integration.
This resolver neither authenticates an engine nor discovers container credentials.

## Acceptance evidence

Actual HTTP login -> real Unix socket with SO_PEERCRED -> worker dispatcher ->
SQLite tests cover persistence, stale/unverified state, secret-free public/audit
records, forged fields, invalid CSRF, expired sessions, wrong peer UIDs, and owner
change inside the write transaction. Additional cases cover EOF before terminator
(no mutation), catalog pagination, oversized-result recovery, and repeated HTTP
create returning the original resource without duplicate creation.

Independent review identified framing, response limits and lost-reply replay gaps;
all were addressed and rechecked. Review is not production acceptance. The complete
platform audit remains open, including inspector UI, engine adapters, migrations,
workload tooling and final deployment.

Validation: 984 backend tests passed; after the final receipt-response marker, all
56 database tests passed again. Receiver, worker and MCP images built and passed
network-disabled import checks. Existing ResourceWarning diagnostics remain in
the broader test suite. No production deployment was performed.
