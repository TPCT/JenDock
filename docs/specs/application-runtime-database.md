# Managed application runtime and database connections

## User outcome

An operator edits a container's Environment settings or selects a database in
that same project and environment. The applied configuration survives application
builds, replacement, failed deployment rollback and worker restart. Development
and Production can run together and have independent settings and credentials.
The existing environment editor remains available. Saving intent, applying it and
observing a healthy runtime are distinct states in the UI.

This implements the authorized full platform scope. It does not add approval gates
to ordinary deployment. Production destructive database operations remain outside
the allowed migration contract.

## Confirmed defect and foundation

Current managed `environment-save` follows legacy Compose editing: it rewrites the
canonical JSON manifest as YAML and replaces a container without advancing its
managed definition or binding. Two executable regressions reproduce both the JSON
failure and the residual manifest-drift failure after serialization alone is fixed.
The fix must cover configuration, identity and recovery together.

Store immutable encrypted runtime configuration revisions independently of image
artifacts. Service definitions and jobs pin reference/digest metadata only. Keep
historical encrypted revisions required by accepted rollout/recovery records.
Managed environment changes use a durable same-image transition with existing
rollout verification, lease/authority rechecks, old/new definition evidence and
compare-and-swap completion. Ordinary subsequent image deployments retain that
runtime reference. Legacy/adopted Compose follows its existing verified path.

Compose manifests and public journals contain no plaintext runtime secrets.
The worker resolves pinned values privately for the child process, and compares
effective runtime environment privately. Provisioning, deployment, rollback,
recovery and ingress verification must use the same configuration contract.
Errors and audit records expose variable names and revision metadata only.

## Database connection

Each optional application database attachment pins the managed service, exact
project/environment, adopted database ownership revision, database name and port,
application credential revision and adapter version. Application credentials use
a separate purpose and immutable encrypted revision history; inspector or migration
accounts are not accepted as application account references.

Create a dedicated internal bridge for each database binding. Attach only the
verified database and explicitly selected same-project/environment applications.
Keep each application's normal environment network; do not attach it to a broad
legacy database network. This is a network-peer boundary, not a single-port firewall.
Preserve the database's existing networks and running/stopped state. Record exact
network/container/endpoint identities and a unique database alias under a durable
intent before effects. Unknown peers, alias collisions and ownership changes stop
the operation. No arbitrary network names or SQL supplied by the browser become
worker authority.

Network attachment changes the database's observed network fingerprint. Record
that authorized before/after transition explicitly; never weaken identity checks
or silently replace a migration proof with uncertain execution. Serialize these
operations with database migration/deployment work and preserve recovery evidence.

The existing container Environment view gains a Database connection section using
same-environment database and application-account choices. Framework connection
variables come from the attachment and are identified as managed values. Changes
must show desired versus applied revision and recoverable operation status.
Revoking a saved account does not erase credentials already delivered to a running
application; distinguish selection revocation from an applied rotation.

## Required acceptance

1. Existing environment edit succeeds, updates registered container identity, and
   survives a subsequent actual Docker image deployment.
2. Normal PostgreSQL adoption plus a separate bounded application account permits
   a real application write and read, without a manually attached test network.
3. Application data and configured values survive another deployment, rollback,
   and worker/SQLite reconnect.
4. Credential rotation uses its accepted revision; rollback resolves the retained
   prior revision. Disabled accounts cannot authorize new selection.
5. Wrong-environment ownership, unexpected network members, alias reuse, replaced
   database containers, stale runtime revisions and lost effect responses are
   tested. Ambiguous effects reconcile rather than replay blindly.
6. Desktop/mobile UI shows saved, pending, applied and failed states accurately;
   reload and unsaved-change navigation work through the real API.
7. Exact local fixture cleanup preserves peer containers, storage and business
   sentinels. Production rollout remains a separate acceptance step.
