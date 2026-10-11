# Environment-scoped adoption of existing workloads

Implemented backend scope, 2026-10-11. This is ownership association of existing
containers and data; adoption does not provision, start, move, or delete them.

## Evidence and compatibility

`workload_adoptions` is keyed by `(project, environment)`. `adoption.initialize`
atomically migrates the former project-only primary key, preserving every original
column value, evidence JSON byte, fingerprint, actor and timestamp. The operation
participates in an existing caller transaction or opens its own. Unsupported
primary-key layouts fail explicitly. Readers select both project and environment;
no fallback to another environment's evidence is permitted.

The retained owner still pins its original recipe revision, recipe digest and
adoption fingerprint. Its evidence survives a later managed-application recipe
binding and SQLite reconnect. The first legacy adoption keeps the project's
`legacy_binding` compatibility anchor. Subsequent adoption does not overwrite
that anchor or the first environment's evidence, recipe or settings.

Default adoption retains the existing whole-legacy-workload behavior. Its recipe
comes from the original registered legacy document, not the compatibility view
that later points to an already adopted or managed environment.

## Explicit selection API

Private worker APIs `adoption.preview` and `adoption.adopt` accept two optional
keyword arguments, forwarded by the authenticated control actions:

- `services`: a nonempty list of unique service names from the selected registered
  recipe. This supports disjoint development/production services in one Compose
  project. It is never inferred from a container name or environment label.
- `recipe_revision`: an existing immutable `registry_recipes` revision owned by
  this project. This supports independent Compose groups and files. Callers cannot
  submit raw recipes, paths or commands through adoption. A recipe's digest is
  checked before use.

When omitted, neither selection field is added to legacy evidence. When present,
`selected_services` and `selected_recipe_revision` are bound into the preview
fingerprint, persisted receipt, and sanitized public preview. Exact retry must
match the original selection as well as fingerprint and actor. The accepted
scoped recipe preserves the registered Compose configuration while restricting
its deployment services and owned services to the accepted selection.

The worker must supply its **complete fresh inventory**, not hide other containers
to manufacture an environment boundary. The verifier checks actual Compose group,
ordered configuration files or trusted equivalence proof, required service
membership, database endpoint identity and persistent storage. Saved adoption
records additionally reject container reuse or overlapping storage across other
projects/environments, even if a conflicting peer is missing from the current
inventory. Existing registered and retained Compose claims remain authoritative.

## Acceptance and remaining user workflow

Focused tests cover two environment-specific database adoptions, scoped candidate
resolution, exact replay after reconnect, independent registered Compose groups,
container/storage overlap rejection, invalid selection, foreign/missing recipe
rejection, and byte-preserving legacy-schema migration with outer-transaction
rollback. Existing adoption, retained-owner, candidate and ownership suites remain
passing. These tests do not establish live Docker or browser acceptance.

The dashboard now loads an authenticated `adoption-options` catalog containing
registered recipe revisions, digests, Compose group names and service names only.
Executable recipes and host paths stay private. Selection is bound to the preview
fingerprint and reused for confirmation and in-session uncertain-response replay.
The success response includes the exact environment and fingerprint receipt; the
first legacy compatibility anchor is not evidence that a second environment failed.
The linking action stays available for another unprovisioned environment.

The UI checkpoints the exact submitted request in session storage before dispatch,
scoped by authenticated username, project and environment. It stores the fingerprint
and recipe/service selection only, not the inspected database/container details.
An unconfirmed request survives reload and exposes Check linking request even if
the environment has already been linked. It cannot silently obtain a new preview.
Storage failures prevent new dispatch, and receipts must match before clearing.
Owner/scope changes remount the dialog; late responses cannot refresh another scope.

Frontend helper tests cover serialization, scope and corrupt storage rejection;
123 frontend tests and the production frontend build pass. Browser acceptance of
the selectors, dropped responses and full reload recovery remains unverified.
Editable, not-yet-submitted selection drafts still do not survive a full reload.
Accepted request replay now checks the exact historical receipt before global
workload projection, the recovery journal or Docker collection. It verifies actor,
project/environment, fingerprint, recipe/service selection and the canonical
stored evidence digest. Replay makes no adoption/ownership/revision changes and
does not claim current runtime health. New requests retain the live verification
and journal gates. Focused tests exercise Docker outage, a pending journal,
invalid identities/selections and corrupted evidence. A public recipe
registration/editor workflow also remains unimplemented; an explicit revision
must already exist through trusted configuration. Do not invent recipe records
or bypass adoption to make a workflow appear complete.

Verification checkpoint: the configured local Python environment completed all
1,335 backend tests. A separate reviewer independently ran 25 adoption/recovery
checks and found no blocking regression. The initial system-Python run failed
because psycopg, MCP and httpx2 were absent; the existing project environment
required no installation. Browser dropped-response/reload acceptance remains open.
