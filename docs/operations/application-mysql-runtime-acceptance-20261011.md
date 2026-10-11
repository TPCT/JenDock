# MySQL and MariaDB managed application runtime acceptance

The real local fixture passed separately on MySQL 8.4 and MariaDB 11.4 using
`tests/live_application_mysql.py`. Each run used two independently adopted database
containers, encrypted application credentials, and two managed Python HTTP
applications in the `deploy-monitor` Compose project. No production service,
provider API, business database or repository was modified.

Both engine runs verified:

- Real database-only discovery and adoption, including persistent named storage.
- Runtime application binding and actual HTTP write/read through the dedicated
  application-to-database bridge, with independent database-side persistence proof.
- Managed application stop/start/restart without data loss or container replacement.
- Different Development and Production sentinels and disjoint application networks.
- Persisted registry state after closing and reopening its SQLite connection.
- A real database restart immediately after successful authentication, before the
  first attachment, is rejected without creating its bridge or durable record.
- Restarting an already attached database rejects its changed runtime identity.
- Replacing the database container rejects its changed adoption identity.
- Production remains usable while Development's identity is rejected.
- After revoking the recorded Production credential, disabling the attachment and
  stopping its database, stopping the application succeeds; starting it rejects.
- Exact owned-resource cleanup and unchanged pre-existing container IDs, images
  and running states. Fixture application images and named volumes were removed.

Machine-readable evidence, including checked source hashes and immutable image
IDs, is in `application-mysql-runtime-20261011.json` and
`application-mariadb-runtime-20261011.json`.

Discovery fixes required for this flow select the installed `mysql` or `mariadb`
client, recognize repository image digests, and obtain the configured database
name through authenticated fixed SQL. Environment variables alone are not
accepted as current database evidence. Names, ports and storage are validated.

Validation: **1,355 backend tests passed** in the existing project Python
environment. Independent multi-agent review corrected generated application
source, initial database readiness and partial-provision cleanup before execution.

## Boundaries

The applications are real Python HTTP services provisioned through production
runtime APIs. Their artifact publication/bootstrap is synthetic; this does not
prove GitHub-triggered checkout, image build/test, migrations or automatic cutover.
The helper is the previously recorded offline source overlay on an existing
immutable dependency image, not a newly built release image. This is not Laravel
queue/scheduler, UI/browser, public ingress, MCP-client or deployed production
acceptance. MySQL-family migrations remain unintegrated. No Git push or production
deployment occurred in this checkpoint.
