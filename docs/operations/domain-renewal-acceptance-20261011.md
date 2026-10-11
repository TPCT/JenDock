# Owned certificate renewal — local implementation

The worker schedules owned certificates within 30 days of expiry. Development
and Production retain separate domain identities. Renewal keeps the original
principal and credential authority; expiry does not discard ownership, and
ownership does not make an expired certificate Ready.

Attempts persist before host RPC, use leases and bounded retry delays, and retain
the same attempt identity after uncertain delivery. A freshly authorized host
response proving the exact attempt journal absent permits replay of that same
identity. Conflicting or unknown outcomes do not start another issuance. Only a
known-ended failed Certbot command may receive a new attempt after backoff.

A changed certificate must pass immutable-generation validation, TLS staging,
activation and served-certificate verification before the worker promotes it.
External verification is then required again. A non-due Certbot result completes
without replacing or reloading the serving generation. Stage/activation exclude
concurrent external domain execution. Scheduler pages are bounded and advance a
durable cursor; malformed domain records cannot starve other domains, while
storage failures remain errors.

Independent host and worker review found and repaired a real CertificateManager
contract mismatch (SAN list versus nonexistent hostname), crash before RPC
arrival, corrupt-record scheduler interruption, and starvation after the first
100 records. Public TLS projection also rejects nonfinite or nonpositive expiry.

Validation on 2026-10-11: 919 backend tests and 75 frontend tests passed. Receiver,
worker and MCP review images built successfully; each passed isolated imports
with networking disabled. The final combined real ACME renewal fixture is tracked
separately in `domain-https-local-acceptance.md` and its runtime report.

This is local evidence only. HTTP redirect transitions, legacy gateway upgrades,
expired-serving-certificate recovery and production/public-provider acceptance
remain open. This checkpoint does not complete the full delivery-platform goal.
Renewal presentation acceptance is recorded in `domain-renewal-ui-20261011.md`.

Follow-through: a host intent interrupted before the certificate manager starts
can now report exact `not-started` evidence. This requires the unchanged captured
manager attempt, confirmed-ended prior process and exact owned generation bytes.
The same ID can resume only after authority/challenge/active-generation and manager
evidence are rechecked under the host lock. Once the manager writes a new intent,
an uncertain outcome cannot be replayed. Contract tests exercise before/after that
boundary and actual HostTLSAdapter candidate promotion/lost-reply recovery.

The first real renewal fixture failed even though Certbot exited successfully:
the certificate was not due, but Certbot wrote an `acme_renewal_info` section that
the lineage parser rejected. The diagnostic retained only command outcome and
sanitized state, not certificate keys or account contents. This failure is a
required integration regression; the earlier passing unit suite does not prove
real renewal acceptance. A separate review also identified recovery of a later
attempt while a prior completed attempt journal exists. Durable per-attempt
journals now cover this case. Proven-ended failures are terminalized before the
next intent so they do not block a third cycle. Both interruption boundaries and
historical replay are regression-tested. The parser now accepts only the pinned
Certbot ARI section and strict timestamp key; hooks and arbitrary metadata remain
rejected. The corrected combined edge fixture now passes actual Certbot non-due
renewal for both environments: two initial issuances, two renewal commands,
unchanged certificate generations, and no extra command or Apache reload on
same-ID reconciliation/replay. The actual ARI section/key is captured. See
`.runtime/domain-https-acceptance.json`; enclosing runtime cleanup is recorded
separately. This proves host renewal no-op compatibility, not actual changed
generation activation or scheduler-driven due renewal through a public CA.

Real changed-generation testing exposed Certbot's default random renewal delay
exceeding the bounded executor deadline. Renewal argv now uses the pinned official
`--no-random-sleep-on-renew` option; the application scheduler already owns delays.
Initial issuance argv and renewal eligibility are unchanged. A regression verifies
that production argv never adds force-renewal. The real changed-generation fixture
uses a documented test-only executor flag and local short-lived CA profile.

The corrected real changed-generation edge now passes for both environments.
Actual worker renewal phases issue a new local-CA certificate, preserve the old
immutable generation, stage and activate the candidate, and only then promote
the matching certificate journal. An observation during each actual activation
confirmed that the old journal remained authoritative and Ready was false before
promotion. Each new generation then regained Ready through the real HTTPS verifier.
Six Apache reloads were observed: four initial HTTP/TLS activations and two renewal
activations. The test-only force flag is absent from production argv. Final outer
runtime cleanup is separately tracked in the full fixture report.
