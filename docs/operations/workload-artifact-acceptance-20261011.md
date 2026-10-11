# Workload artifact verification acceptance

The final isolated live fixture completed with exit 0 on 2026-10-11. Evidence is
recorded in `workload-artifact-evidence-20261011.json` with exact source/image hashes.

The final complete backend suite passed **1,317 tests in 64.645 seconds**. Receiver,
worker and MCP images built successfully; read-only, network-disabled probes matched
the source hashes and imported the Python modules across all five system images.
Four exact superseded untagged/unreferenced images were removed without pruning
volumes or touching the retained candidate. Existing test resource warnings remain.

## What was exercised

- A real Laravel 12.69.3 candidate image with the measured package installed at its
  standard Composer path, running its own Artisan bootstrap as UID/GID 65534.
- Queue and scheduler capability requests and read-only initial observations inside
  candidate containers sharing only a pinned, network-disabled PostgreSQL fixture.
- Independently measured package bytes and Composer framework metadata, strict
  protocol scope/results, no host mounts/public ports, read-only candidate rootfs,
  fixed command, bounded process I/O, resource caps and no production vault lookup.
- Private durable journal replay with identical evidence, keyed by complete artifact,
  workload, verifier version and administrator-selected PostgreSQL dependency pin.
- Real registry/build-profile save, immutable job capture and publication through the
  actual verifier callback. Both role catalog entries were artifact-verified; their
  operational flags remained false. Completed publication replay did not call the
  verifier again.
- Exact-owned verifier container removal and an unchanged snapshot of all 32 peer
  containers. The candidate review image is retained for subsequent runtime acceptance.

The first publication run overlapped a separate root-owned Docker import probe,
so its global peer comparison was inconclusive. The final exclusive-window run
resolved this: all functional assertions, cleanup and peer equality passed together.

## Code review and failure coverage

Separate agents implemented the executor, publication transaction and actual-image
fixture; a read-only reviewer checked isolation/recovery and worker wiring. Review
fixes included secure first-use state-directory creation/fsync, exact attached
network checks, role-profile/scope validation before effects, and PostgreSQL pinning
in cached verification identity.

Stateful unit tests cover package tampering, false digest echo, framework/schema
mismatch, unsafe configuration, timeout/output bounds, uncertain create recovery,
missing ambiguous creates, foreign-resource protection, open transactions and
dependency-pin changes. Publication tests cover all-profile atomicity, evidence-write
rollback, source changes, missing/mismatched verifier and completed replay. These
negative Docker responses are simulated; the live fixture establishes the successful
actual-container path, not every injected-failure path on Docker.

## Outstanding work

This acceptance does not prove actual business-job execution, scheduler effects,
authenticated creation and runtime controls, simultaneous independent environments,
failed health rollout/rollback or legacy adoption. The complete matrix remains in
`../specs/workload-roles.md`. The fixture also does not exercise GitHub checkout or
the worker's full queued-build loop. Nonstandard application bootstrap and public
source-owned fixture settings remain unsupported until their versioned contract is
implemented. No production deployment or business data change occurred.
