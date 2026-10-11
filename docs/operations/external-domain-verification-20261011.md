# External domain verification checkpoint

The worker now has a separate, read-only external verification phase. It requires
an existing verified DNS intent and ownership record, observes origin TLS, checks
the exact gateway/backend and application HTTP health, requests the fixed public
HTTPS proof endpoint, and rechecks origin and backend identities before committing.
It never provisions, starts an application, changes DNS, or reloads Apache as a
verification retry. Lost/failed probes retry after 30 seconds.

New schema-3 Nginx gateways expose a hostname-specific proof bound to project,
environment, service, backend container, definition and HTTP endpoint. Old schema
1/2 configurations retain their existing byte representation; they do not gain
proof support implicitly. The public probe validates DNS destinations as public,
pins the numeric connection address, uses normal CA/SNI verification, refuses
redirects, and bounds resolution, headers, body and total time.

Ready is projected from a bundle with at most 60 seconds of validity and current
local ownership/binding pointers. Pending route changes, changed backend/gateway,
stopped bindings, revoked authority, expiry or invalid evidence remove readiness.
A successful background read-only refresh retains the preceding unexpired bundle
while checking; a failed check removes it. Route, list and operation projections
copy the same ready/external fields. The React link gate checks public expiry and
updates its label without waiting for a server refresh.

## Evidence and remaining acceptance

Real Docker/Nginx fixture `tests/live_ingress_addition.py` passed schema-3 exact
proof responses, content type/cache policy, unknown-host rejection, distinct host
tokens, preservation through sequential route additions, uncertain reload
recovery, sibling preservation and exact cleanup. The report is
`.runtime/ingress-addition-acceptance.json`. This was an actual HTTP gateway test;
its result is not combined external HTTPS acceptance.

Unit/integration tests cover public-address restrictions, TLS transport/SNI,
response bounds, wrong proofs, journal-preserving read-only retries, concurrent
backend drift, projection expiration and current registry drift, and UI expiry.
A single full Ready workflow through actual ACME, Apache, Nginx, domain worker and
public verifier together remains to be exercised. Committed backend replacement now has an owned proof-generation refresh path and
observation authority that retains the original accepted domain and grant identity.
Initial provisioning still requires exact accepted pins. Legacy gateway upgrades, automated certificate renewal and production
verification remain open. No production/provider mutation occurred in this pass.

## Post-delivery refresh acceptance

The real Docker/Nginx fixture now verifies exact public proof responses after
normal managed completion, pre-CAS recovery, and health rollback to a replacement
container. Each transition performs one reload. The recovery case deliberately
loses the reply after the actual reload and reconciles without another signal.
Unchanged same-environment hostname proofs, the production environment, existing
local product containers, and physical gateway identity remain preserved. Exact
fixture cleanup passed. Evidence: `.runtime/ingress-addition-acceptance.json` and
`/tmp/ingress-proof-refresh-live-final.log` (successful process exit).

The first run reached the later stopped-backend check but its 3-second HTTP
client deadline raced the gateway's 3-second upstream connect deadline. That
single assertion now allows 6 seconds for the actual gateway error response;
it still requires HTTP 502/503/504 and proves the stopped container was not
started. The complete fixture passed on the subsequent run.

This proves the local runtime proof transition, not the combined ACME, Apache,
public DNS/provider, and end-to-end Ready workflow. The worker coordinator and
current-authority paths have separate integration tests. Full combined HTTPS
acceptance, legacy upgrades, certificate renewal, and production rollout remain
open.

The post-delivery coordinator queues in the binding transaction, scans terminal
deliveries after restart, and rechecks each original domain principal after the
old delivery lease has been released. Stable provider/origin/policy/source pins
remain protected. Untouched peers in failed deliveries need exact original
bindings and no rollout journal. Compensated pre-signal failures allow at most
three linked attempts; uncertain reloads keep their original operation identity.
Host-grant validation follows committed observation authority without changing
the original published grant identity.

Final checkpoint: 862 Python tests passed; receiver, worker and MCP images built
and passed isolated imports. The worker coordinator source SHA256 matched the
built image. The current frontend passed its 73 tests and build in the preceding
spacing checkpoint; no frontend files changed in this backend pass. No production
or external provider writes occurred.
