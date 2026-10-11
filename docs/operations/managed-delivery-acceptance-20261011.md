# Managed pipeline acceptance — 2026-10-11

The source profile can now drive build-and-deploy for an existing verified managed environment. A fingerprinted managed-build target includes exact prior definitions, Docker bindings and effective budgets. Source validation alone remains build-only; no provisioning or waking stopped containers is inferred.

## Verified evidence

- Full backend suite: 380 tests passed; frontend: 43 tests passed and Vite build passed. Docker receiver and worker builds and isolated imports passed.
- `tests/live_managed_rollout.py`: actual Docker replacement and unhealthy-candidate restoration verified. Direct caps 80 MiB / 0.3 CPU preserved, stopped peer stayed stopped, other environment and preexisting host peers unchanged. Cleanup passed. Report `.runtime/managed-rollout-last.json`.
- `tests/live_managed_pipeline.py`: actual source-profile save, Docker ownership snapshot, immutable enqueue, Git checkout, Docker build, isolated tests, publication, worker rollout and metadata CAS. Source completion left job deploying with original runtime; only verified replacement + metadata completion yielded succeeded. Stopped peer reported skipped-stopped. No persistence/authorization/state helpers mocked; local Git remote and isolated network substituted. Report `.runtime/managed-pipeline-last.json`.
- Independent reviews identified and resolved recursive worker locking, missing managed dispatch, cross-environment artifact masking, Compose unsupported flag, new-binding digest/image CAS, and queued pre-claim stale-plan abandonment.
- Pending image publication is durable and retryable. Build proof survives a later runtime failure. Runtime uncertainty preserves journals and does not claim restoration. Completed rollouts release historical image protection; current definitions and unresolved recovery retain their images. Cleanup never forces deletion or prunes data volumes.

## Not yet accepted

This is local backend/Docker acceptance, not production deployment or complete browser UAT. Full authenticated browser-clicked managed delivery, source/runtime version history UI, automatic reconciliation of interrupted ambiguous mutations, DNS/routing, persistence/database templates, MCP integration and the overall production adoption remain open. Existing SQLite ResourceWarnings and bundle-size warning remain. No Git push or production service mutation occurred in this pass.
