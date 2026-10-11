# Automatic migration runtime

The application images contain the coordinator; database execution runs in the
separately built `Dockerfile.migration` image. Add `compose.migration.yml` to the
existing Compose invocation. Keep all other enabled overrides in that invocation.

- `DEPLOY_MIGRATION_HELPER_IMAGE` is the full immutable `sha256:` image ID loaded
  into the worker's Docker daemon. Receiver and worker receive the same value.
  A local default-daemon build is not evidence that the production isolation
  daemon has the image. Inspect its ID using the configured Docker socket before
  enabling a migration binding.
- `DEPLOY_DATABASE_INSPECTION_KEY_FILE` points to the existing private database
  vault key. Migration accounts have a separate purpose and registry from inspector
  accounts, but share this encryption key. Never replace it during an upgrade.
  The worker receives a read-only bind with automatic host-path creation disabled.
  The receiver receives no vault key; its configuration actions go through the
  authenticated worker socket.
- `compose.database.yml` remains necessary for database inspection. It can be
  combined with this override; both refer to the same vault key and mount target.

Before rollout, render the combined Compose configuration, verify the identical
helper IDs and single read-only key mount, then inspect the image in the target
daemon. Do not print the key or account passwords. Provision a verified owned
database binding and a separate migration account in the selected environment.
Enabling that binding authorizes automatic supported migrations for new runs;
it does not retroactively change an already captured run.

Current helper support is PostgreSQL with the supported Alembic and Laravel
grammars. Unsupported migrations, unknown historical ledger hashes, or unresolved
database ownership are explicit failures, not approval requests or successful
deployments. Development reset support and broader dialect support remain separate
work. No production rollout is established by this packaging file.
