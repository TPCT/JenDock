# Local TLS acceptance foundations

Current status, 2026-10-11: `tests/live_ingress_tls.py --prepare DIR` prepares configuration without runtime effects; `--run` starts the exact labeled local CA fixtures and Apache, performs real issuance and HTTPS/recovery acceptance, then cleans up. Initial preparation-only notes below are historical; the final checkpoint records current proof.

Architecture: all fixture services use Compose project `deploy-monitor`. Two uniquely named disposable services run Pebble and its DNS-only companion with Linux host networking. All listeners bind loopback ephemeral ports. DNS answers fixture names with 127.0.0.1; Pebble's configured HTTP validation port is the real ephemeral Apache HTTP listener. The DNS companion's HTTP, HTTPS and TLS-ALPN challenge responders are disabled. No challenge answers are injected and `PEBBLE_VA_ALWAYS_VALID` is absent. Apache must actually serve Certbot's webroot tokens. Host-network mode avoids changing host DNS, production Apache or public ports.

During the initial preparation checkpoint, image manifests were read remotely with `docker manifest inspect --verbose`; no pulls or starts occurred during that initial checkpoint. Linux/amd64 pins in the harness correspond to Pebble 2.10.1, pebble-challtestsrv 2.10.1, and Certbot v5.8.0. None was installed in the initial local image inventory. Runtime entrypoints/options still require actual validation when execution is authorized.

Trust is deliberately split. Preparation generates an ephemeral local transport CA and localhost certificate solely for the ACME API/management endpoint. Certbot must trust that root using its fixed runner environment `REQUESTS_CA_BUNDLE`. Application certificates must come from real ACME HTTP-01 orders. After authenticated management startup, `read_issuing_root` fetches Pebble's separate issuing root through verified TLS; application probes use that root, SNI and hostname validation. There is no `--no-verify-ssl`, insecure SSL context, or fabricated application certificate.

Future runner integration: use the pinned Certbot image under the same Compose project, fixture UID, host network, trusted transport CA and exact absolute-path fixture mounts. Adapter-generated argv goes to Certbot unchanged through an injected trusted runner; this is test infrastructure, not a product API for caller commands. Mount the challenge/config/work/log directories writable and CA read-only; no Docker socket in the Certbot container. Preserve original absolute paths because the product journal validates ownership. Fixed administrator binary execution remains the production path.

Before any future start, inventory peer IDs/state and persist fixture intent. Capture exact returned container IDs plus project/fixture labels immediately; after ambiguous start discover by the unique label and verify exact config before claiming ownership. `cleanup_owned` removes only saved exact IDs with matching labels; never Compose down/remove-orphans/prune. Run cleanup in nested finally independently of diagnostic collection, then compare peers and prove no matching fixture resources remain. Current host-network fixture creates no Docker networks or volumes.

Preparation uses released ephemeral port candidates, not durable reservations; authoritative bind failure must fail the run, never choose a production listener. DNS port additionally needs UDP availability validation immediately before startup. Root remains responsible for final adapter wiring and deciding when to execute the full fixture.

Acceptance to add after APIs freeze: actual issuance for both environment routes; trusted SNI body/route/backend verification; missing HTTP token causes real ACME failure; renewed fingerprint served; expired/wrong-host certificates rejected; lost issuance/reload replies recover without duplicates; sentinel and sibling preserved; stop/no wake; exact final cleanup. Until these run, the scaffold is not acceptance evidence.

Primary references: [Pebble v2.10.1 documentation](https://github.com/letsencrypt/pebble/tree/v2.10.1), [DNS companion flags](https://github.com/letsencrypt/pebble/blob/v2.10.1/cmd/pebble-challtestsrv/README.md), and [Certbot command reference](https://eff-certbot.readthedocs.io/en/stable/man/certbot.html).

## Actual issuance checkpoint

The scaffold now supports explicit `--run`. On 2026-10-11 the actual local run exited 0: pinned Certbot registered a real Pebble account, then CertificateManager issued and verified `development.fixture.test` through real Apache HTTP-01. Manager replay made zero additional executor calls; fresh inspect preserved the immutable generation. Certificate fingerprint: `ec3bcfd6cdbd7a46d2853b9fc82f0b89df345580c16c5ec70f4a3243867401be`; generation: `04261b0996c6d73f66a11ed49dc95dbb2ef0693a6aaca9daa9869fecf0e19dd9`. Result log: `/tmp/deploy-monitor-tls-live.log` (temporary local evidence).

All fixture containers were removed and the original peer container ID inventory matched after cleanup; Apache was terminated and temporary material removed. A separate query found no containers with the fixture label. This does not prove peer HTTP uptime or complete TLS delivery. HTTPS activation, application/gateway routing, renewal, negative challenge tests and ambiguous-effect recovery remain to integrate with the host adapter.

Fixture corrections required by actual execution: Python 3.14 strict trust required explicit CA keyUsage; the pinned Certbot image needs fixed `/usr/local/bin/certbot` entrypoint under the product's sanitized PATH; fresh inspect legitimately updates observed_at, so replay compares generation/status and command count. Verification was never disabled. No public CA, DNS or production site changed.

## Actual HTTPS activation checkpoint

A subsequent run (session 60114, exit 0) now uses HostAdapter's generated HTTP challenge alias and HostTLSAdapter.request_certificate with its challenge probe, then stages the actual issued generation. The real Apache graceful signal succeeds and the fixture deliberately drops its reply. `recover_tls` establishes active serving state by observation with exactly one reload call; `verify_tls` confirms the issued fingerprint. A separate SSL client validates the CA chain and SNI hostname and reads the synthetic application's exact body over HTTPS. All fixture containers, local Apache and upstream server were cleaned; original peer ID inventory matched.

This verifies initial issuance, actual HTTPS serving and lost-reload-reply recovery. The fixture uses synthetic static trusted route/certificate authority callbacks and a real local HTTP application; it does not prove durable domain-worker/SQLite principal/DNS authorization, Docker gateway/backend integration, renewal, two-environment TLS, public CA/provider behavior or browser acceptance. Those remain explicit later acceptance work.

## Final frozen-module rerun — 2026-10-11

Session 22635 exited 0 with actual issuance, strict renewal metadata inspection, same-generation replay without command, trusted SNI HTTPS body, and lost-reload recovery without repeat. Source hashes were stable throughout the run:

- `ingress_certificates.py`: `8174825c72bc5e987d17710b38973e427fd7891ed4148eb7cef42af7c5b126fc`
- `ingress_host.py`: `f994c18944becdd0669d32a653f64814d37f6c50927fb134dc96689f04d5c47a`
- `ingress_host_tls.py`: `095bd5303ce46d679fe270a8ac249044bba3da6df32acbc10cb0193273414482`
- `ingress_apache_executor.py`: `b35e56d4db5fa6f10516087d1b27a5cf86cfda7a04af8e371b4839915d86a0bd`
- `tests/live_ingress_tls.py`: `1ade01dc574ad9dbc1d51f4b23ce231bb552d202ce4ef8b6423c97aceb53d6ec`

The fixture docstring was then corrected to describe --run; this was a prose-only change. Final fixture SHA256: `270eefe8dddd0a7f50e8307daf0af56e7758c9498236fc818594f9dff0927645`. No further issuance was run for this documentation correction.

Actual Certbot 5.8.0 emitted `webroot_path = <challenge_root>,`, then `[[webroot_map]]` with `development.fixture.test = <challenge_root>`. The final strict module accepted this real metadata. Certificate fingerprint: `7ef0ed86bdbaeb0635f72e7ed7660a23245390540bcab9c139a1bb18dc731509`. Exact cleanup and unchanged peer ID inventory passed; subsequent fixture-label inventory was empty. Raw local result: `/tmp/deploy-monitor-tls-final.log`.
