# Managed migration pipeline: local acceptance, 2026-10-11

The disposable local acceptance in `tests/live_managed_migration.py` completed successfully (process 91211, exit 0; execution log `/tmp/live-managed-migration.log`). This proves the migration-to-managed-rollout slice. It does **not** prove application database connectivity or production deployment.

## Actual execution

The fixture used Compose project `deploy-monitor`, a private monitor SQLite database, disposable PostgreSQL storage, and real development and production managed application containers. It:

1. Collected the PostgreSQL engine's real database name, port and persistent storage, then used the product adoption preview and adoption operation.
2. Retained the immutable adopted database recipe while provisioning a managed application in the same environment, then closed and reopened SQLite to verify ownership persistence.
3. Saved a separate purpose-bound migration credential and environment configuration; enqueued an immutable managed job through `store.enqueue`.
4. Built a real Docker image and ran its isolated test through `worker.build_artifacts`; published the application artifact and persisted the selected migration sources.
5. Ran the normal managed worker, which planned and applied the PostgreSQL migration through the packaged helper, reconciled its ledger/schema proof, replaced the application container and verified its health and HTTP response.
6. Ran a second job with the same migration sources, verified an empty pending set, and completed another healthy application replacement.
7. Lost the response after a real successful migration commit. Recovery verified the committed database state without applying again, observed the unchanged application and released the failed delivery.
8. Interrupted another job after planning but before apply. Recovery performed no migration mutation and released the unchanged application delivery.

The fixture retained both a pre-existing business sentinel and an application table row. The production application remained running. Cleanup removed exact recorded fixture containers, networks, volumes and build images; the baseline container identities, image identities and running states matched afterward.

The pinned migration helper image was:

`sha256:181aa258b69f0be25f34a6c3a22ebac7d6dd76aab71d1c43df3994bfb02360b0`

Git transport was explicitly simulated by copying a fixed local source tree and supplying its fixture commit identity. Docker build, isolated image test, publication, enqueue, adoption, ownership checks, helper execution, PostgreSQL transaction/ledger/schema checks, container replacement and health checks were actual operations. The fixture deferred the worker's general image-cleanup scheduler and performed exact fixture cleanup itself.

## Ownership correction established by this run

The first attempt stopped before enqueue because one environment recipe could not represent both an adopted database and a managed application. The fix retains the original adopted recipe through `adopted_workload_owners`, tied to the immutable recipe digest and verified adoption fingerprint. Ownership resolution considers current and retained recipes without duplicate claims; conflicting ownership fails closed. Managed coexistence is limited to a verified database-only adopted workload. Mixed legacy application/database adoption does not implicitly authorize a managed application takeover.

PostgreSQL direct database evidence comes from the trusted read-only engine query. It is a fallback when no application client already identifies a database on that container, avoiding substitution of the default maintenance database for an application's database.

## Recovery retention

A subsequent review added enqueue-retention coverage: old terminal parent jobs remain retained when their migration is unresolved or its journal is missing. This preserves the job target, migration target, sources and proof needed for recovery. Proven terminal orphan metadata can expire atomically; unknown orphan targets cannot be deleted. These retention changes were tested separately after the live run and did not change its executed migration/rollout scenario.

## Remaining product boundary

The fixture application serves an HTTP health response; it does not connect to PostgreSQL. Managed application provisioning currently has an internal environment network and no explicit product contract for joining the adopted database network and delivering scoped application database credentials. That network/configuration/secret-binding workflow still needs implementation and actual application read/write acceptance. The migration helper's private connection to the database namespace is not a substitute for application connectivity.

No external Git provider, public service or production database was mutated. No claim is made here that all database engines, baseline onboarding, or the entire CI/CD product are complete.
