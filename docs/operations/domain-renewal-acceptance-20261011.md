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

Validation on 2026-10-11: 911 backend tests and 73 frontend tests passed. Receiver,
worker and MCP review images built successfully; each passed isolated imports
with networking disabled. The final combined real ACME renewal fixture is tracked
separately in `domain-https-local-acceptance.md` and its runtime report.

This is local evidence only. Real changed-generation renewal, renewal-specific
browser presentation, HTTP redirect transitions, legacy gateway upgrades, and
production/public-provider acceptance remain open. This checkpoint does not
complete the full delivery-platform objective. An interrupted host intent before
the certificate-manager effect remains an explicit reconciliation state rather
than an assumption that issuance failed; unattended recovery for that boundary
still needs exact manager-attempt evidence and acceptance.

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
