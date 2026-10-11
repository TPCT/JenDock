# Shiftcodes deployment management

English operator dashboard at **https://monitoring.shiftcodes.net/** with a real
login page, GitHub branch selection, test-only builds, automatic and manual deployments,
retry, pending-job cancellation, event history and an administration audit log.
It uses neither GitHub Actions nor Jenkins.

## Architecture

The receiver has no Docker socket. It authenticates administrators with a salted
PBKDF2 password hash and issues a random, hashed-at-rest session token. Production
cookies use the `__Host-` prefix, Secure, HttpOnly and SameSite=Strict. Mutations
require an exact configured Origin plus the session CSRF token. Login attempts
are rate limited. Sessions expire after one hour without an authenticated user
request and at twelve hours absolute; background event streaming does not extend
an idle session. A reload or management action does.

The worker has Docker authority and no public listener. It serializes builds,
fetches branch heads with the existing approved SSH identity, tests images
without production secrets or networking, changes only the configured Compose
services, and verifies runtime image identities and availability. Its recovery
journal is isolated from the receiver database and survives restarts.

## Operator workflow

1. Log in, open a project, and refresh its real GitHub branches if needed.
2. Choose a branch and start a build. The confirmation shows its full commit.
3. Choose **build and test only**, or **build, test and deploy** when the project has a complete recipe. A build choice does not change the automatic branch.
4. Follow the job's recorded stages. Failed or cancelled jobs offer a new build
   of the selected branch's latest commit, explicitly shown before confirmation.
5. To change automatic deployment, save the branch and automatic switch in the
   separate GitHub settings section. Saving settings never launches deployment.

Manual requests are durably idempotent, and concurrent manual requests for a
project cannot enqueue duplicate active work. Pending jobs can be cancelled;
an in-progress deployment is allowed to complete or recover safely.

## Container operations

Open a project's Services tab, then **Manage container**. Details expose the
actual image tag and ID, OCI commit when recorded, runtime health, restart count
and persistent mounts. Start, stop and restart require an explicit confirmation.
Operations serialize against deployments and have durable request IDs and
secret-free audit history. Unmapped shared infrastructure is observational only.

The Environment tab masks values until individually revealed. Editing or adding
variables stores per-service Compose overrides, preserving shared env files and
unedited services. Save previews changed key names, verifies the container and
configuration revision, checks that the configured image matches the current
image, and recreates only the service without pulling or building. A stopped
service remains stopped. Runtime environment and image identity are checked;
failed activation restores the original configuration and prior runtime state.
A private temporary journal supports crash recovery and is removed after success.
An unresolved recovery blocks further mutations and deployments.

Database password environment changes do not rotate an existing database account.
Operators must coordinate real database account changes and dependent services.
The editor does not claim to implement database administration, a host terminal,
or full Webmin feature parity. Secrets never enter job/audit records or URLs.

The Terminal tab opens an interactive `/bin/sh` inside the selected running
container, with its configured user. Sessions are bound to the authenticated
operator session and exact container identity. Input is sequenced, output is
bounded, and sessions close after two disconnected minutes or thirty minutes
absolute. Commands and terminal output are not stored in the audit database.
Mobile shortcut keys expose Tab, Escape, interrupt, EOF, arrows and Enter.
This does not expose a host shell. Terminal connection tests are separate from
application deployment acceptance.

The receiver forwards authenticated and CSRF-protected operations to the worker
through a group-restricted Unix socket on their shared volume. It still has no
Docker socket and the worker exposes no public control port.

Service details and environment editing use dedicated pages, keeping runtime actions
above technical metadata. Mobile tables become labeled, stacked rows.

The English navigation uses a desktop sidebar and a mobile off-canvas sidebar
with breadcrumbs. Searchable keyboard/touch listboxes replace visible native
selects. SSE supplies project, operation and runtime updates; editing is preserved
while observations update, and lost connections are visibly marked stale.

## Pipeline rollout boundary

This dashboard does not certify every application's test suite. The policy file
is authoritative: a complete recipe is required for deployment. Automatic deployment defaults on; saved owner opt-outs remain off. Projects with recipes can
run test-only validation; missing recipes are explicitly unavailable. At the
start of this dashboard expansion, captcha had a successful real GitHub-push
build/test/deployment, DSL01 had validated without cutover, and DSL02/CRM tests
had failed before any production change. Other project pipelines remain under
validation. The existing twenty repository hooks are distinct from this gate.

## Production layout

Compose project: `deploy-monitor`. Receiver binds only `127.0.0.1:19190`.
Apache terminates TLS on `monitoring.shiftcodes.net` and overwrites `X-Real-IP`.
Configuration lives in `/opt/docker-engine/projects/deploy-monitor`.
`policy/projects.json` is a directory-mounted, administrator-owned allowlist;
API requests cannot supply Docker commands, paths, repositories or recipes.
SQLite stores selected branches, sessions, jobs and sanitized evidence.

The host-generated `credentials.json` contains a webhook secret and password
hash; it is never committed. The bootstrap plaintext `admin-password` file was removed after login verification.
Only the salted password hash remains. Never publish credentials or log their contents. Existing project environment files, S3 settings and
database volumes are preserved.

Certificate issuance uses the existing pinned Certbot Docker image and shared
HTTP-01 webroot. The monitoring certificate is in the existing Docker renewal
configuration, not a new native Certbot installation.

## Deployment and cleanup guarantees

- Signed GitHub raw payloads must match repository ID, full name and saved branch.
  Recent delivery IDs deduplicate; newer pushes supersede only pending webhook jobs.
- Builds bind an immutable commit and verify the chosen branch head again before
  cutover. Migration changes stop for review. No database migration is run here.
- Each job uses unique image tags; retrying the same commit cannot overwrite a
  running tag. Source is cloned inside a temporary worker volume directory and
  erased afterwards. No host application source mounts, release folders, worktrees
  or checkout switches are introduced.
- The prior image environment is journaled before mutation. A failed cutover
  restores it and checks service availability. Failed recovery stops deployment.
- Old image references are removed only after successful health checks, without
  force. Job images not referenced by containers and unused build cache are cleaned.
  Volumes are never pruned. Rollback images survive until the health gate passes.
- The UI exposes sanitized stage events. Bounded raw failure diagnostics remain
  private to the worker for seven days. They may contain sensitive application
  output and are deliberately not an unaudited browser log endpoint.
- Per-container CPU/memory and resources carry observation timestamps. Failed
  polling or failed collection marks retained evidence stale without clearing
  active forms or changing the chosen branch.

A healthy process or HTTP response proves availability, not a complete business
workflow. External uptime monitoring and outbound alerts are not configured.
Docker isolation does not prove integrity of the previously compromised host.

## Verification

### Optional authenticated MCP endpoint

`compose.mcp.yml` extends the receiver/worker deployment with the official SDK
sidecar. Set `DEPLOY_INTEGRATION_KEY_FILE` to the existing private encryption-key
file used by the receiver; preserve this key when encrypted integrations exist.
Both receiver and MCP must be able to read the same key as UID 65534. The overlay
refuses to create missing host bind files. It is packaging, not proof of an
activated production endpoint.

Compose validation (does not start services):

```sh
docker compose -f compose.yml -f compose.mcp.yml config --quiet
```

The sidecar uses the shared registry database and current console owner file,
has no Docker socket, and publishes only `127.0.0.1:19192`. It waits for the
worker healthcheck at startup. Its own unauthenticated probe requires the
listener to reject requests with 401; that proves listener/auth-boundary
availability, not database or provisioning health.

Include `deployment/apache-mcp.conf` inside the existing monitoring TLS vhost
before the catch-all `ProxyPass /`, then validate Apache and verify actual TLS
requests before declaring deployment complete. The route preserves the exact
public Host and removes forwarding headers rejected by MCP. Keep the existing
certificate and dashboard routing. See Apache's [ProxyPass ordering and
ProxyAddHeaders documentation](https://httpd.apache.org/docs/2.4/mod/mod_proxy.html).

Create scoped, expiring bearer credentials in Integrations → MCP access.
The one-time token is not recoverable; revocation is immediate. OAuth is not
implemented. Run one sidecar process because rate limiting is process-local.
`operation_status` supports `after_event` and `limit` for sanitized progress;
recovery currently directs an authorized operator to the console.

MCP tests require `requirements-mcp.txt` in the test Python environment.
`tests/live_mcp_creation.py` exercises SDK → real local Docker worker → console
HTTP on temporary synthetic data and cleans only its fixture resources.
`tests/live_mcp_proxy.py` checks the Apache route against the running local MCP
preview without tokens. Neither test is production acceptance.

```sh
python3 -m unittest discover -s tests -v
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
```

Tests cover HMAC and repository/ref boundaries, real HTTP login/cookie/CSRF/Origin
behavior, idle expiry, persistence, settings routing, immutable job metadata,
retry/cancel/audit, concurrent deduplication, image environment updates and
restart recovery. `tests/live_recovery.py` is a separately controlled Docker
fixture, never an automatic production-application failure test.

Build committed source with `build_service.py`; install committed policy and
images with `deploy_service.py` only while the worker is idle. New public routing
is managed narrowly by `configure_domain.py` with a trusted Apache syntax check
and graceful reload. The GitHub hook helper reuses existing matching hooks.

GitHub SSH uses ssh.github.com:443 with HostKeyAlias=github.com, strict host-key checking and the existing pinned known_hosts. The VPS connection to GitHub port 22 timed out during production acceptance; port 443 passed authenticated repository access.

## Design preview boundary
The current redesign uses an English project catalog and shared service workspace.
A local preview can use explicitly labeled captured service metadata and proposed
deployment settings; it is not evidence that those settings are active on production.
Preview operations are disconnected. Environment values are not copied.
The design review covers rendered desktop/mobile workflows; production terminal
and deployment acceptance require separate live checks after rollout.

## Host monitoring
The worker reads the verified host CPU, memory, uptime, load and eth0 counters
through five read-only proc file mounts. Disk usage is the filesystem containing
`/opt/docker-engine/projects`. Host collection is independent of Docker observation.
Current samples refresh every five seconds; SQLite retains one sample per fifteen
seconds for twenty-four hours. Charts offer 15m, 1h and 24h windows with bounded
sampling; missing history and counter warm-up are never presented as zero.
Disconnected or expired readings are marked historical and live KPIs are masked.
The dashboard shows current resource threshold warnings, not external alert delivery.

## React interface

The active frontend source is `frontend/src`, using React, Material UI and xterm.
The receiver image serves Vite's `dist` at its existing authenticated API origin.
`web/` is retained as legacy source and is not copied into the production image.
No Node process runs in the receiver. The local product runtime must mount
`frontend/dist` at `/app/web` after building; use the product's `deploy-monitor`
Compose project. Never mistake captured local observations for production health.

The project workspace owns its services, deployment history and persisted branch
settings. SSE observations do not reset drafts or focus. Ambiguous build responses
retain the submitted request identity for explicit retry; do not change its branch
or commit while its outcome is unknown. Historical deployment SHAs are labeled
separately from currently observed container images. Host graphs expose timestamped
samples with pointer and keyboard inspection, including missing/stale states.

The sign-in page uses external labels without asterisks. Required input semantics,
inline validation, first-invalid-field focus and server authentication remain active.
