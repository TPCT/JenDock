# Compose provenance reconciliation

Status: reviewed implementation design; no live identities relaxed or adopted.

Exact ordered file identity remains required today. Captured discrepancies exist for aic-academy, aic-sys, aic-website, dsl01, dsl02, elfak-legacy and licence. Six involve datastore/cache services; dsl01 also involves its deployable application service. Matching project labels alone does not authorize deployment.

The next implementation must separate the canonical deployment invocation from per-service observed provenance. Worker-owned evidence must resolve both configurations with the same explicit environment context and compare the selected service, referenced networks/volumes, dependencies, image, runtime mounts and database endpoint. Unknown differences remain explicit blockers. Never union historical file lists, discard sensitive configuration fields to gain equality, or infer ownership from prefixes alone.

An immutable recipe may contain verified original and canonical ordered invocation identities per service. Read-only membership and permission to recreate are separate capabilities. Adoption, workspace, control and worker must consume the same provenance matcher. Jobs pin the provenance map with their recipe revision. Verified canonical recreation must remain recognized afterward.

Required acceptance: captured seven variants, semantic equality, meaningful mount/network/environment differences, wrong service, unknown paths, reversed/duplicate files, stale preview, canonical recreation, unchanged sibling services and unchanged database sharing guards. First run a disposable real Compose fixture; current evidence does not authorize production reconciliation.

## Implemented comparison layer

`compose_reconciliation.py` compares resolved service/dependency/resource semantics; `compose_review.py` resolves worker-owned managed paths with explicit interpolation context, hashes referenced config/secret file bytes and repeats resolution to detect drift. They return no configuration values. Lowercase top-level `x-*` blocks are nonexecuting extension definitions per https://docs.docker.com/reference/compose-file/extension/; expanded service settings still compare exactly. Unknown globals remain compared.

Read-only live evidence at 2026-10-10T20:58:50Z found equivalent current resolved configuration for all ten alternate-invocation services across the seven projects. See `docs/operations/compose-reconciliation-20261010.json`. SSH host/UID/address were verified. No production files, containers, bindings or database records changed.

Still required: integrate immutable per-service proof into adoption and all consumers, revalidate before mutations, advance proof after legitimate config changes with journal/CAS, inspect actual runtime/config divergence and rerun database identity/sharing checks. Equality of two current definitions is not evidence that either exactly matches the already running container.
