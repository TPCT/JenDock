# Platform completion audit — 2026-10-11

This is a requirement audit, not a declaration of completion. Existing unchecked
plan boxes and old “not implemented” headings are not authoritative implementation
inventories. Source, targeted runtime evidence and deployed state are separate.

## Verified production baseline

Read-only SSH with BatchMode and strict host-key checking succeeded. Hostname
`root`, UID 0, and eth0 `176.118.198.49` matched the authorized target.
Docker is not in the SSH PATH and the default socket does not exist. Using the
configured executable `/opt/docker-engine/29.8.2/docker/docker` and socket
`unix:///run/docker-isolation.sock` returned the live monitor inventory:

- receiver: `deploy-monitor-receiver:0ac2162`, healthy, up 15 hours.
- worker: `deploy-monitor-worker:0ac2162`, healthy, up 15 hours.
- receiver image ID: `sha256:7fe7ed26f62dfb22deae697309b6e389f2e2d757055249a007c9a1376954a554`.

This inspection proves login and current container identity/status only. It does
not prove whole-product health, host integrity, database correctness or deployment
of subsequent local commits. No server mutation was performed in this audit.

## Requirements and evidence boundary

| Capability | Implementation/evidence | Remaining acceptance or implementation |
| --- | --- | --- |
| Dynamic project/environment registry | `registry.py`, shared authenticated `application_operations.py`, registry-backed settings and immutable targets | Production import/adoption and public runtime verification |
| UI and MCP creation | Shared operations, scoped credentials, verified artifacts, durable provisioning; local SDK/worker/console evidence in MCP acceptance docs | Production packaging/client acceptance; database/queue/scheduler role templates |
| Automatic managed delivery | Immutable source/build/test/publication, rollout identity and recovery, health and protected image cleanup | Complete database migration stage; durable cleanup retry and actual untagged-image recovery now verified |
| Production migrations | Explicit executor design; legacy path fails closed for unsupported schema operations | Alembic and Laravel PostgreSQL parsers/executors passed actual PHP/PG schema acceptance; environment binding, helper protocol and worker/ledger integration remain in progress |
| Development migrations | Fixed Laravel pending migration command in legacy path | Verified DB binding and pending ledger; Alembic adapter; explicit development-only reset configuration |
| Database inspection | Encrypted configuration; actual authenticated HTTP/Unix/Docker/PostgreSQL two-environment browser configuration/acceptance/rows/revocation; durable rejected-save recovery; scoped MCP protocol reads; MySQL 8.4.11 and MariaDB 11.4.13 direct-adapter and authenticated HTTP/Unix/helper acceptance; PostgreSQL official SDK-to-live-engine acceptance | MySQL/MariaDB full browser acceptance, broader row types, production rollout |
| Scheduler/queue | Deployment queue and monitor scheduling exist | Workload-specific schedule and job adapters, actual queue depth/failed job/retry evidence |
| Container tools | Identity-checked operations, logs, terminal, resource controls and read-only files | Role-specific DB/scheduler/queue tools; broader production acceptance |
| Domains/TLS | Durable owned DNS/routing, TLS issuance/renewal, external proof freshness; actual local two-environment fixtures | Public provider/CA rollout, legacy routing/redirect, expired-serving recovery acceptance |
| Responsive product UI | Shared spacing, current React environment workspace, desktop/mobile evidence | All-screen/state acceptance against real production APIs remains open |

## Required execution order

1. Make unsupported managed migration requirements explicit before cutover; do not
   convert missing implementation into an approval prompt or a completed stage.
2. Implement verified environment database bindings and a useful read-only inspector.
3. Use that binding foundation for automatic supported migration execution and
   actual ledger/schema evidence. An interim rejection is not migration support.
4. Complete workload scheduler/queue instrumentation and cleanup recovery.
5. Reconcile all packaging, legacy compatibility and deployment prerequisites;
   deploy and perform production acceptance separately from local fixture results.

The target remains the full monitoring and delivery product, including every
required capability above. A passing component test suite cannot close this audit.

## Interim managed migration source boundary

The managed build worker now compares every selected service's pinned artifact
source SHA with the exact candidate checkout before Docker build/publication or
cutover. Recognized changed migration/schema paths fail with a concrete unsupported
runner error and bounded escaped filenames. Renames/deletions are included. Missing
baseline plus current recognized migration paths, and unavailable baseline commits,
are rejected. Validation-only builds remain available; unchanged recognized files
with application-only changes remain eligible.

This is a source heuristic, not ledger verification. Unknown baseline with no
current recognized files cannot detect removed old migrations; custom paths,
imported/startup code and submodules are not comprehensively classified. Unchanged
files may still be unapplied DB migrations. Already-published artifact resume is
outside this pre-build check. No database safety or migration completion claim is
made. The verified binding and trusted executor remain required.

Independent implementation/review plus actual temporary Git histories cover
added/changed/deleted/renamed paths, multiple baselines, unavailable baselines,
sanitized diagnostics, no Docker/publication effects on rejection, and isolated
validation. Current backend suite: 928 tests passed. Receiver, worker and MCP
Docker targets built successfully; network-disabled module import checks passed.
Images: receiver `a507d553883b`, worker `0992608299ce`, MCP `e2916dacc0a7`.
These are local builds; the verified production baseline above remains unchanged.

The next implementation contract is `../specs/database-inspector.md`, reviewed
against both backend identity/permission boundaries and frontend workflows.
The PostgreSQL backend path now has real local runtime acceptance (see database-console-20261011.md). The broader inspector workflow and platform requirements remain open.

## Latest local build and review

Three application targets rebuilt successfully after cleanup and shared database
ownership changes; read-only, network-disabled Python import smoke checks passed.

- receiver: `sha256:8338fec92f5a3ea7f8c649d4236aacb244551d1128d4f8f20b9a2bec9573a0ae`
- worker: `sha256:0e95aa6f49c04dac3fc0073d14438507fbf16aff8c991984797319f012a3a2e1`
- MCP: `sha256:7169d6f0a4a28576a6932bb0e734869b1c11574a350c35f9194c11202f2511df`

The full suite before the new migration-binding work passed 1089 backend tests
and 89 frontend tests. New ownership and purpose-bound migration-credential tests
passed separately. Purpose-separated migration accounts are encrypted and support
rotation/revocation and durable save receipts; this is not yet a published UI/API
or an integrated migration stage. See cleanup-recovery-20261011.md for the actual
Docker untagged-image deletion/retry proof and the Laravel acceptance note for
real engine evidence. Production still runs the separately verified baseline.
