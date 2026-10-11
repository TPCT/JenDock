# Interrupted managed delivery acceptance — 2026-10-11

Recovery now separates runtime observations from Docker mutations. A unique recovery lease and durable 30-second retry backoff guard read-only reconciliation; stale worker authorization cannot complete a newer attempt. Exact candidate identity, manifest, fingerprint, image/configuration, health, peers and network ownership are verified before atomic metadata CAS. Unknown outcomes retain protection and never replay create/start/rollback commands.

## Evidence

- 403 backend tests and 48 frontend tests passed. Vite build passed. Receiver/worker Docker builds and isolated imports passed.
- `python3 tests/live_managed_pipeline.py --recovery` passed with actual Docker: interruption injected after healthy replacement and before metadata commit; recovery adopted the exact existing candidate, persisted definition/binding and reached succeeded. Runtime recovery commands were inspection only. Effective caps, stopped peer, other environment and all existing host peers unchanged; cleanup passed. Report `.runtime/managed-pipeline-recovery-last.json`.
- Two-pass convergence tested: verified replacement metadata can be saved while sibling remains unknown; next read-only attempt verifies sibling using authorized updated metadata. Test advances backoff clock deliberately; production uses persisted 30-second delay.
- Local browser synthetic outcome fixture: Development label, deployed/rolled-back/skipped/pending rows, partial-failure heading and disabled Re-run with explanatory tooltip. Desktop and 390x844 mobile inspected. Screenshot `/tmp/monitor-managed-outcomes-mobile-20261011.png`. Synthetic fixture removed afterward; viewport reset. This is UI presentation acceptance, not browser-triggered production delivery.

## Boundaries

Preexisting rollout journals without peer snapshots or mutations without a recorded exact candidate identity remain unknown; they are not silently adopted. Docker unavailability or metadata/policy drift retains recovery-required. Full production activation, browser-triggered delivery acceptance, version history, persistent service templates, domains and MCP remain broader platform tasks. Existing SQLite ResourceWarnings and bundle-size warning remain. No Git push or production mutation in this pass.
