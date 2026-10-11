# Managed ingress TLS implementation specification

Date: 2026-10-11. Design for the already authorized ingress Task 3/4 scope; no new approval gate. Implement in the existing checkout. This document records design, not delivered TLS capability or production acceptance.

## Outcome and boundaries

A domain progresses automatically from verified owned DNS and HTTP routing to an owned certificate, verified origin HTTPS, and verified external HTTPS reaching the selected application. Both environments and unrelated adopted Apache sites continue serving throughout. Issuance, renewal, routing, and readiness are separate durable facts. Failure retains working DNS/HTTP and the previous valid HTTPS generation. No stopped application or Apache process is started implicitly.

Use Certbot's webroot authenticator with `certonly`; never its Apache installer, standalone listener, caller plugins, hooks, shell strings or caller certificate paths. An administrator supplies the exact executable, ACME directory, account/contact configuration, dedicated config/work/log directories, challenge root, TLS listeners, trust roots, timeout and renewal policy. Prefer a dedicated product-owned Certbot namespace; never run a global renew over adopted lineages. Reusing an existing host account/lineage requires explicit verified ownership during installation, not inference from matching hostname.

## Exact compatibility changes

- `ingress_host.py:_policy` currently accepts exactly four HTTP-only keys. Introduce a versioned administrative schema and compatibility reader for HTTP-only installations; TLS absent means capability unavailable, not startup failure. Extend daemon's closed schema deliberately.
- `_config` currently proxies every HTTP path and stamps a route header; it has no challenge exclusion. Add a fixed `/.well-known/acme-challenge/` Alias with ProxyPass exclusion before the catch-all, directory access limited to the owned challenge directory, no directory listing or executable handlers. Validate exact generated challenge bytes through the intended hostname before issuance. Never redirect this path.
- `ApacheExecutor` accepts one loopback HTTP observation listener. Add separately configured loopback TLS observation address/port, verified SNI and CA trust. Binding listeners remain administrator configured (may be public); observation stays loopback. Host setup must verify mod_ssl and required modules without enabling them implicitly.
- `_headers` checks a route marker even on 5xx. That is useful config evidence but insufficient readiness. Add bounded HTTP/TLS evidence returning status, unique route/gateway identity and certificate fingerprint; keep config observation separate from backend health.
- `_safe` rejects all symlinks, but Certbot normally uses live-to-archive symlinks. Do not relax this generic helper. Add a dedicated certificate resolver for the one journal-owned lineage; validate each component and bounded link target under its archive directory. Copy validated bytes into immutable product-owned certificate generations used by Apache, so Certbot moving its live links cannot silently alter an active configuration.
- Host journals currently hold one HTTP candidate/prior config and grant-derived token. TLS generation needs a distinct config token including certificate fingerprint, listener and configuration digest, plus previous certificate generation. Preserve old HTTP journal compatibility and compensation behavior.
- `domain_execution.py` explicitly terminates at `awaiting-tls`. Replace that terminal boundary with durable certificate, TLS stage/activate, external verification and ready phases. Existing awaiting-tls records resume the same operation without recreating DNS/gateway/grants.
- Add RPC methods only through the mandatory authorized `HostService` facade, explicit transport allowlist, and daemon assembly. Existing authenticated peer/server UID checks and original-operation principal revalidation remain mandatory.

## Certificate ownership and bounded executor

New `ingress_certificates.py` owns the journal, inspection and Certbot executor; keep Apache process mutation in `ingress_apache_executor.py`.

1. Derive certificate name from immutable route ID (`deploy-monitor-<route-id>`); accept exactly one normalized non-wildcard hostname from the current trusted grant. Request cannot specify extra SANs, names, account, server, path, plugin or arguments.
2. Under the existing host mutation lock, revalidate original operation authority, route/DNS ownership and current gateway/backend health. Persist issuance intent containing route/operation, hostname, account/server identity, exact lineage name, previous fingerprint (or absent), policy digest, attempt and start time before invoking Certbot.
3. Build argv from validated administrative fields: fixed binary, `certonly --non-interactive --webroot --webroot-path <owned-root> --cert-name <derived-name> -d <grant-host> --config-dir ... --work-dir ... --logs-dir ... --server ...`, fixed explicit config file and account/contact policy. Terms acceptance is part of administrator installation configuration. No inherited CLI config, hooks, environment plugins or default global directories. Reject hooks in renewal config; suppress hook-directory execution using the supported pinned Certbot option. Inventory and pin the tested Certbot version/options during packaging.
4. Execute without shell, stdin closed, sanitized environment, bounded merged output (64 KiB), hard deadline and process-group termination/reaping. Never persist raw Certbot output or key material. A timeout is an uncertain effect, not evidence that no certificate was issued.
5. Inspect files after every outcome and before retry. Require exact owned lineage, trusted uid/modes/ancestors, bounded regular files, no hardlinks, SAN set exactly the normalized hostname, valid time window, supported key, public key matching private key, chain trusted by configured CA, and expected server/account renewal metadata. Private keys are owner-read-only; never expose bytes/paths in API/SSE. Do not overwrite or adopt a foreign preexisting lineage.
6. Copy verified certificate/chain/key to an immutable generation directory with atomic writes/fsync. Journal fingerprint, serial, SAN, not-before/not-after, CA and lineage identity, and generation digest. Public evidence contains only safe certificate metadata.
7. Restart/lost reply: inspect exact intended lineage and journal first. A valid resulting certificate completes issuance without another command. If absence/failure is uncertain, remain reconciliation-required; do not issue repeatedly or force-renew. Permit a new bounded attempt only after the prior process is proven ended and inspection establishes no acceptable completion, with backoff recorded.

## HTTPS activation and rollback

Add `request_certificate`, `stage_tls`, `activate_tls`, `verify_tls` to the host facade. Requests contain only route_id, original operation_id, expected current config digest and (for stage/activation) journaled certificate generation/digest. `inspect`/`recover` return protocol/phase-specific sanitized evidence. Client lost replies never replay a mutation automatically.

Generate an owned HTTPS vhost using the fixed listener and immutable generation paths; sanitize forwarded headers and set X-Forwarded-Proto=https, preserve Host, retain bounded proxy timeouts. Collision checking includes exact/wildcard/alias inventory on both HTTP and HTTPS, allowing only this route's journal-owned files. Do not edit foreign sites. HTTP continues proxying during initial TLS activation.

Journal candidate, previous bytes/digests/certificate generation and operation before install. Hold the existing lock across install/configtest/reload/restore; recheck authority before each forward effect. Use exact running Apache identity and pidfd SIGUSR1, never apachectl start. Known pre-signal failures restore only journal-owned previous bytes even if authority was revoked. Signal intent is durable before signal. After ambiguous reload, inspect TLS with hostname SNI, trusted chain, expected fingerprint and config token; no blind second reload or restoration signal. A reachable different generation is distinct from an unknown handshake/timeout.

After confirmed TLS activation, add HTTP-to-HTTPS redirect as a separate journaled config transition, excluding challenge paths. Verify redirect target, HTTPS identity and all affected owned routes. Redirect failure cannot invalidate a working TLS generation. Rollback to previous TLS requires retaining its immutable key/cert bytes and verifying the actual previous served fingerprint/token. Initial-install compensation restores HTTP-only behavior. Cleanup never deletes active/previous/pending certificate generations or foreign lineages.

## Orchestration, renewal and current readiness

Persist certificate issuance intent/verified, TLS staged/reload-intent/verified, external verification and their timestamps separately in domain execution. DNS ownership and current origin health are revalidated before issuance. The gateway challenge must prove routing through the selected route/backend generation; an Apache-added header alone or Cloudflare edge certificate alone is insufficient. Use a route-specific bounded challenge endpoint in the gateway plus fresh backend identity/health verification; strip upstream spoofed identity headers.

External verification uses exact normalized public hostname and HTTPS with normal hostname/chain verification, bounded connect/read/body/time, no cross-host redirects and no caller URL. Check intended route challenge, current gateway/backend evidence and DNS record ownership. Proxied Cloudflare mode must verify both origin TLS directly and the external path; its edge leaf fingerprint need not equal the origin leaf. DNS caches or unavailable external reachability leave external evidence pending, never Ready.

Ready requires fresh DNS ownership, healthy exact gateway/backend, valid unexpired served origin certificate, verified external hostname/challenge and no ambiguous pending mutation. Store `observed_at` and expiry; public projection downgrades stale evidence immediately even before the next worker sweep. Stop/drift/revocation/expiry must not wake or repair applications implicitly.

A bounded worker maintenance pass schedules due renewals per owned lineage with leases, backoff and the same original authority checks; no extra system-wide Certbot timer or command hook. Use scoped `renew --cert-name <derived-name>` with the fixed directories/server policy. Inspect fingerprint and validity after completion: exit zero can mean renewal was unnecessary. A changed cert goes through immutable generation copy, full configtest and journaled TLS activation. Failed renewal preserves the serving valid generation and reports warning/due/last-attempt; expiry removes readiness. Revoked principals do not silently gain an administrative renewal bypass—report blocked renewal unless an explicit persisted lifecycle service authority is designed and authorized separately.

UI keeps DNS / Origin / TLS / external readiness separate. Resume advances the same operation. Display certificate expiry, last verified and renewal error; show Open application only for fresh verified readiness. No manual approval step for normal issuance or renewal.

## Acceptance and implementation sequence

1. Certificate journal/executor unit tests: closed requests, traversal/link escape/hardlink/key permissions, SAN/key mismatch, foreign lineage, configured hook rejection, bounded timeout, lost successful reply and restart inspection. Then HTTP challenge and TLS config generation tests.
2. Real local Apache + gateway + applications + local ACME server (Pebble or equivalent) and actual pinned Certbot. Use local CA trust for ACME API and HTTPS verification; never disable certificate validation. Configure CA validation DNS/ports to reach real HTTP challenge routing, with its challenge checks enabled. No self-signed-copy shortcut as issuance proof.
3. Add the fixture CA as a disposable test service under the actual `deploy-monitor` Compose project; exact fixture labels/IDs/cleanup, no separately named product copy. Ephemeral local Apache is acceptable as in existing host fixture. Root owns running fixtures; preserve sentinel adopted HTTP/TLS sites and both application environments.
4. Prove initial issuance, SAN/key and chain checks, SNI HTTPS body/challenge identity, HTTP challenge availability after redirect, sequential second hostname without disturbing first, and simultaneous environments. Exercise invalid config, revocation before effects, crash after issuance, lost reload reply, crash before DB evidence commit, failed health and stop/no-wake. Verify reload count and no duplicate issuance on recovery.
5. Force renewal only through fixture-only trusted policy against local CA; verify changed certificate served, old generation retained, unrelated route unchanged. Test expired certificate, wrong SNI/trust, upstream spoofed header, stale readiness, unreachable CA and successful issuance followed by lost reply. Record cert fingerprints, phase timestamps and exact cleanup.
6. Browser desktop/mobile setup/resume/renewal-state evidence and authenticated dashboard/MCP parity. Production deployment remains a separately evidenced installation/activation using the existing authorization; local ACME does not prove public CA/DNS/provider acceptance.

References: [Certbot command reference](https://eff-certbot.readthedocs.io/en/stable/man/certbot.html) for fixed certonly/webroot/lineage/directory options; [Certbot user guide](https://eff-certbot.readthedocs.io/en/stable/using.html) for renewal semantics and live/archive layout. Pin and test the installed version before selecting final argv. This plan relies on post-command certificate inspection rather than renewal exit code or Certbot hooks as proof of Apache activation.

## Implementation checkpoint — 2026-10-11

Implemented certificate ownership/immutable generations, fixed bounded Certbot
execution, exact challenge proof, separately journaled TLS stage/activation and
observation recovery, authorized Unix RPC dispatch, schema-2 host assembly, and
durable domain-worker phases through origin verification. Existing HTTP-only
capabilities remain supported. An awaiting-TLS operation can resume its original
identity once TLS is installed. Origin verification currently terminates at
`awaiting-external`, with public `ready=false`; the UI explains that boundary.

Independent review found and fixed missing socket dispatch, replaceable TLS
facade capabilities, renewal overlap after an unknown process exit, expired
owned certificate renewal, and saved webroot drift. Actual local Certbot/Pebble
and Apache acceptance verifies initial issuance, trusted SNI application response,
and lost-reload-reply recovery with one signal. See the fixture evidence document.

Still required to finish the specification: external route/backend challenge and
freshness projection; HTTP-to-HTTPS redirect transition; owned legacy HTTP
challenge-config upgrade; host/worker renewal scheduling and explicit uncertain
process recovery; actual two-environment and full domain-worker acceptance;
production compatibility, rollout, and public-provider verification. This
checkpoint does not establish those outcomes or complete the platform goal.

### External verification follow-through

A bounded external verifier, schema-3 route proof endpoint, read-only worker
checks, automatic 30-second rechecks, 60-second Ready expiry and UI expiry gate
are implemented. Real Nginx hostname proof/addition acceptance passed. See
`docs/operations/external-domain-verification-20261011.md` for evidence and the
remaining combined HTTPS/worker acceptance, legacy upgrade and post-delivery
proof-generation/authority gaps. These do not replace the remaining renewal,
redirect, two-environment and production requirements above.
