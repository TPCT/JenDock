# Authentication and security

## Components and trust boundaries

- The **API** is internet-facing. It has no Docker socket and **no secret encryption key**
  (building the app never reads a key file). All secret encryption/decryption happens in the
  **worker**, reached over a Unix socket (`JENDOCK_WORKER_SOCKET_PATH`).
- The worker owns `SecretCipher`. The key file (`JENDOCK_SECRET_KEY_FILE`, mode 0600) lives in
  the `keys` volume, mounted **only** in the worker (`/run/jendock-keys/secret.key`). The API
  container mounts only `data` and `worker-socket`.
- Worker RPC (`backend/worker/rpc.py`, client `backend/adapters/worker_client.py`):
  JSON lines, method allowlist (`ping`, `secrets.set|list|delete|reveal`, `github.verify_webhook`,
  `github.branch_head`), 2 MiB request limit (a 1 MiB webhook body travels base64-encoded),
  per-request id, 5 s client timeout, structured errors mapped back to domain errors. Each secret
  call carries the acting principal (user or token) which the worker writes to the audit log; the
  worker takes that identity at face value (see below). The socket is created with mode 0660
  inside the `worker-socket` volume, a setgid 2770 directory owned by group gid 10001 in both
  images; only members of that group can connect.
- Users: the API runs as uid 10002, the worker as uid 10001, both in group gid 10001. The data
  volume (SQLite DB, WAL/SHM) and the socket directory are setgid 2770 and every entrypoint sets
  umask 0007, so both processes can write while others cannot. The `keys` volume is 0700 owned
  by uid 10001: the API user cannot read it even if it were mounted.
- **Trust model of the actor identity.** The worker records the `actor` sent by the API in the
  audit log without being able to verify it. The socket is the trust boundary: only processes in
  the shared group can reach it, and the API is the only such process. This is acceptable
  because a compromised API could already do everything its principals can do (it holds the
  database and decides whom to serve), and the worker exposes only a fixed allowlist of secret
  operations, never Docker or key access. If the API is compromised, audit actor attribution
  for that period is unreliable, which is a known limit rather than an isolation failure.

## Browser auth (sessions)

- Admin users use argon2id hashes (`argon2-cffi`), rehashed on login when parameters are weaker
  than the current policy. Minimum password length 12.
- Login creates a server-side session: a 32-byte random token in the cookie, only its SHA-256
  is stored. Idle expiry 1 hour, absolute 12 hours (`SESSION_IDLE_TIMEOUT`,
  `SESSION_ABSOLUTE_TIMEOUT` in `config.py`). Expired sessions are rejected and deleted lazily.
  Background endpoints (the run event stream) use `require_scope(..., background=True)` /
  `get_background_principal`, which authenticate without extending the idle timer.
- Cookie: `__Host-session`, Secure, HttpOnly, SameSite=Strict, Path=/, no Domain. Only a
  development setup with an `http://` `JENDOCK_PUBLIC_ORIGIN` uses the plain name
  `jendock_session` without Secure.
- CSRF: every POST/PUT/PATCH/DELETE authenticated by cookie (and the login request) needs
  `Origin` exactly equal to `JENDOCK_PUBLIC_ORIGIN` and header `X-CSRF-Token` equal to the
  session's CSRF token (`GET /api/auth/session` or the login response returns it). The token is
  an HMAC of the cookie value, so nothing CSRF-related is stored; it is compared with
  `hmac.compare_digest`.
- Login rate limit: 5 failed attempts per username and 20 per client IP per 15 minutes, then
  HTTP 429 with `Retry-After`. State is in process memory (single API process); a restart
  resets it and multiple replicas would not share it. The client IP is the peer address; behind
  Apache/Cloudflare set `JENDOCK_TRUSTED_PROXIES` (IPs/CIDRs, default empty = trust nobody;
  compose var `TRUSTED_PROXIES`, e.g. the docker bridge gateway). `python -m backend.api`
  (the image CMD) then starts uvicorn with proxy headers trusted only from those addresses.
  Without it every client appears as the proxy and the per-IP limit becomes a global lockout.
  Unknown users and wrong passwords return the same 401 and both run one argon2 verification.
- Security headers on every response: CSP (`default-src 'self'; frame-ancestors 'none';
  base-uri 'none'`), nosniff, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`,
  `Cache-Control: no-store`, and HSTS in production. Swagger UI is disabled (its CDN assets
  conflict with the CSP); `/openapi.json` exists in development only.

## Creating the first owner

Passwords are read from a hidden prompt or, with `--password-stdin`, from stdin. There is
deliberately no argv or environment option.

```
make migrate
.venv/bin/python -m backend.cli users create admin            # prompts twice
printf '%s\n' "$PW" | python -m backend.cli users reset-password admin --password-stdin
# inside the deployment:
docker compose run --rm -T api python -m backend.cli users create admin --password-stdin
```

`create` makes an **owner** by default (`--role admin|member` for others; further users are
normally created in the UI, see "Users, roles and access control"). `reset-password` sets the
given password and also ends all sessions of that user. Both commands are audited
(`user.create`, `user.reset_password`, actor `system:cli`).

## Users, roles and access control

- Global roles: `owner` (everything, including users and integrations), `admin` (everything
  except creating, changing, disabling, deleting or resetting owners) and `member` (only what
  out. New users start as `member` unless an owner or admin picks another role.
- Settings → Members (owners and admins): add a member (username, name, optional email, role),
  edit, disable/enable, delete and reset the password. A new member and a reset user get a
  generated **one-time password shown once**; the next sign-in is forced to the change-password
  screen (every other endpoint answers 403 `password_change_required` until it is changed).
  Nobody changes their own role or disables/deletes themselves, and the last active owner can
  never be demoted, disabled or deleted. Disabling or deleting a user ends their sessions and voids
  their API tokens.
- Project Settings → Access (owners and admins): like GitHub's "Manage access". A grant is on
  a project (all environments), one environment, or one container service, held by a member:
  - `viewer`: read projects, runs and step logs, pipelines, deployments, the container inventory,
    domains, database metadata, secret names.
  - `developer`: viewer + start/cancel/retry runs, save the pipeline and browse rows/console
    on **development**.
  - `maintainer`: developer + the same on **production**, redeploy, container logs and actions,
    environment variables and limits, files, terminal, domains.
  - container grants carry `logs`, `terminal` and/or `actions` (start/stop/restart) for one
    service and nothing else.
  - Always owner/admin only: creating/editing projects and environments, database connection
    configuration, secret values, container variable reveal, certificates overview, users and
    access. Production rows and the SQL console stay owner/admin-only unless the member holds a
    `maintainer` grant on that production environment.
- **One policy decides everything**: `backend/services/policy.py` (`authorize(principal, action,
  resource)`, rules in `backend/domain/access.py`). Every router names its action through
  `Depends(require(Action, Target))` (`backend/api/deps.py`) and every MCP tool is mapped to an
  action in `backend/mcp/access.py` (a test fails when a tool has no mapping). Lists (projects,
  runs, containers, MCP tools) only contain what the caller may see; a member asking for a project
  they cannot see gets 404, not 403. Run events, container log follow and terminals re-check the
  caller at every heartbeat, so revoking a grant or disabling a user ends them.
- **Tokens never exceed their user**: a request authenticated by a token is allowed only when the
  token's scopes allow it *and* its owner may do it (role and grants are read at every request).
  Managing users, access and tokens is browser-session only.
- `GET /api/auth/session` returns the role and an effective-permission summary the UI uses to
  hide or disable what the user cannot do (the API stays the authority); see
  `docs/api-contract.md` ("Access control").
- Audited: `user.create`, `user.update` (profile, role, enable/disable), `user.delete`,
  `user.password_reset` (admin reset, never the password), `user.password_change`,
  `access.grant_add`, `access.grant_update`, `access.grant_remove`.

## API tokens (agents / MCP)

- `Authorization: Bearer jdt_...`; no cookie, no CSRF. A request with an `Authorization` header
  never falls back to a session cookie.
- Created only by a logged-in browser session (`POST /api/tokens`, also `GET /api/tokens`,
  `DELETE /api/tokens/{id}`); a token can never manage tokens. The value is returned exactly
  once; only its SHA-256 and an 8-character display prefix are stored.
- Scopes: `read`, `run`, `deploy`, `containers`, `terminal`, `pipelines`, `admin` (`admin`
  implies every other scope except token management, which is session-only). Secrets: list needs
  `read`; set, delete and reveal need `admin`. Sessions hold all scopes.
- A token acts as its owner: the effective permission is the **intersection** of its scopes and
  the owner's role and grants (see "Users, roles and access control"); tokens of a disabled or
  deleted user stop working. A user lists and revokes only their own tokens.
- Revoked or expired tokens are rejected; `last_used_at` is updated at most once per minute;
  token actions are audited with actor type `token`. Runs can record `triggered_by_token_id`.

## Your account (profile, sessions, two-factor)

Open the avatar menu in the header: **Your profile** and **Settings** lead to the settings
pages (API reference: `docs/api-contract.md`, "Account").

- **Public profile**: display name and an optional email. The avatar shows initials.
- **Password and authentication**: the existing change-password screen (current password
  required, rate limited, other sessions ended) is reused; the two-factor controls sit next to it.
- **Sessions**: every live login with its browser and OS (from the `User-Agent`), client address
  (the peer, or the real client behind `JENDOCK_TRUSTED_PROXIES`), last activity and a *This
  device* marker. Revoke one session or all others.
- **API tokens**: your own tokens (create, revoke), listed under Settings.
- **Security log**: your sign-ins and account changes (profile, password, two-factor, tokens).
- **Two-factor authentication (TOTP, RFC 6238)**: *Set up* shows a QR code (drawn in the browser
  from an `otpauth://` URI, no outside service) and the key to type by hand; entering a valid
  six-digit code turns it on and shows ten one-time recovery codes **once**. Login then asks for
  the code after the password (SHA-1, 6 digits, 30 s, one step of clock drift; a code works once)
  or for a recovery code. Wrong codes are rate limited like wrong passwords. Turning it off needs
  the password and a code. Lost your phone and your codes: an operator turns it off with
  `python -m backend.cli users disable-two-factor <username>` (audited as `two_factor.disable`,
  actor `system:cli`); the user can then set it up again.
  *Design:* the TOTP key must be readable to verify codes, so it cannot be hashed. It is
  generated and decrypted **only by the worker** (same Fernet key and trust boundary as project
  secrets, `totp.generate` / `totp.check` RPC methods); the API stores the ciphertext and sends
  it with the code to the worker, which answers with the matching time step only. A database or
  backup leak, or a compromise of the API process alone, does not reveal the keys, and the
  plaintext appears once, in the enrollment response. Recovery codes carry 80 bits of entropy,
  so their SHA-256 is stored (like API tokens). Without a reachable worker, users with
  two-factor cannot complete a password login (recovery codes still work).

## Audit log (`/audit`)

The sidebar's **Audit log** lists every recorded action in words ("Revealed the secret
DATABASE_URL"), newest first, with the actor, time, target and the raw (secret-free) details
behind a disclosure. Filter by search text, actor type, action (or a whole group such as
`secret.*`), project, target and date range; **Load older events** continues with a cursor;
**Export CSV** downloads the current filter (streamed, at most the newest 10 000 events,
spreadsheet formulas neutralised). Owners and admins see every event; a member sees only their
own activity, and only about projects they still hold a grant on (policy action `audit.read`,
`authz.audit_filter`). Everybody sees their own activity under Settings, Security log.

## Home dashboard (`/`)

One request (`GET /api/dashboard`, fixed number of queries) feeds the landing page: needs
attention (running or queued runs, environments whose newest run failed), recent deployments
(status, who or what triggered, commit, duration), pushes waiting for a manual deploy
(auto-deploy off), certificates expiring within 30 days and the host's CPU, memory and disk.
Every card links to its details and explains in its empty state what to do next; members only
see what their grants allow (deployments, runs, domains and certificates per environment, the host
only with any grant); a brand-new
installation shows a welcome with *Create your first project*. If the worker cannot be reached
only the host card says so.

## Secret endpoints

`GET|PUT|DELETE /api/projects/{slug}/environments/{env}/secrets[/{name}]` and
`POST .../{name}/reveal` all call the worker. Values are never returned except by `reveal`,
which is audited (name only, never the value).
