# Database backups to S3

Jendock's worker creates one daily dump for each configured database connection in production.
Development environments are skipped unless a project maintainer enables **Include this
development environment in daily backups** on its Database page. The schedule uses the
configured hour in `Africa/Cairo` (default 03:00). Scheduled jobs and **Back up now** run one
connection at a time across the entire worker. A manual request waits behind the active backup;
a second manual request for the same connection is rejected while its record is running.

## Configure S3

In **Settings → Integrations → S3 backups**, enter an AWS access key ID and secret access key,
bucket, region, prefix, and schedule hour. The secret key is write-only and encrypted by the
worker. Keep the key in a dedicated IAM user and grant only the generated policy. The policy
allows object upload, read-back verification, retention deletion within the configured prefix,
and listing keys under that prefix. Use **Test connection** to put, list, read, and delete a
temporary object before enabling the schedule.

The default destination is `s3://aic-consultation/backups/`. Dump client images are configurable
in the same section; defaults are `mysql:8.4`, `mariadb:11.4`, and `postgres:17`.

## Object format and retention

Objects are named:

```text
<prefix>/<project>/<environment>/<connection>/<YYYY-MM-DD>T<HHMMSS>.sql.gz
<prefix>/<project>/<environment>/<connection>/<YYYY-MM-DD>T<HHMMSS>.dump
```

The timestamp is the run's start time in `Africa/Cairo`; it gives every run its own object key,
including manual re-runs on the same day. After S3 confirms the new object is non-empty, Jendock
records the run as succeeded, then removes other objects from that connection prefix only.

MySQL and MariaDB use a transaction-consistent `mysqldump`/`mariadb-dump` stream compressed with
gzip. PostgreSQL uses `pg_dump -Fc`. Uploads request S3 server-side encryption with AES-256. A
new object is retained only after the dump process exits successfully and S3 reports a non-zero
object size. Then Jendock removes older keys from that exact connection directory, keeping one
object. If retention cleanup fails after verification, the new object remains and the run stays
succeeded with a cleanup error recorded and notified; older objects are preserved. Failed dumps
and uploads are recorded and notified, and do not replace or delete existing objects.

## Restore

For MySQL or MariaDB, download and decompress the SQL dump, then import it into the intended
database:

```sh
aws s3 cp s3://aic-consultation/backups/<project>/<environment>/<connection>/<timestamp>.sql.gz .
gunzip -c <timestamp>.sql.gz | mysql -h <host> -u <user> -p <database>
```

For PostgreSQL, restore the custom-format dump into an existing database. Create the database
first if it does not exist:

```sh
aws s3 cp s3://aic-consultation/backups/<project>/<environment>/<connection>/<timestamp>.dump .
createdb -h <host> -U <user> <database>
pg_restore --clean --no-owner -d <database> <timestamp>.dump
```

Review the target host and database carefully before restoring; `--clean` removes matching
objects in the target database.
