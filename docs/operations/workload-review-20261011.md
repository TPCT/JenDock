# Workload control review — 2026-10-11

## Verified boundaries

The console container-details response now projects recorded queue/scheduler role
metadata from the accepted definition. The projection exposes only typed public
configuration. It never claims a live observation and exposes no actionable target
when the binding is missing, abbreviated, or belongs to another definition digest.
The catalog and container details use the same projection.

The real SQLite registry/transport suite passed 10 tests, including current and stale
binding projection, defensive copying of queue configuration, fixed executable
arguments, pre/post identity checks and uncertain retry outcomes. Docker responses in
this suite are synthetic; this is not candidate-container acceptance.

The complete backend baseline passed 1,287 tests in 51.427 seconds before the additional
metadata regression was added. The frontend baseline passed 118 tests and Vite built.
These baseline results do not cover subsequent changes without a focused rerun.

## Independent review and remaining acceptance

The independent frontend review found retained pagination and unsubmitted retry
selection after a container/definition replacement. Browsing state must reset while
an already persisted request keeps its original target for receipt-only recovery.
Browser acceptance and the correction are being checked separately.

The correction was independently re-reviewed. Exact target changes now remount the
browsing view while the stored recovery key remains owner/project/environment/service
scoped. A browser pass also caught and fixed the route's undefined username and a
mobile scheduler digest overflow. The final frontend suite passed 119 tests.

Authenticated HTTP/Unix fixture browser checks covered queue/failure observations,
retry reload and receipt recovery, scheduler registry/history and desktop/mobile
rendering. Adapter responses were synthetic. Evidence is under
`/tmp/workload-ui-evidence/`; temporary fixture sessions exited and cleaned their
databases/credentials. Pending-navigation clicks were not separately exercised.

The independent package-byte verifier now exists and its eight negative/positive
tests pass (18 together with the transport suite). Actual `docker cp` archives of
the four fixed components from an exact stopped disposable container passed the
same 13-file digest. The fixture was never started and was removed afterward. This
does not execute the candidate's framework or prove its runtime capability.

Receiver, worker and MCP Docker builds completed. Read-only, network-disabled probes
checked all five system images against current source hashes and imported their Python
modules; all nine receiver frontend assets also matched the local build. The worker
packages the verifier but the publication hook is still outstanding. Exact superseded
receiver/worker images were removed only after checking all stopped/running container
references and tags; no volumes were pruned.

Final full backend rerun: **1,296 tests passed in 51.518 seconds**. Existing resource
warnings and the frontend large-chunk warning remain; neither is described as fixed.

The build pipeline still lacks actual candidate-image capability verification. The
proposal in `../specs/workload-build-verification.md` defines independent package-byte
measurement, isolated ephemeral database probing, durable cleanup and atomic publication.
A package manifest verifier by itself is only an integrity primitive, not that full
pipeline or runtime proof.

No production deployment, actual queue-job execution, scheduler business execution,
or real workload-container acceptance is established by this review.
