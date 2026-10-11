# Domain configuration intent foundation

`ingress.py` provides scoped preview and immutable acceptance without DNS, Docker, Apache, or certificate side effects. This is not yet an exposed dashboard/MCP provisioning flow.

Requests bind an exact managed service, normalized hostname, verified Cloudflare zone/account and administrator-configured origin. Trusted origin inputs are INGRESS_ORIGIN_IPV4, INGRESS_ORIGIN_IPV6 and INGRESS_ORIGIN_HOSTNAME; request targets must match the corresponding configured identity. Callers cannot supply URLs, proxy directives or arbitrary upstreams.

Preview captures service definition/container/artifact identities, HTTP endpoint, project/environment/source/resource revisions, integration metadata digest and origin digest. Zone metadata can change without integration revision change, so the metadata digest is part of the comparison. Acceptance repeats authorization and evidence validation transactionally, reserves the hostname globally, and supports same-payload request replay. Immutable definitions cannot be updated or replaced.

A persisted running binding is historical evidence, not present health. Old valid bindings remain selectable with runtime_verification pending; stopped/missing/incoherent bindings reject. The future worker must freshly verify exact runtime identity and health before effects. Preview and acceptance report DNS/origin/TLS unverified, executor unavailable and ready false. Existing DNS adoption remains unavailable until actual provider preview/ownership reconciliation exists.

## Outstanding integration

Domain scope issuance, authenticated API/MCP adapters, dashboard, live provider record preview/adoption, worker orchestration, host edge and TLS are not delivered by this foundation. No production activation or provider write occurs here.

## Verification checkpoint

The full backend suite passes 555 tests, including 13 domain tests across implementation and independent-review files. Independent cases cover replay after source-environment permission loss, strict revision pin types, same-revision zone metadata changes without writes, Development/Production hostname collision and credential revocation during the acceptance transaction. These tests prove DB/auth/intent contracts, not live provider or runtime provisioning.

Final frozen code verification: 555 backend tests passed after review hardening. All three Docker targets built and passed imports; the worker ingress.py hash equals source `94883dca48a4fc7e71e105be8842364855e5bf3c994b00e3a27462d40789b1e5`. No frontend, provider or production acceptance is claimed.
