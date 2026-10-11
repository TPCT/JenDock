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

There were two initial certificate commands, two renewal commands, four Apache reloads total and two Nginx route-addition reloads. Successful renewal no-op is proven; actual issuance and activation of a **new renewed generation**, expired-certificate recovery, live Cloudflare/public DNS and production installation were not covered by that no-op run.

Final executed hashes:

- `tests/live_domain_https.py`: `4ba4dbfcd88e68d4be851b0c6d5e98c7d56d3756e556d881f2bbf905bc0452ad`
- `ingress_certificates.py`: `c94169c1fc8a917ada12fdb3c3c5e5421680a3cc83b377fea1b5e555d719ea09`
- `ingress_host_tls.py`: `1f7a71932d86f078d2eed90886e6307954a2252b8ab6fb2ae8d5c00d072bd0ba`
- `domain_execution.py`: `78e595ad37a8a79f82932f77c1ab683ae904e368361f43dfaf2d11c5a8c996e8`

The final enclosing run (session `94199`, log `/tmp/deploy-monitor-domain-https-verified.log`) exited 0. Managed delivery, pre-CAS recovery, failed-health rollback, proof refresh, dynamic backend replacement, and stopped-backend no-wake checks also passed. Final exact cleanup, sibling preservation and peer inventory checks were true; ACME and source-inspection labels had no leftover containers. All captured source hashes were rechecked after the complete run.

## Changed-generation scheduler acceptance mode

`python3 tests/live_domain_https.py --changed-renewal` retains the initial non-due renewal checks, then tests issuance and activation of genuinely new generations. The local CA uses a two-day default certificate profile so the production scheduler's unchanged 30-day window selects both owned routes. Pebble v2.10.1 supports this through `profiles.default.validityPeriod`; see its [pinned configuration](https://raw.githubusercontent.com/letsencrypt/pebble/v2.10.1/test/config/pebble-config.json) and [configuration reader](https://raw.githubusercontent.com/letsencrypt/pebble/v2.10.1/cmd/pebble/main.go).

Only the trusted fixture executor appends `--force-renewal`, and only during this later scenario against the exact local CA. The executor asserts the production manager never supplied that flag. Neither product requests nor administrator production configuration gain a force option. Actual Certbot still obtains a new certificate from Pebble, and all certificate, authorization, challenge and TLS checks remain enabled.

The test invokes actual `domain_renewal.process_once` scheduling and phase transitions. It checks that old certificate metadata and immutable generation files remain intact before verified activation; the sibling's served TLS fingerprint stays unchanged until its own activation. An observation immediately after the host activation RPC verifies that the worker has not yet promoted metadata and does not expose Ready. After promotion, the route must wait for external verification and regain Ready through the actual HTTPS proof verifier. Initial issuance, non-due no-op, and changed-generation results are recorded separately.

The changed-generation edge scenario passed against the frozen host and certificate implementation; its results are recorded below.

The first changed-generation run exposed Certbot's own noninteractive renewal jitter: the pinned client selected a roughly 430-second delay, exceeding the fixture's 90-second execution bound. The attempt remained queued with `renewal_unverified`; the active certificate was not promoted. The exact Certbot container and all fixture resources were removed before the host implementation was changed. The product scheduler must disable this second scheduling layer with a fixed trusted argv option, then rerun the actual changed-generation scenario.

## Changed-generation result

The actual scheduler completed issuance, staging and activation for both environment domains. Each served certificate fingerprint and immutable generation changed. Both old generations remained present with identical file hashes. Until each route's own activation, its served fingerprint matched its original committed metadata. Immediately after each real activation RPC, the old certificate still occupied the worker journal and Ready was false; promotion followed exact verification. Both routes then regained Ready through actual external HTTPS proof verification. There were six Apache reloads total: four for initial HTTP/TLS, then exactly two for renewal activation.

- `second-development.fixture.test`: generation `e9c416c2b3cfc0c5f98754e0e9965edd00481dfcbeed8b76188a850173eff7d1` → `3a94e7b5687cd157553376524d2bb216b9075a9e56df7487434abf8ee9afb5c3`; new fingerprint `cb107c73ce5374ac2a41ff5cd1c87a134081c3dbab4fec8ccbf06c58eb895443`.
- `second-production.fixture.test`: generation `5603ea12560042cb08b8e3fd3520ae4173e58ccb62d8b3981853168156acbddb` → `272fa1cb7eeace8612d2142cbe88cdc69cc05fbc15570749e578c08f0df9a9ba`; new fingerprint `033609fa3b8fa88bd04382598ad0b950bd38f1a70c60ef6b45a211f02ee36c08`.

Executed changed-generation hashes:

- `tests/live_domain_https.py`: `86692f130ff42c74a08541d0d1f77c502a70a1c7065a4e2d32c00acc6e0bd54e`
- `domain_renewal.py`: `df980988467d55a7c4843ee6741d5e67f1f5af7e0eaac20595c4b70326ae5665`
- `ingress_host_tls.py`: `9dcf618e3ad8145dd5fd524cba68b969aaddeeea6ed1b39b464c7a0381af09ec`
- `ingress_certificates.py`: `aa718846e034be63b61b0880ba8e03bdbd6ca231e80bfa05cd0b68ca9531cb0d`

This proves real issuance and activation of new renewed generations in the local CA fixture. Public DNS/Cloudflare, public CA deployment, expired-certificate recovery, and production installation remain separate boundaries.

Final changed-generation enclosing run `48491` exited 0 (`/tmp/deploy-monitor-domain-https-changed-final.log`). All outer runtime scenarios passed; exact cleanup, peer preservation and sibling-environment checks were true. No ACME fixture containers remained. Captured source hashes matched current files after the complete run.
