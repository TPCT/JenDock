# Combined local domain HTTPS acceptance

`python3 tests/live_domain_https.py` exercises the domain workflow using the existing `deploy-monitor` Compose project and disposable services with exact ownership labels. Run only when other acceptance fixtures are stopped and product source is frozen. It reuses `live_ingress_runtime.main` for managed applications, environment-private networks, Nginx gateways, peer inventory and exact teardown.

The test submits domain previews and idempotent creation through the authenticated console HTTP API. A worker invokes the actual domain execution state machine. The host helper uses a real Unix socket, current-principal grant issuer, Apache executor, HTTP adapter, certificate manager and TLS adapter. Pebble and Certbot use the pinned images in `live_ingress_tls.py`; certificate validation performs genuine HTTP-01 requests through the generated Apache challenge route. Application certificates come from Pebble, not the transport CA.

External proof verification uses `ingress_external.verify` with its explicit trusted fixture resolver and transport to reach the ephemeral loopback HTTPS listener. It retains normal certificate-chain validation, hostname verification, TLS SNI and the bounded HTTP parser. Proof bodies come from the actual hostname-specific Nginx endpoint. The test does not replace the verifier with synthetic success.

Cloudflare API responses and records are synthetic. Ready in this fixture establishes that the complete local orchestration and evidence chain agree; it does not establish public DNS propagation, Cloudflare behavior, public CA issuance or Internet reachability. The test records these limits separately from local TLS and Ready evidence.

Expected evidence is written to `.runtime/domain-https-acceptance.json`, with the outer runtime report in `.runtime/ingress-runtime-acceptance.json`. The combined report captures source hashes at module load. Lost gateway and TLS activation responses must recover without a second reload, two environment domains must serve their own application identities, the foreign Apache sentinel must remain unchanged, and all disposable container/network IDs must be removed while existing product peers remain unchanged.

On 2026-10-11 the combined edge phase reached Ready for both `second-development.fixture.test` and `second-production.fixture.test`. It observed two genuine ACME certificate issuances, two synthetic provider writes, four Apache reloads (HTTP and TLS for each environment) and two Nginx route-addition reloads. Lost replies recovered without repeating the reload. A deliberately wrong proof token was rejected over the same normally validated HTTPS connection. Apache/ACME teardown completed; the enclosing managed runtime acceptance and final peer cleanup are recorded separately in the outer report.

Executed source identities are preserved in the report, including fixture `bbab23706e6bf33991d220c43ba8566950f6a495f9de758f7edeb8b8a131a483`, certificate manager `eceb826d4bfdb8e746859c0c7b2446717712b38b06525cabf2cf40ed7708198a`, and TLS adapter `1c113e46308e28347cdd44815848c4e3c32652d5b25e13c6154486f476c00084`. Source stability was checked after the edge flow. Certificate renewal and production installation are outside this initial issuance acceptance.

The first real renewal extension uncovered a parser compatibility failure: pinned Certbot returned exit 0 with a non-due no-op, but added its `[acme_renewal_info]` section and the manager rejected the lineage. Safe diagnostic evidence is in `.runtime/domain-https-renewal-diagnostic.json`. This failed run cleaned up all fixtures. The final rerun against the narrow parser fix passed the combined edge and renewal checks described below.

## Final renewal no-op acceptance

The final combined edge run verified both environment domains as Ready, then invoked genuine Certbot `renew` once for each freshly issued, non-due certificate. Both commands exited 0 and reported that no renewals were attempted. The emitted `[acme_renewal_info]` section contained exactly `ari_retry_after`. Both certificate generations remained unchanged. Authorized reconciliation before submission returned absent without running a command. Reconciliation and replay after completion preserved SANs, generation, fingerprint, expiry and operation identity, with no additional Certbot command or Apache reload; fresh observation timestamps were permitted.

There were two initial certificate commands, two renewal commands, four Apache reloads total and two Nginx route-addition reloads. Successful renewal no-op is proven; actual issuance and activation of a **new renewed generation**, expired-certificate recovery, live Cloudflare/public DNS and production installation remain separate acceptance work.

Final executed hashes:

- `tests/live_domain_https.py`: `4ba4dbfcd88e68d4be851b0c6d5e98c7d56d3756e556d881f2bbf905bc0452ad`
- `ingress_certificates.py`: `c94169c1fc8a917ada12fdb3c3c5e5421680a3cc83b377fea1b5e555d719ea09`
- `ingress_host_tls.py`: `1f7a71932d86f078d2eed90886e6307954a2252b8ab6fb2ae8d5c00d072bd0ba`
- `domain_execution.py`: `78e595ad37a8a79f82932f77c1ab683ae904e368361f43dfaf2d11c5a8c996e8`

The final enclosing run (session `94199`, log `/tmp/deploy-monitor-domain-https-verified.log`) exited 0. Managed delivery, pre-CAS recovery, failed-health rollback, proof refresh, dynamic backend replacement, and stopped-backend no-wake checks also passed. Final exact cleanup, sibling preservation and peer inventory checks were true; ACME and source-inspection labels had no leftover containers. All captured source hashes were rechecked after the complete run.
