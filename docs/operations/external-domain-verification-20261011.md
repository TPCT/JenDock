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
public verifier together remains to be exercised. Current full definition pins
also reject a legitimate backend replacement; rebinding/owned proof generation
refresh and authority handling for continuous verification after delivery remain
required. Legacy gateway upgrades, automated certificate renewal and production
verification remain open. No production/provider mutation occurred in this pass.
