# Managed domain routing acceptance — 2026-10-11

Scope: the existing local `deploy-monitor` product and temporary, explicitly
owned application/gateway/Apache fixtures. No production changes, live provider
DNS writes, public host exposure, or certificate issuance were performed.

## Verified runtime path

`tests/live_ingress_domain.py` connects the actual managed-service provisioning
and gateway runtime to the dynamic host-grant issuer and installed Apache.
Development and Production serve distinct HTTP response bodies simultaneously.
The issuer derives the allowed loopback port from the verified gateway binding,
rather than accepting an upstream address from a request.

The fixture verifies authority revocation before activation and during Apache
configuration testing. A denied candidate is removed before an unrelated
Apache reload. Restoring authority permits the same staged operation to complete.
An existing sentinel vhost retains its bytes and response. Apache processes and
the Docker fixture resources are cleaned up without replacing existing peers.

The surrounding managed-runtime fixture also covers normal image replacement,
recovery before binding commit, failed-health rollback, changed backend IP with
stable gateway identity, stale-binding rejection, and an unchanged sibling
environment. Its machine-readable results are written to
`.runtime/ingress-runtime-acceptance.json`; the Apache edge evidence is in
`.runtime/ingress-domain-acceptance.json`.

`tests/live_ingress_executor.py` uses the installed Apache executable with an
ephemeral loopback listener, a trusted enabled-site symlink and an included alias
file. Actual `-S` inventory preserves the included wildcard aliases, HTTP
activation succeeds, and the original vhost remains unchanged. A stopped daemon
cannot be started through the reload operation.

## Recovery and transport review

Forward activation always revalidates the original accepted authority and exact
physical gateway identity. File-only compensation before a reload signal uses
the journaled previous bytes, cannot remove foreign changes, and remains possible
after authority revocation. Compensation and restoration have separate durable
phases so recovery does not repeat an uncertain reload signal.

`HostService` assembles one adapter and its matching issuer and wires the
authorization callback automatically. The bounded Unix transport exposes grant
issuance only for that assembly. Real socket tests exercise issuance, staging,
activation, revocation, peer rejection and sanitized failures.

The facade rejects missing authorization capabilities and refuses every forwarded
operation if its wired callback is subsequently changed. It cannot silently fall
back to static-grant mode.

## Public reads and verification

Authenticated console HTTP actions `domain-options`, `domain-list`, `domain-get`
and `domain-operation`, and the equivalent four MCP tools, delegate to the same
scoped ingress reads. They require an explicit project/environment. `domains:read`
is opt-in and requires bounded environment grants; existing credentials are not
expanded. No public domain mutation tool is advertised by this checkpoint.

Fresh validation after the coordinated changes: **671 backend tests**, including
authenticated loopback HTTP and official MCP SDK domain reads, and **66 frontend
tests** passed. The frontend production build passed, with its existing bundle-size
advisory. The combined managed-domain/runtime fixture and installed Apache executor
fixture completed with exit code 0 and verified cleanup.
All three Docker targets (`receiver`, `worker`, `mcp`) rebuilt successfully;
isolated import checks verified the packaged ingress and credential module hashes
against the working source. These image builds do not constitute runtime rollout.

## Remaining delivery requirements

- The host service is an assembly capability; a configured and installed daemon
  with its production policy/socket and current credential resolver is still
  required.
- Scoped domain reads and their evidence are not execution. Gateway, Apache and
  DNS effects still need the leased worker orchestration and dashboard flow.
- Sequential addition of another hostname to an existing gateway requires the
  revisioned configuration implementation described in the ingress plan.
- Certificate issuance/renewal, external origin proof and current TLS evidence
  are absent. No result in this checkpoint is HTTPS Ready.
- Live Cloudflare, the public domain and production rollout remain unverified.

Apache source expansion permits only configured trusted roots, bounded literal
and filename-wildcard includes, and validated source symlinks. Directory includes,
wildcard directory components and dynamic/conditional aliases remain rejected;
production setup must explicitly inventory and configure the actual source roots.
