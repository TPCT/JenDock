# Environment workspace acceptance — 2026-10-10

Local implementation and browser evidence; not production deployment acceptance.

- Project header selects Development or Production; primary tabs are Overview, Containers, Deployments and Settings.
- Selected environment and workspace tab survive reload through URL parameters.
- Local AIC website Development automatic preference was toggled off, preserved across a Production switch, saved through the real registry API and reread after browser reload. Production remained on. Development was restored to on through the same UI.
- Existing workloads without a verified environment binding remain explicitly unassigned and retain their management routes. No running workload was reclassified or recreated for this UI review.
- Mobile 390 px: branch menu bounds left 19/right 364 (345 px width), no document horizontal overflow. Desktop checked at 1024 px.
- Screenshots: /tmp/monitor-workspace-mobile.png and /tmp/monitor-workspace-desktop.png.
- Local receiver data now persists in the existing deploy-monitor Compose project through a private ignored state directory. Captured runtime observations retain original timestamps; they are not represented as live production data.

Remaining acceptance: verified live workload adoption, selected-environment build routing/provisioning, direct per-container limits UI/runtime integration, complete container tools and production rollout. Environment-specific builds are not enabled by this frontend work alone.

## Direct container limits checkpoint

- Container page now includes real inspected RAM/CPU and Edit limits, with a centered dialog and footer actions, identity/revision checks, policy limits, retained drafts and stable retry IDs.
- 167 backend tests and 17 frontend tests passed. Receiver and worker Docker images built successfully.
- Real Docker test: direct running 96 MiB/0.2 CPU and stopped 160 MiB/0.4 CPU, persisted recreation, kernel enforcement, replay and stale-ID rejection passed. Existing policy-based checks also passed. Test container removed and existing peers unchanged.
- Local captured browser data lacks live resource inspection; unavailable resource values and mutation guards were inspected, not substituted with synthetic successful values. End-to-end browser submission against a live control worker remains pending.
- Current source remains local and has not been pushed or deployed to production in this checkpoint.

## Environment delivery and workload linking checkpoint

- Reviewed environment revisions now travel with manual builds and webhook runs; Development and Production runs do not supersede each other. Retry preserves the recorded target.
- 197 backend tests and 20 frontend tests passed. Receiver and worker images built locally; the final frontend actions refinement was rebuilt separately.
- Existing workload secondary actions now share one keyboard-accessible menu. Containers and environment linking remain visible.
- Real local Docker adoption test verified persisted registry binding, exact replay and unchanged container/data/configuration. This does not establish production adoption or database integrity.
- Read-only production assessment found 13 exact configuration matches and 7 Compose identity conflicts. No production bindings were changed. See adoption-assessment-20261010.json.
- Production remains on its previously observed revision. Current changes have not been deployed. Production migration execution, provisioning, complete container tools and live browser-to-worker acceptance remain open.

## Container logs and loading checkpoint

- Added authenticated bounded Docker logs using immutable container IDs, known-secret redaction, 128 KiB output bound, subprocess timeout and no persisted log bodies.
- Log UI polls every five seconds without overlapping requests, preserves snapshots when paused or on errors and discards replaced-container responses. Oversized partial lines are omitted with an explicit excerpt state.
- Real disposable Docker log fixture passed stdout/stderr, stopped-container reading and redaction; fixture removed and existing Compose peers unchanged (`tests/live_logs.py`). This is backend/runtime evidence; production browser-to-worker reading remains pending.
- 212 backend tests and 23 frontend tests passed. The frontend entry bundle dropped from about 1,058 kB to 684 kB by deferring container/terminal, telemetry, run details and integrations screens. Core size warning remains; no browser speed metric claimed.
- Exact ordered Compose invocation matching is shared across adoption, workspace, control and worker. Reversed and duplicate file lists reject; no alternate provenance was silently accepted.
- Local browser smoke confirmed integrations and telemetry chunks load. Rebuilding while a tab held the previous entry script exposed a stale-chunk retry issue; recovery is being fixed before completing this checkpoint.
- Recovery fix: failed dynamic imports now offer explicit Reload application to obtain current asset hashes; ordinary render failures retain Retry. A fresh local browser loaded integrations, telemetry and container detail successfully. Captured stale inventory correctly disables live log access; successful browser-to-live-worker log reading is not yet verified.
- Final receiver and worker Docker builds both completed successfully. Frontend test rerun: 23 passed. No production deployment in this checkpoint.

## Reviewed backend continuation

- Real authenticated HTTP -> Unix worker -> Docker log acceptance passed (`tests/live_logs_http.py`): 401/403, owned immutable identity, redaction, stale ID after same-name replacement, refreshed ID success, fixture cleanup and unchanged peers. Rendered live browser polling remains unverified.
- Direct container start/stop/restart now invokes and waits on immutable ID. Bound registry compatibility projection now loads and validates the exact environment recipe, preserving adopted service ownership and rejecting corruption/cross-project references.
- Semantic Compose comparison and bounded managed resolver implemented and independently reviewed. Changed referenced secret/config bytes change the proof. Live read-only comparison succeeded for ten alternate-invocation services across seven projects; no adoption/mutation performed.
- Full backend suite: 230 tests passed. This checkpoint does not close provisioning, migration execution, file/database tools, MCP, or production acceptance.

## Provenance lifecycle checkpoint — 2026-10-11

- Alternate Compose invocation proof is integrated into adoption, workspace ownership, container control and worker prebuild/precutover checks. Proofs remain private; invalid or changed proofs cannot silently grant membership/control.
- Real Docker alternate-file adoption passed without moving data or changing container identity. Real stopped-container limits passed twice (96 MiB/0.2 CPU then 160 MiB/0.4 CPU) using independent original/canonical definitions, successor proof persistence, registry reopen and replay. Fixtures removed and peer lifecycle unchanged.
- Journal recovery tests cover before/after proof commit, concurrent preferences, restoration/metadata failure and failed journal deletion. No deployment success is reported before cleanup.
- 266 backend tests passed. Production binding/rollout and the remaining full-product scope are still incomplete.
