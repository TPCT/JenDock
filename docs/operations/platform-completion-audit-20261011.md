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
| Automatic managed delivery | Immutable source/build/test/publication, rollout identity and recovery, health and protected image cleanup; supported PostgreSQL migration-to-rollout fixture verified | Broader migration dialect support and production rollout |
| Production migrations | Supported Alembic/Laravel PostgreSQL parsers and helper protocol, immutable environment binding, automatic worker/ledger integration, no-op and lost-response reconciliation verified locally | Broader migration syntax/dialects; production rollout and real application acceptance |
| Development migrations | Supported PostgreSQL migration binding and pending ledger through the managed worker; legacy fixed Laravel command | Explicit development-only reset configuration and broader migration coverage |
| Database inspection | Encrypted configuration; actual authenticated HTTP/Unix/Docker/PostgreSQL two-environment browser configuration/acceptance/rows/revocation; durable rejected-save recovery; scoped MCP protocol reads; MySQL 8.4.11 and MariaDB 11.4.13 direct-adapter and authenticated HTTP/Unix/helper acceptance; PostgreSQL official SDK-to-live-engine acceptance | MySQL/MariaDB full browser acceptance, broader row types, production rollout |
| Application runtime/database connections | Immutable encrypted runtime settings, application account and attachment API; actual PostgreSQL app read/write/deploy/rollback/reconnect and browser Apply verified; stopped-DB/revoked-credential stop verified | Actual multi-application shared DB acceptance; MySQL/MariaDB application bindings; endpoint replacement; production rollout |
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

## Migration integration review follow-through

The optional helper protocol, durable job capture/coordinator and environment
configuration UI are now implemented locally. The actual helper fixture passed
PostgreSQL/Laravel/Alembic apply, reconciliation and no-op checks with exact cleanup
and unchanged peer containers. This is helper acceptance, not whole-pipeline proof.

The combined managed-delivery fixture initially exposed a recipe ownership
conflict. A separately pinned, immutable adopted-workload association now retains
the verified database owner alongside a managed application recipe. Independent
review found and fixed pre-effect acceptance after unrelated recipe rebinding,
and false ambiguity between the default PostgreSQL database and client evidence.
The normal PostgreSQL-only adoption path now records trusted engine observation.

The corrected actual fixture exited successfully: normal collect/preview/adopt,
managed provisioning, SQLite reconnect, enqueue, actual Docker build/test/image
publication, helper migration and healthy application rollout. A subsequent run
verified no pending migrations; a lost commit response reconciled without replay;
interruption before apply caused no database mutation. Business sentinels and the
separate production fixture survived. Exact cleanup preserved all preexisting
peers. Only Git transport was simulated with immutable local fixture sources.
Evidence: `/tmp/live-managed-migration.log`, session 91211, exit 0.
This does not prove application database connectivity: the fixture's HTTP app
returns a fixed body. Managed application database network/configuration binding
is still required; no manual network attachment was used to mask that gap.

The real HTTP/Unix configuration browser fixture saved and reread an enabled
Laravel binding; switching environments now uses unsaved-change protection.
The fixture ended normally and its disposable local UI journal row was removed.
Migration run status exposes only bounded file paths and verified journal digests.
A stopped pre-execution run displays Not run, while uncertain execution remains
unresolved even when its parent run is terminal. Four backend status tests and
98 frontend tests passed; frontend build passed with the existing bundle warning.
Actual desktop and 390x844 mobile browser review verified terminal Not run,
unresolved recovery, Files dialog wrapping and Close behavior. Disposable UI rows
were removed exactly. Screenshots are in
`/tmp/deploy-monitor-migration-status-evidence/`. The backend suite in the existing
MCP virtual environment passed 1145 tests on the final frozen source; system Python alone lacked required MCP
and PostgreSQL dependencies and was not the valid full-suite runtime.

`compose.migration.yml` supplies the same helper image ID to receiver and worker
and the existing vault key to the worker only. Actual Compose rendering combined
with `compose.database.yml` verified one read-only, no-create key mount. Independent
review passed. See migration-runtime-setup.md. No production update occurred.

The final receiver/worker/MCP builds passed. Every packaged Python module was
hashed against the frozen checkout (38, 63 and 39 modules respectively), and
network-disabled, read-only import smoke checks passed in all three images:

- receiver: `sha256:b40efa46358a4a02832647be3554aa4aba2e19d251e26cf5fd043f129f5279c3`
- worker: `sha256:86115bde2c89fb712ed4db71addd6630e0b5c98c7705cb429617f07083e8282a`
- MCP: `sha256:c21863d080ed96126e36ecbd2c00be29627e62e6c2ebaf3a7fb0f05909ade74e`

## Application runtime configuration integration — current work

Managed environment edits previously rewrote canonical JSON as YAML and failed
to advance the managed definition/container identity. Regression tests now cover
the durable same-image transition. Independent review found and corrected
non-atomic runtime acceptance and stale control recovery receipts. These fixes
are still undergoing integration; they are not a production completion claim.

Application credentials and database attachments now have closed authenticated
HTTP/Unix API actions. Sixteen transport tests pass, including encrypted account
save/replay/current-state/revoke, retained ownership attachment save/get/list/disable,
wrong-environment rejection, no secret echo and unchanged read-only MCP scope.
Thirty-five application-domain tests pass. Configuration receipts explicitly
report unobserved runtime application; saving a binding does not prove connectivity.

The dedicated internal database bridge and immutable runtime revisions are in
implementation/review. Actual application write/read, deployment/rollback/restart
acceptance and complete browser applied-state acceptance remain required.

Backend checkpoint during this integration: 1,190 tests passed in 42.956s.
Subsequent network/runtime integration edits require fresh verification; this
checkpoint does not establish their completion or production acceptance.

Managed RAM/CPU edits now route through the same durable transition as encrypted
runtime settings. Actual SQLite control tests verify policy/definition/binding
commit on success and exact previous policy preservation on candidate failure;
Docker effects in these two tests are scripted. Forty-one control-pattern tests
passed at this checkpoint. Environment variable edits now reject attachment-managed
keys rather than silently accepting values that the binding would override.

A newly reproduced lifecycle regression remains open: direct managed Stop returns
success after Docker stops, while the managed service/binding still report running.
`tests/test_managed_lifecycle_regression.py` is intentionally failing until the
identity-preserving lifecycle journal/projection is integrated. This means the
current complete test suite is not green; the earlier 1,190-test checkpoint is not
current completion evidence. Actual application/database Docker acceptance is
running independently; no result is claimed before its process terminates.

Actual application/database Docker run subsequently completed successfully with
exact cleanup and peer preservation. See `application-runtime-acceptance-20261011.md`
for the tested outcomes and remaining boundaries. Direct lifecycle repair and
actual browser runtime Apply remain open.


## Runtime acceptance and packaging review update

The lifecycle state regression above is repaired locally. A fresh full backend
suite passed 1,207 tests; two subsequently added isolated packaging tests also
passed. Receiver/worker COPY lists now include managed_lifecycle; MCP inherits it.
New images have not yet been verified for this source.

Actual Docker fixture session 71026 exited 0. It verified PostgreSQL application
read/write, settings persistence through replacement and image delivery, lost-result
reconciliation and failed-health rollback, then stopped the database (both Docker
EndpointIDs cleared), revoked the application account and disabled its binding.
The exact registered app could still be stopped; a subsequent start was rejected.
All nine runtime module hashes matched before/after. Exact cleanup preserved peers.
Evidence: /tmp/live-application-database-stop.log.

The separate authenticated browser Apply fixture also passed actual HTTP/Unix/Docker
application and SQL persistence checks; see application-runtime-acceptance-20261011.md.
These results supersede earlier open acceptance statements for those exact paths.
Normal start/restart and resource changes still need equivalent actual Docker
acceptance. Independent review found a lifecycle replay receipt mismatch gap;
it is being repaired before final image build/acceptance. This is not production
or full-platform completion.


Final local runtime checkpoint: extended session15362 passed normal lifecycle,
actual resource replacement, SQL preservation, revoked/stopped dependency checks
and exact cleanup with matching source hashes. 1,210 backend and 106 frontend tests
passed; all five Docker images passed build, packaged-source hash comparison and
network-disabled read-only import checks. Image identities and evidence are recorded
in application-runtime-acceptance-20261011.md. The prior failed run remains an
unclassified intermittent outcome. Workload queue/scheduler capabilities are the
next implementation contract in ../specs/workload-roles.md; that document is not
an implementation claim. The full platform and production acceptance remain open.

## Role foundation and recovery review checkpoint

The next local checkpoint adds schema-3 typed queue/scheduler profiles while
preserving application artifact versions. Workload metadata is immutable; a
worker-only capability verifier seam gates role acceptance and delivery. A
user-declared package name/digest cannot make a role operational. The isolated
verifier still needs to be wired to the build pipeline and runtime control panels;
those features remain open. Trusted process metadata is derived from the accepted
definition and cannot be written through runtime variables. Queue grace is carried
through Compose and lifecycle commands; real workload-container acceptance remains
required.

Independent product review found and repaired browser recovery gaps: generic
container actions now persist a safe original identity before dispatch, creation
stops if recovery storage fails, and direct container navigation resolves the
managed environment from server evidence. An explicit conflicting URL environment
is blocked. The read-only operation lookup checks actor/project/container/action
and is independent of the 100-record activity window. A second review found the
state feed bypassed actor filtering; both HTTP state and SSE now expose only the
session actor's operation records with sanitized messages. Actual authenticated
HTTP tests prove recovery lookup does not dispatch to the worker and rejects
missing sessions/incorrect CSRF.

Current checkpoint evidence: 1,233 backend tests passed in 44.028 seconds
(`/tmp/platform-role-recovery-final-suite.log`), and 113 frontend tests plus Vite
build passed. Earlier 22 suite errors came from impossible artifact-less managed
service test fixtures; fixtures now model accepted artifacts, and malformed
production evidence fails closed with a clear error. Browser recovery acceptance,
fresh five-image packaging, actual framework instrumentation acceptance and
production deployment are tracked separately and are not implied by these tests.

Fresh five-image build completed after adding workload_environment.py to the
Docker context allowlist. A new packaging regression checks every copied Python
module against that allowlist. All 3 packaging tests pass. Build input hashes
remained unchanged; network-disabled read-only import checks and source hashes
matched every packaged Python file (plus migration parser).

- receiver: `sha256:8534f6e0245954f6a235178339df00fa6066a11d87cfec48b89a94aa2eae7780`; 44 files checked.
- worker: `sha256:0c67efccfa50ee7de0d924ec52f9ebf30d14d6c7d4b9232a7b115e5fc9525c97`; 71 files checked.
- mcp: `sha256:fe6cb9587a9389e25b7ec4c65ba6c583a0138874b9ad1bb107d6c0ba9caa81c9`; 45 files checked.
- database: `sha256:4107c8d38e02e61b98a4682a5ed348ebd88e7c616bab63d8d915c7f43b61a660`; 3 files checked.
- migration: `sha256:11c8ca46e010909fe7020296686ef2adf2d3e358e7b5d3711f580f47dc132987`; 7 files checked.

Three exact superseded, untagged, unused receiver/worker/MCP images were removed
without force after inspecting container references. Volumes and older helper
images pinned by acceptance fixtures were retained.

Desktop browser recovery acceptance passed with real authenticated HTTP, Unix
transport and SQLite, using explicitly synthetic container observations/outcomes:
original receipt restored after reload, older-than-100 receipt recovered, later
terminal result displayed without redispatch, missing environment query resolved,
conflicting environment blocked, platform variables protected. Evidence is under
/tmp/container-recovery-ui-evidence/. Fixture exit 0 and exact cleanup confirmed.
This is browser workflow acceptance, not proof of actual Docker mutation.

Instrumentation checkpoint: the experimental package pins patched Laravel 12.69.3
(the earlier 12.35.0 pin was rejected by Composer's advisory checks). Eight unit
checks and five isolated framework regressions pass. The actual host-PHP/Laravel
plus disposable Docker PostgreSQL fixture exited 0 in session 6185, covering real
queue events, atomic single-job retry/receipt replay, business completion,
scheduler events, no execution during reads, and wrong-scope rejection. Exact
cleanup, peer inventory and package-source preservation passed. Package digest:
`b0a6dcf746f0e2a22393bd9ed44e6e020e1e94965b46bf7c3b08f854006aafa5`.
Evidence: /tmp/live-workload-laravel-four-fixes.log. Four review defects were fixed:
skipped-task attribution, accepted-timeout heartbeat freshness, ISO worker times,
and immutable empty schedule registries. Docker-exec identity, public API/MCP/UI
integration and broader concurrency/interruption acceptance remain required. The
new WorkloadPanel components are deliberately unintegrated until those boundaries
exist; neither the artifact gate nor those components prove an operational role.

## Private workload control integration checkpoint

The new workload_protocol validates closed adapter requests/responses, scope and
receipt identity before any application output can reach the console. The bounded
workload_transport verifies the registered container/image/runtime settings,
networks, package capability and trusted process metadata before fixed-argv Docker
exec; timeouts after retry dispatch are explicitly ambiguous. It never starts a
consumer or database. The independent workload_auth resolves live console/MCP
credentials with workload-only scopes, including repository/environment grants.
New scopes do not expand old tokens. Private peer channels require explicit
WORKLOAD_RECEIVER_UID / WORKLOAD_MCP_UID; they default disabled.

The durable retry journal records original actor and exact target before dispatch.
Lost replies/process death lead to receipt-only recovery, never an automatic second
enqueue. Reconciliation can persist a verified receipt under continuing read
permission; mutation acceptance/dispatch still require read plus retry. Container
preflight happens outside SQLite transactions, with static pins rechecked inside.
The controller preserves source observations separately from worker receipt times.

Console HTTP actions now reach the shared controller through bounded Unix RPC.
Actual authenticated HTTP/session/CSRF and kernel peer checks were exercised;
container observations in that fixture are test doubles. A separate integration
test uses the actual registry/definitions and journal with the bounded transport
and synthetic Docker replies. Neither is actual workload-container acceptance.
The 61 focused workload tests pass, including concurrent acceptance, lost reply,
authority revocation, response limits and real subprocess timeout/stdout/stderr
bounds. Frontend 113 tests and Vite build pass, with existing bundle-size warning.

Three affected images built; all five images passed packaged-source hashes and
network-disabled/read-only Python import probes. Evidence:
/tmp/workload-images-build.log and /tmp/workload-image-proof.log.
Receiver afd6143d6124649959d9b5fb1b1ff6b28bc715d564a79084b5fffa30e78a5a7f
(45 modules); worker 01633d45e0413001c47a060b8e5304c6ffb0e94ca5903b717f350335ae7a005b
(77 modules); MCP 1672fe0921dc04b2863014db43f2f03d414417aa87bac66ec5177e4380595ef5
(46 modules). Database/migration helper image/source identities are unchanged.

Still open: workload MCP tool declarations/routes, functional role-panel controller
and browser flows, build-time isolated role verifier wiring, enabling peer channels
in the accepted deployment configuration, actual Docker-exec Laravel/PG acceptance
with both environments, and the broader adapter/platform matrix. No production
push, deployment or destructive database operation occurred. Two review agents
stopped on usage limits after delivering code; root completed integration checks.

After successful image verification, exactly the three prior receiver/worker/MCP
image IDs from the preceding checkpoint were removed without force, after checking
all running/stopped container references and image tags/digests. No volumes or
helper images pinned by earlier acceptance were removed.

Final source checkpoint: all 1,279 backend tests passed in 47.755 seconds
(/tmp/workload-final-suite.log); 61 focused workload tests and 3 packaging checks
also passed. Existing test ResourceWarnings remain. The image import/hash probes
completed successfully and all build/test process handles exited 0.
