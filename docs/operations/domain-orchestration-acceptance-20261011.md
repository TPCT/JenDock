# Domain orchestration acceptance — 2026-10-11

This checkpoint is local, not production rollout or public HTTPS acceptance.

The combined real-runtime fixture (`tests/live_domain_execution.py`) completed
with exit 0. Authenticated console HTTP creates and replays domain operations in
Development and Production. The leased worker adds hostnames to actual Docker
Nginx gateways and calls the real Unix-socket host service, grant issuer, and
Apache executor. DNS uses the actual ownership adapter with an explicitly
synthetic provider transport; no live Cloudflare writes were performed.

Both an accepted gateway reload with a lost reply and an accepted Apache reload
with a lost reply were reconciled via HTTP resume. Exactly two gateway reloads
and two Apache reloads served the two additions; recovery did not repeat them.
Both operations truthfully ended `awaiting-tls`, `ready: false`.

Evidence is retained locally in `.runtime/domain-execution-acceptance.json`,
`.runtime/ingress-runtime-acceptance.json`, and the recorded source hashes.
The runtime acceptance also covered replacement, pre-CAS recovery, health
rollback, changed backend IP, stopped-backend preservation and sibling isolation.
Fixture resources were removed and existing peers remained unchanged.

The full backend review found a regression in the adoption preview: inclusion of
all record content also disclosed unrelated TXT content. The corrected projection
includes target values only for A, AAAA and CNAME records and retains unrelated
record identity/type solely to explain collisions. The provider-failure fixture
flag no longer shadows unittest's `fail()` assertion method.

The host helper remains an opt-in administrative package. Current Apache support
requires the exact verified explicit `-f` startup configuration; default distro
startup relying on shell envvars is not yet supported. Do not install using
assumed argv or claim `--check` establishes runtime readiness.

Open delivery outcomes: certificate issuance, HTTPS activation, renewal,
public application verification and production deployment. Broader platform work
also includes database/persistence/secret onboarding, database inspection,
migration adapters and scheduler/queue operations. This checkpoint does not
close those requirements.

## Integrated review follow-up

The worker now publishes a bounded, timestamped executor observation after
validating its configuration and contacting the read-only host capability.
Receiver/MCP read that observation; an enable flag alone does not enable new
submission. Evidence expires after 90 seconds, host probes are throttled for
30 seconds for unchanged available or unavailable configuration, and the UI
distinguishes disabled, stale, blocked, invalid and unavailable observations.
Existing accepted operation replay and authorized recovery remain accessible;
queuing recovery while disabled does not start the worker or claim progress.

An actual invalid Nginx candidate was rejected before reload. Normal invocation
refused the aborted operation; explicit worker resume verified the old route
head and runtime identity, retained attempt history, and completed exactly one
reload on the existing gateway/port. The route-addition fixture then verified
normal delivery, interrupted recovery and health rollback with exact cleanup.
See `.runtime/ingress-addition-acceptance.json`.

Integrated automated verification: 731 backend tests and 72 frontend tests passed.
Receiver, worker and MCP Docker targets built; isolated import checks succeeded
and SHA-256 comparisons verified the bundled changed backend source against this
checkout. Compose base plus MCP plus ingress overlay passed configuration
validation. These checks do not establish production deployment or TLS readiness.

The final combined fixture was rerun with the availability gate enabled and
completed with exit 0. `.runtime/domain-availability-source-hashes.txt` matched
at the end; exact cleanup and unchanged preexisting peers were reconfirmed.

The local receiver was restarted after fixture cleanup. Browser inspection of
Domains at desktop and 390px mobile verified the real disconnected-worker error,
disabled creation and Retry recovery affordance with no horizontal page overflow.
An empty bordered list under the error was removed. The local captured-preview
installation does not provide connected-worker browser creation acceptance;
that remains separate from the real authenticated HTTP/worker fixture above.
