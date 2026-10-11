# Managed application-service creation — 2026-10-11

## Delivered source boundary

The authenticated console can request a stopped or running application service in an explicitly selected Development or Production environment. Requests select immutable artifacts published by a successful verified build and its reviewed service profile; they cannot supply arbitrary images, commands, Compose YAML, host paths or credentials. Creation atomically records definition, idempotency result, name and RAM/CPU reservations. Running requires start permission. A worker claims the durable operation, journals mutations, creates an isolated internal environment network and managed Compose service, verifies actual ownership, image, caps and health, then commits the binding and allocation. Resume retains the original operation and reconciles recorded resources.

The React Containers tab contains the creation modal, environment identity, artifact selection, RAM/CPU inputs, stopped default, explicit start option, operation polling and same-operation recovery. Nonsecret request identity survives reload under the authenticated user/project/environment. The local preview exposes real catalog reads but remains disconnected from production operations.

Service artifacts publish as an atomic batch. Successful build/deployment state and retry intent are persisted together; later catalog failures preserve source success and produce a visible sanitized pending event. Retries cannot let an older build replace a newer source artifact. Cleanup protects current catalog artifacts, accepted service sources and pending publication outputs; no volumes are pruned. Source-job retention preserves this provenance.

## Evidence and limits

- Final backend suite: 330 tests passed in 12.433 seconds. Two SQLite ResourceWarnings surfaced during test cleanup; no failures. Their origin still needs cleanup investigation.
- Final frontend suite: 38 tests passed. Vite production build passed; the existing large-chunk warning remains.
- Docker receiver and worker builds passed. Actual isolated container execution imported server/application operations/managed services and worker/provisioner/publication modules successfully.
- Actual local Docker acceptance (`tests/live_service_creation.py`, evidence `.runtime/service-creation-last.json`): Development and Production simultaneously running on distinct internal networks; additional service stopped and never started; actual cgroup 64 MiB/0.25 CPU/no-swap; idempotent replay; actual limit recreation to 80 MiB/0.3 CPU followed by adding another service without reverting caps; verified binding-ID reconciliation; cross-environment TCP denied; workspace/control reads after SQLite reconnect. Fixture containers, networks and image removed; peers unchanged after comparing full mount dictionaries independent of array order.
- Browser verified the actual empty-catalog state, selected environment, disabled unavailable artifact selector and desktop/mobile modal. Final mobile screenshot `/tmp/monitor-service-create-mobile-20261011.png`; no horizontal overflow and visible footer actions. Successful creation, interrupted-response recovery and resume were verified through application/executor tests, not yet as complete browser flows.
- Independent reviews fixed: adoption versus queued creation race; partial publication corrupting successful source state; discarded same-ID peer state; public pagination truncating executor inventory; delayed old artifact replacing newer build. The final scoped chronology re-review found no residual issue within that scope.

## Still required for the full product

A new metadata project has no source/build profile onboarding UI yet, so its catalog correctly remains empty. This flow currently provisions applications without domains, public ports, persistence or secret injection. Database/queue/scheduler templates, mixed adopted/managed environment extension, full MCP authentication/transport, domain orchestration, production-safe migration execution and complete container tools remain open in the platform plan. These are unfinished requirements, not delivered features.

No production deployment, workload adoption or Git push was performed in this checkpoint. Existing production environments were not modified.
