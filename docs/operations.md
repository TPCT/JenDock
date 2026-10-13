# Backup, restore and reproducible builds

## Backup and restore

```
python -m backend.cli backup create --to /backups [--include-logs] [--keep 7]
python -m backend.cli backup restore --from /backups/jendock-backup-<UTC>.tar.gz
python -m backend.cli backup prune --dir /backups --keep 7
```

- `create` writes `jendock-backup-<UTC timestamp>.tar.gz` (0600, built under a temporary name
  and renamed when complete). It holds a snapshot of the SQLite database taken with SQLite's
  **online backup API** (consistent while the API and worker keep writing), the managed compose
  overrides (`<data_dir>/deployments/*/override.yml[.prev]`; journals are transient and
  skipped), with `--include-logs` the per-run logs, and `manifest.json` (format version, UTC
  time, **Alembic revision**, SHA-256 and size of every file).
- The **secret key is never in the archive.** `--include-key` additionally writes it to a
  separate `<archive>.secret.key` (0600) and prints a loud warning: whoever has the key and an
  archive can decrypt every stored secret, so keep the two apart. Without the key a restored
  database cannot decrypt its secrets.
- `restore` verifies every member against the manifest (and rejects unexpected, duplicate,
  absolute or `..` paths) and requires the Alembic revision to be one this code knows (a backup
  from a newer version is refused). It writes only into an **empty** data directory and refuses
  when the data directory or the database file already has content; on any failure it removes
  what it wrote. `--key-file` puts a saved key in place (never overwriting one). Afterwards run
  `make migrate` (`python -m backend.repositories.migrate`) to bring an older backup to head.
- Scheduled backups run in the worker and are **off by default**:
  `JENDOCK_BACKUP_ENABLED=true`, `JENDOCK_BACKUP_DIR` (a mounted directory the worker
  can write), `JENDOCK_BACKUP_KEEP` (default 7, newest kept), `JENDOCK_BACKUP_HOUR_UTC`
  (default 3), `JENDOCK_BACKUP_INCLUDE_LOGS`. They never include the key. Only files named
  like our archives are pruned. Only SQLite databases are supported.

## Reproducible builds

`requirements.lock` pins runtime dependencies and is used as pip constraints in both
Dockerfiles. `make lock` regenerates it inside `python:3.13.9-slim`.
`make test-py313` runs the test suite in that image.
