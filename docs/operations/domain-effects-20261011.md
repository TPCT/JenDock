# Domain effects and authenticated host HTTP checkpoint

## Delivered contracts

`cloudflare_dns.py` performs exact-name DNS observation, explicitly authorized
adoption, and owned record creation/update against a fixed HTTPS Cloudflare API
origin. Preview excludes record contents/comments. Before a provider write, it
persists a random ownership marker and immutable operation fingerprint. Lost
responses reconcile the exact record identity, marker and expected payload;
replays never blindly create another record. Zone/account, integration, origin
and principal evidence are rechecked through the ownership commit. An identical
foreign record is not adopted implicitly. DNS verification does not imply origin
or TLS readiness.

`ingress_host.py` stages only administrator-granted hostname/loopback-port routes.
The helper validates filesystem ancestry, ownership, hard links and symlinks,
serializes configtest/reload/restore, checks collisions against Apache inventory,
and verifies a served route identity after activation. Restoration persists and
observes the previous identity; an unknown result remains unresolved and does not
trigger another reload. First-install restoration requires explicit confirmed
absence from the executor. A stopped Apache is never implicitly started.

`ingress_host_transport.py` authenticates exact peer UIDs with SO_PEERCRED in both
directions. The client authenticates before transmitting. The transport has a
closed five-method contract, bounded length-prefixed JSON, restrictive socket
permissions, finite I/O deadlines, fixed sanitized errors and no automatic retry.
The authenticated transport is not an arbitrary host shell.

## Verification

Final backend discovery: **605 tests passed**. Includes synthetic provider
contracts, independent DNS review tests, filesystem/recovery regressions and
14 real Unix-socket transport tests. Independent review found writable ancestor
and unobserved restoration issues; both were reproduced before correction.

`tests/live_ingress_apache.py` passed against a real cached Apache Docker image,
a real loopback HTTP backend, and the authenticated Unix transport. It proves:

- staged HTTP route activation and observed identity;
- replay without a second reload;
- real Apache syntax rejection preserving the prior runtime;
- lost successful activation reply reconciled without another reload;
- failed candidate reload followed by prior-runtime restoration;
- lost restoration reply reconciled without another reload;
- an unchanged, still-serving sentinel site;
- exact fixture cleanup and preservation of preexisting product containers.

The fixture uses a private temporary directory beneath root-owned sticky /tmp,
because the workspace's writable ancestor chain is intentionally rejected by the
host security contract. Compose ownership labels remain deploy-monitor.

All receiver, worker and MCP Docker targets built and imported successfully.
Cloudflare module SHA256 inside all three images equals source:
`df3f8a185ac9b84c84133556f3fbf8f7fee9246e802ead4e836b23a2fafb5149`.
The build initially detected its missing .dockerignore allowlist entry; this was
fixed and all targets rebuilt successfully. Host helper modules remain standalone
source; no privileged daemon has been installed or exposed.

## Remaining product work

This is not full domain delivery. Live Cloudflare writes, dynamic verified host
grants, the production Apache executor/daemon installation, ACME issuance and
renewal, worker phase orchestration, authenticated dashboard/MCP domain controls,
and end-to-end public TLS acceptance remain open. Static administrative grants
are a bootstrap trust contract, not the final dynamic project workflow.
No production or live provider mutation occurred in this checkpoint.

Local evidence: .runtime/ingress-apache-acceptance.json,
/tmp/monitor-full-suite.log and /tmp/monitor-*-build.log. These runtime artifacts
are local verification data rather than committed production evidence.
