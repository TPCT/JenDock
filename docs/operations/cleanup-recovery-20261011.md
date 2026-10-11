# Durable image cleanup review — 2026-10-11

Cleanup is persisted separately from deployment status. Immutable job/kind requests
use leases and bounded retries, including reconstruction of terminal-job cleanup
after a crash. Legacy previous images are captured by immutable ID before replacement.
Recovery journals and open database transactions defer external cleanup effects.
Protection is recomputed for every attempt; removal is never forced and volumes
are not pruned. Historical jobs without a managed target are inapplicable only
when no managed-delivery evidence exists; corrupt managed targets remain pending.

The public job projection exposes only kind, state and attempt count. The React run
screen reports pending/running/completed cleanup independently of build success.
Absent records do not imply completed cleanup.

Verification: 1089 backend tests passed, including 12 cleanup recovery regressions;
89 frontend tests passed and the production frontend build passed. The existing
large-chunk Vite warning remains. `git diff --check` passed.

Browser review reused the local product on port 19191. A single disposable local
job-27 cleanup observation traversed pending, running and succeeded through the
actual public API; the run screen updated without manual refresh. Desktop and
390px mobile spacing were inspected. The observation was removed afterward.
Screenshot: `.runtime/spacing-review/cleanup-mobile.png`.

That UI fixture did not execute Docker cleanup. Actual Docker cleanup acceptance
and production deployment remain unverified; successful unit tests are not a claim
that live images were deleted. The broader migration executor integration is also
separate work.

## Actual Docker follow-through

The disposable real Docker fixture found an inventory defect: untagged images
were omitted by the default `image ls` query. All three cleanup inventory paths
now use `--all`. `tests/live_image_cleanup.py` explicitly verifies untagged images,
actual artifact-catalog protection, one injected removal failure, persisted
backoff after SQLite reconnect, successful removal on retry, unchanged successful
deployment status, and no extra removal on completed replay. Final session 23674
exited 0; evidence is `/tmp/live-image-cleanup-untagged.log`.
Exact fixture teardown preserved pre-existing images/container identities.
Builder-cache pruning and volume operations were forbidden in this fixture;
actual builder-cache cleanup remains separate from this image-removal proof.
