# Environment gateway runtime — local acceptance

This slice adds one loopback-only Nginx gateway per managed environment. It does not expose public domains or install an Apache/TLS helper.

## Implemented

- Pinned unprivileged image, read-only root, dropped capabilities, exact RAM/CPU limits, and explicit temporary paths.
- Separate environment application networks and dedicated gateway transport network. Application containers retain their no-published-port boundary.
- Strict hostname routes sourced from verified HTTP endpoint metadata; Docker DNS re-resolution follows application IP changes.
- Durable port and capacity intent before Docker effects, exact container/network ownership, no automatic recreation after ambiguous creation outcomes.
- Gateway capacity participates in existing application acceptance, host capacity and environment budget reduction checks.
- Shared read-only infrastructure admission in provisioning, rollout and reconciliation. Unknown network peers fail closed.
- Docker health plus bounded actual HTTP checks are required before readiness; unknown hosts must return 421.

## Evidence and defects found

`tests/live_ingress_runtime.py` creates real synthetic Development and Production applications inside the local deploy-monitor product. The fixture checks different HTTP bodies, hostname rejection, capacity drift, network isolation, idempotent replay, forced backend IP replacement without gateway recreation, stale route pin rejection, stopped backend without wake-up, unchanged sibling/preexisting peers, and exact teardown. Machine evidence lives in `.runtime/ingress-runtime-acceptance.json`.

Real execution exposed a read-only Nginx temporary-directory startup failure. All temporary directories now use /tmp. Review also found a Docker inspect list-shape mistake and a readiness probe pipe-lifetime issue; regression coverage and actual Docker verification cover the corrected paths.

Receiver, worker and MCP image build/import checks are separate from deployment. No production containers were recreated by this slice.

## Still open

- Advance durable route pins atomically with verified application rollout/rollback. Dynamic DNS connectivity after replacement does not establish durable route reconciliation; stale pinned identities currently reject verification.
- Authenticated domain operations, API/MCP adapters and dashboard workflow.
- Root-owned host edge helper, Apache configuration ownership, certificates, renewal and Cloudflare reconciliation.
- Public HTTPS identity acceptance and production activation.

These gaps prevent claiming full ingress or whole-platform completion.

## Final verification

All 524 backend tests passed using `.runtime/mcp-venv/bin/python -m unittest discover -s tests`. Receiver, worker and MCP Docker targets built and passed module-import checks. The final worker contains runtime SHA256 `10158cb57676090944aa61bfc6f26a62bf22e5e9f69486942d82e955e9d5239e`, matching source and the final successful real Docker fixture. The fixture report records unchanged executable hashes and empty leftover-resource arrays after cleanup.
