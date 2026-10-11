# Isolated candidate-image fixture

This is a real Laravel 12.69.3 application bootstrap used only by the capability
verifier acceptance test. It contains no production configuration or credentials.
The script copies the package's cached vendor dependencies, installs the reviewed
package at the standard vendor/shiftcodes/deploy-monitor-workloads location and
runs Composer dump-autoload --no-scripts --no-plugins. The root fixture declares
that package namespace explicitly; it does not replace any Laravel classes or
protocol methods. No Composer install/update or network fetch is performed.

All database endpoints and account data come from the verifier's fresh fixture
variables. Conventional Laravel discovery cache files use bounded /tmp paths so
read-only candidate execution needs no host mounts or writable project root.
Artifact verification does not execute schedules or business jobs. The separate runtime
acceptance harness uses only the synthetic SentinelJob and named Fixture heartbeat
schedule against disposable databases, proving consumption, failure/retry and isolation.

Run from the repository root with `python3 tests/live_workload_artifact.py
--build-only`, then pass the returned immutable image ID to
`python3 tests/live_workload_artifact.py --image sha256:... --publication`.
The latter exercises real registry/build-profile/job publication and independently
verifies queue and scheduler artifacts with the worker executor. It intentionally
leaves `operational` false: build verification is not runtime service acceptance.

The caller must reserve a local Docker test window. The fixture checks exact peer
container IDs, images and running state before/after, and leaves its private
journal plus peer diagnostics on failure. It never deletes unrecognized resources.
Existing candidate images passed with `--image` remain available for review.

Two-environment runtime acceptance runs with the existing project Python runtime:
`.runtime/mcp-venv/bin/python tests/live_workload_runtime.py --evidence /tmp/workload-runtime.json`.
Subsequent runs can reuse its returned immutable candidate with `--image`.
It uses named disposable PostgreSQL volumes, complete Docker inventory, explicit
per-environment adoption service selection, real managed provisioning/runtime
attachment/lifecycle, and authenticated workload control plus transport.

PostgreSQL is launched using a local immutable `postgres@sha256:...` repository
digest and checked against the fixed expected image ID before launch. The current
adoption collector identifies its database engine from the configured image name;
a raw `sha256:...` image reference is not recognized as PostgreSQL by that collector.
The fixture does not bypass or weaken that boundary.
