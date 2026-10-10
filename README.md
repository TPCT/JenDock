# Shiftcodes deployment monitor

Lightweight authenticated Arabic monitoring and signed GitHub push receiver.
No GitHub Actions or Jenkins. The receiver has no Docker socket. A separate
worker serializes builds, tests images without production secrets or network,
updates only approved Compose services and checks runtime image identities.

## Current rollout status

Implementation is being validated. Entries in `projects.json` remain disabled
until their exact deployed branch, Docker build and acceptance checks have been
verified. A disabled project records a delivery as `awaiting-validation`; it
does not update production. Do not interpret repository inventory as deployment
readiness. External monitoring and outbound alert delivery are not configured.

## Boundaries

- Repository numeric ID, full name and branch must match the administrator-owned
  allowlist; webhook commands and clone URLs are never executed.
- Verify `X-Hub-Signature-256` on raw bytes before reading the payload. Delivery
  IDs deduplicate for thirty days. Pending older pushes are superseded.
- The worker verifies the configured branch head before building and again
  before deployment. Database migration changes require review; this worker
  never invokes a migration command.
- Recovery metadata is in a private worker volume, separate from the receiver's
  delivery database. It is persisted before changing image settings. A failed
  rollback stops subsequent deployments and retains recovery instructions.
- Source is an ephemeral clone inside the worker volume, removed after each job.
  No host application source bind mounts, release folders, worktrees or branch
  switches. Existing project secrets and database volumes stay in place.
- Once deployment health passes, superseded image references are removed without
  force. This job's unreferenced images and unused build cache are pruned. Volumes
  are never pruned. Rollback images remain available until the health gate passes.
- A healthy process or HTTP response is deployment availability, not complete
  business-flow acceptance. Project-specific tests are required separately.
- The worker controls Docker and therefore has host-equivalent authority. Keep
  its configuration root-owned and its network listener absent. Containerizing
  this service does not establish integrity of a previously compromised host.

## Run tests

```sh
python3 -m unittest discover -s tests -v
```

Local tests cover raw signature enforcement, repository/ref rejection, concurrent
deduplication, persistence, authenticated read-only HTTP, targeted env updates and
recovery state. Recovery command tests substitute an executor; live Docker
cutover/recovery evidence must be recorded separately before enabling projects.

## Production layout

Compose project: `deploy-monitor`. Receiver listens only on `127.0.0.1:19190`.
Apache should proxy `https://shiftcodes.net/_deploy/` to that loopback port before
the existing site catch-all. Only `POST /_deploy/github` accepts signed events;
dashboard and state require Basic authentication over HTTPS. Never expose the
loopback HTTP listener to the internet.

Configuration lives in `/opt/docker-engine/projects/deploy-monitor`. The
`credentials.json` file is generated on the host and never committed. It stores
the webhook secret and a salted PBKDF2 password hash. The clear dashboard password
is provided through the owner's terminal, never through logs or chat.

Git credentials are reused from the approved host identity via protected read-only
mounts. Do not disable SSH host verification. Keep per-project recipes disabled
if source history or the deployed branch cannot be established.

Logs do not retain raw webhook bodies, environment dumps or application build
output that could expose secrets. Public state contains only project metadata,
commit identifiers, sanitized step results and timestamped runtime observations.
