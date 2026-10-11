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
No schedules or business jobs are run by this fixture.

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
