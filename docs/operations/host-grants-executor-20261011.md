# Dynamic host grants and Apache executor checkpoint

The host grant issuer derives hostname/zone/port from persisted accepted domain,
current route evidence, verified gateway journal and matching bound capacity/port
reservations. Requests contain only route ID, operation ID and expected grant
revision. Current principal/repository/environment/source authority is rechecked.
A host-owned write-ahead journal and atomic policy publication handle lost replies
without replacing unrelated or foreign grants. Existing activations cannot be
silently repointed.

DockerGatewayVerifier performs strong Docker/backend verification, actual bounded
route-health and default-host rejection probes, then strong verification again.
Changed runtime identity or route evidence rejects issuance. Independent review
found no remaining blocker in this contract after its routed-HTTP check was added.
Twelve grant/verifier tests pass; this is not yet real gateway-to-edge acceptance.

ApacheExecutor uses fixed administrator configuration, bounded argv-only syntax
and inventory commands, exact /proc executable/argv/UID checks and pidfd SIGUSR1.
It never starts a stopped daemon. Direct bounded loopback probes verify route
headers; missing or invalid observations remain unknown.

Actual installed Apache acceptance exposed two gaps in initial synthetic tests:
variable-width -S output spacing, and omitted aliases when only one vhost exists.
Both were corrected. The executor supplements each reported source file/line with
literal ServerName/ServerAlias values. Dynamic includes, macro/conditional blocks,
variables and continuations inside vhosts fail closed: such configurations need
an expanded trustworthy inventory adapter before production support, not silent
claims that the inventory is complete. Source reads and ownership are bounded.

## Evidence

- Full backend discovery: 628 tests passed.
- Eleven focused executor tests cover parser shapes, literal alias extraction,
  bounded subprocess/HTTP handling and wrong-process signal refusal.
- tests/live_ingress_executor.py passed with a separate ephemeral loopback Apache
  process using /usr/sbin/apache2, a private temporary config and synthetic backend.
  It verified real explicit/wildcard aliases, PID identity, HTTP route activation,
  sentinel preservation, refusal to reload/start a stopped daemon, and exact
  preservation of preexisting Apache PIDs after cleanup.
- .runtime/ingress-executor-acceptance.json contains the local result. No production
  Apache configuration, process, provider record or certificate was changed.

## Remaining end-to-end work

Real dynamic grant + managed Docker gateway + Apache acceptance is still required.
The authenticated helper daemon must compose these capabilities and resolve current
principals, rather than exposing the trusted callbacks as request inputs. Sequential
addition of a second domain needs a revisioned gateway reconfiguration journal;
current immutable initial gateway configuration cannot be overwritten. Domain
options/read/status, shared worker leases/phases, API/MCP/UI scopes, ACME issuance
and renewal, and production public TLS acceptance remain open.
