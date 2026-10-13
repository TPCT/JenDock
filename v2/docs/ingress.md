# Ingress: Apache, Cloudflare, TLS (Task 11)

How a hostname reaches a container: **Cloudflare DNS** -> **Apache on the host** (reverse proxy,
TLS) -> `127.0.0.1:<published port>` of the container. The HTTP API is in
`api-contract.md` ("Domains"); the host helper's installation is in `ingress-host-helper.md`.

## Components and trust

```
browser --> API (no secrets, no root) --RPC--> worker (secret key, Cloudflare token, ACME)
                                                  |            |             |
                                                  |            |             +--> Cloudflare API (https)
                                                  |            +--> ACME CA (https)
                                                  +--> host helper (Unix socket, root on the host)
                                                          writes ONE managed vhost dir, certs,
                                                          runs apachectl configtest / graceful
```

- The **API** only stores domain definitions and reads status. Every operation that touches
  DNS, Apache or TLS is an RPC to the **worker** (`ingress.preview|apply|remove|check|certificate`),
  because only the worker holds the secret key (the Cloudflare token) and can reach the helper.
- The **host helper** is a separate, minimal process (`python -m backend.hosthelper`) that is
  the only thing with host privileges. Neither container gets root or write access to
  Apache's configuration. Its protocol is a closed list of operations; it never accepts
  configuration text (see "Host helper protocol").
- Settings are `JENDOCK_INGRESS_*` (`backend/config_ingress.py`); ingress is off unless
  `JENDOCK_INGRESS_ENABLED=true` in the worker.

## Domain model

`Domain` (table `domains`): hostname (unique across all projects), environment, `service`,
`container_port` (documentation of the container side), `published_port` (the host loopback
port Apache proxies to), DNS record type/target/proxied flag, `tls_enabled`, `status`
(`draft` -> `active` | `error`), Cloudflare zone/record ids, `last_verified_*`.
`DomainCertificate` holds the current certificate's expiry/serial/fingerprint (never key
material); `DomainCheck` the latest status check. A path prefix is intentionally not modelled yet.

Hostnames (`backend/domain/hostnames.py`): trimmed, lowercased, IDNA 2008 (UTS #46) to `xn--`
A-labels, at least two labels, LDH only, no IP literals, no reserved zones (`.local`,
`.internal`, ...), wildcards (`*.zone`) only when `JENDOCK_INGRESS_ALLOW_WILDCARDS=true`
**and** the helper's `allow_wildcards` is true. The host helper re-validates everything
independently.

## Preview, then apply

`POST .../preview` computes a `Plan` and changes nothing: the DNS part (create/update/noop/
conflict against the real zone), the exact vhost text and unified diff (rendered by the helper
itself, so the preview cannot differ from what will be written), and the TLS action. The plan
carries a `plan_hash`. `apply` and `remove` recompute the plan and refuse (409) when the hash
differs, so what the user reviewed is what runs. Apply order:

1. DNS record (create/update, only records tagged as ours).
2. If a certificate is needed: HTTP-only vhost first (reachable immediately, and needed by
   HTTP-01), then certificate issuance and install.
3. Final vhost (TLS: port 80 redirects to https except `/.well-known/acme-challenge/`).
4. Domain marked `active`; every step audited (`domain.apply`, `certificate.issue`, ...). A
   failure marks the domain `error` with a message and audits `domain.apply_failed`; applying
   again converges (all steps are idempotent). All ingress changes in the worker are
   serialized by one lock (`services/ingress_lock.py`).

## Cloudflare DNS

`backend/adapters/cloudflare.py`. Token: the environment secret `CLOUDFLARE_API_TOKEN`
(set it through the secrets endpoints; the worker reads it per call, audited as
`secret.resolve`). Use a token scoped to **Zone:DNS:Edit** for the single zone (plus
**Zone:Zone:Read**, needed to look the zone up by name); nothing else.

- Zone lookup by walking up the hostname (`a.b.example.com` -> `b.example.com` -> `example.com`).
- Records carry the comment `managed-by=jendock domain=<id>`. **Only records with that tag
  are ever updated or deleted.** A record with the same name and a clashing type (same type,
  or CNAME versus anything) that does not carry the tag is reported as `conflict`; apply is
  refused and nothing is changed. A foreign `AAAA` next to our `A` is not a clash.
- Idempotent: an equal record is `noop`; differences become `update`; changing the record type
  replaces only our own old record; losing a creation race converges instead of failing.
- Planning only issues GET requests (that is the dry run); proxied flag supported (`ttl` auto).
- The token travels only in the `Authorization` header: never in URLs, argv, logs or audit.

## Apache vhost and the host helper

Fixed template: `backend/hosthelper/vhost.py` (golden files in `tests/fixtures/vhost/`).
Per domain: a port-80 vhost (ACME alias; proxy when no certificate yet, else 301 to https)
and a port-443 vhost (TLS, `setifempty` security headers: HSTS, nosniff, X-Frame-Options,
Referrer-Policy; websocket upgrade via `RewriteRule ... ws://127.0.0.1:<port>` `[P]`;
`ProxyPass / http://127.0.0.1:<port>/`; `X-Forwarded-Proto`; `Proxy` request header unset).
Required modules: `proxy proxy_http proxy_wstunnel rewrite headers alias ssl`.

### Host helper protocol

JSON lines on a Unix socket (`backend/hosthelper/server.py`), one request per connection,
served **serially**. The kernel-reported peer uid must be in `allowed_uids`. Request
`{"id", "op", "params"}`; response `{"id", "ok", "result"|"error": {"code","message"}}`.

| op | params | effect |
| --- | --- | --- |
| `ping` | - | liveness |
| `vhost.render` | hostname, upstream_port, tls, websocket | dry run: `{proposed, current}` |
| `vhost.get` / `vhost.list` | hostname / - | read managed files |
| `vhost.put` | hostname, upstream_port, tls, websocket | atomic write, configtest, graceful reload, **revert on failure** |
| `vhost.remove` | hostname | remove, configtest, reload, revert on failure |
| `cert.install` | hostname, fullchain_pem, private_key_pem | validate, install (key 0600), test+reload if a TLS vhost uses it, revert on failure |
| `cert.info` / `cert.remove` | hostname | read / remove (refused while a TLS vhost uses it) |
| `challenge.put` / `challenge.remove` | token, content | HTTP-01 file in the webroot |
| `backend.check` | port | TCP connect to `127.0.0.1:port` from the host |
| `apache.configtest` | - | run `apachectl configtest` |

Validation (all inside the helper, none trusted from the caller): hostnames must already be
canonical lowercase ASCII, match the administrator's zone allowlist on a label boundary and
cannot contain `/`, `..`, whitespace, quotes, newlines or `;`; ports must be integers inside the
allowlisted ranges (>= 1024); unknown parameters are rejected (no free-form configuration);
file names are derived from validated hostnames (`sc-<host>.conf`) inside one fixed directory;
an existing file without the `# jendock-managed:` marker is never overwritten or deleted;
the `apachectl` argv comes from the root-owned config, never from a request; certificate
material is parsed (key must match the leaf, SAN must cover the hostname, must be valid now,
no encrypted keys). Writes are atomic (temp file in the same directory, fsync, rename), and a
failing `configtest` or reload restores every touched file (a created file is removed).

## TLS: ACME

`backend/adapters/acme/` is a small RFC 8555 client (ES256 account key in
`<data_dir>/worker-private/acme-account.key`, mode 0600; a fresh P-256 certificate key per
issuance, so renewals rotate the key; the key is handed to the helper and never stored by the
platform). Challenge solvers are pluggable:

- **DNS-01 via Cloudflare (primary, `JENDOCK_INGRESS_ACME_CHALLENGE=dns-01`).** Reasons:
  the platform already holds a zone-scoped Cloudflare token and needs no new privileged
  capability; the challenge needs no host access (so the helper has less to do); it works while
  the hostname is Cloudflare-proxied or the `http -> https` redirect is in place; it is the only
  way to issue wildcards; and issuance does not depend on the vhost or DNS having propagated.
  It costs one more permission on the token (TXT records in the same zone, covered by
  Zone:DNS:Edit). `_acme-challenge` TXT records are tagged and only tagged ones are removed.
- **HTTP-01 via the helper's webroot (`http-01`).** Fully implemented: the helper writes the
  key authorization under `<webroot>/.well-known/acme-challenge/` and the vhost template serves
  that path (also excluded from the https redirect). No wildcards.

Both were run against Pebble (`tests/test_acme_pebble_docker.py`). Configure
`JENDOCK_INGRESS_ACME_EMAIL` and `JENDOCK_INGRESS_ACME_DIRECTORY_URL` (default Let's
Encrypt production; use the staging directory while testing).

**Renewal:** `RenewalScheduler` runs in the worker (hourly by default,
`JENDOCK_INGRESS_RENEWAL_INTERVAL_SECONDS`) and renews every certificate whose expiry is
within 30 days (`..._RENEW_BEFORE_DAYS`); a failed renewal is retried after 6 hours
(`..._RENEW_RETRY_HOURS`) and keeps `last_error`/`last_attempt_at` on the certificate. Renewals
are audited as `system` / `ingress-renewal`. **Expiry monitoring:** `GET /api/ingress/certificates`
and the `certificate` part of each domain report `days_remaining` and a `state`
(`ok`, `renewal_due`, `warning` < 14 days, `critical` < 3 days, `expired`).

## Status checks

`POST .../check` stores, with timestamps: DNS resolves to the server's address (for proxied
records: resolves at all), HTTPS answers with a valid certificate for the hostname (system trust
store, hostname verified) and status < 500, and the backend accepts TCP connections on the
published loopback port (probed by the helper, because the worker's `127.0.0.1` is not the
host's). Wildcard domains are probed through `ingress-check.<zone>`.

## Settings reference (`JENDOCK_INGRESS_*`)

`ENABLED` (worker; default false), `HELPER_SOCKET_PATH`, `PUBLIC_IPV4`, `PUBLIC_IPV6`,
`ALLOW_WILDCARDS` (API and worker), `CLOUDFLARE_API_BASE`, `ACME_DIRECTORY_URL`, `ACME_EMAIL`,
`ACME_CHALLENGE` (`dns-01`|`http-01`), `ACME_CA_BUNDLE` (private CA only),
`ACME_ACCOUNT_KEY_FILE`, `DNS_PROPAGATION_WAIT_SECONDS`, `ACME_POLL_TIMEOUT_SECONDS`,
`RENEW_BEFORE_DAYS`, `RENEW_RETRY_HOURS`, `RENEWAL_INTERVAL_SECONDS`, `CHECK_TIMEOUT_SECONDS`.
`compose.ingress.yml` is the opt-in overlay that wires the helper socket into the worker.

## Not done / known limits

- `published_port` is entered by the user; it is not read from Docker (deployment, Task 7,
  does not exist yet) and nothing verifies that the container actually publishes it.
- No path-prefix routing, no per-domain custom headers, no HTTP-01 for wildcards.
- Cloudflare calls have no retry/backoff on 429 or 5xx (an apply simply fails and can be repeated).
- The renewal pass is sequential and holds the ingress lock while it runs.
