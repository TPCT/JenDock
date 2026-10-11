# MySQL/MariaDB direct adapter acceptance — 2026-10-11

The private `database_mysql.py` adapter passed real local engine acceptance. It is
not yet connected to the production inspector entrypoint, trusted candidate
resolver, authenticated HTTP workflow, or helper image. Those integration steps
remain required; this report does not mark the complete database inspector done.

## Verified run

- Executable: `tests/live_database_mysql.py`, explicit `--run`.
- Final process: 23421, exit 0.
- Local evidence: `/tmp/deploy-monitor-mysql-live-final.log`.
- Adapter SHA256: `e9e381cfd1e671f2af0d2f72608073de71b664237d11a2247287d7950309aeba`.
- Driver: PyMySQL 1.1.2, installed only in temporary test dependency directory
  `/tmp/deploy-monitor-mysql-driver-112`.
- MySQL observed version: 8.4.11; immutable image
  `sha256:6ea90827b1100f8f2ae306a539f86d2c264a26ed435a2a9f75551dd5c3aeb242`.
- MariaDB observed version: 11.4.13-MariaDB-ubu2404; immutable image
  `sha256:1292844148b311e4ed4300022a996d39083f415a963e970cf47cad1b3b18e3a6`.

Each engine ran against separate synthetic development and production fixture
contexts, sequentially to bound memory. These are fixture labels, not customer
production databases. All containers belonged to Compose project `deploy-monitor`
and carried an exact run ownership label. No existing database was contacted or
modified.

## Actual checks

For all four cases, the fixture verified authenticated account identity,
schema/table catalog pagination, column structure, primary keys and foreign-key
references, two row pages of sizes 2 and 1, unsigned bigint keys above 2^63,
SQL-bounded 256-character previews, truncation, NULL and empty strings. A cursor
from another environment context was rejected.

A SELECT-only account could read metadata and received the explicit
`row_lock_privilege_required` error for rows. Adding UPDATE to the synthetic
inspector account caused privilege rejection; the grant was removed afterward.
No adapter operation creates accounts or grants permissions.

The concurrent DDL checks used actual independent engine connections:

1. Replace the base table with a view containing `SLEEP` between the initial
   catalog read and lock acquisition. The fresh catalog check rejected the view
   before the adapter sent its row SELECT.
2. Attempt `RENAME TABLE` from another connection after the adapter acquired its
   READ lock. The fixture observed the second connection waiting on a metadata
   lock, read the original row while that wait persisted, then confirmed DDL
   completed after the adapter released the lock.

The engine contract deliberately uses SELECT plus database-scoped LOCK TABLES
for rows. It does not claim plain SELECT alone provides this namespace fence.
Locks have bounded acquisition time; row SELECTs have engine-specific deadlines,
and the fixture connector bounds socket operations. A READ lock can briefly
block writers. Cluster administrators remain trusted.

## Defect found and corrected

The initial real run passed MySQL but failed the MariaDB constraint assertion.
The adapter joined KEY_COLUMN_USAGE with TABLE_CONSTRAINTS. MariaDB's
TABLE_CONSTRAINTS implementation requires a non-SELECT table privilege, while
KEY_COLUMN_USAGE admits SELECT. The corrected fixed query reads key evidence
directly from KEY_COLUMN_USAGE, without granting broader privileges or parsing
DDL text. A regression failed before the fix and passed afterward.

Reference: official MariaDB 11.4 `sql/sql_show.cc`,
`get_schema_constraints_record` and `get_schema_key_column_usage_record`:
https://github.com/MariaDB/server/blob/11.4/sql/sql_show.cc

Sixteen focused adapter unit tests passed after the correction.

## Cleanup and peer evidence

The final run removed only exact captured fixture container IDs after checking
the run ownership label, including their disposable anonymous volumes. It
verified no containers with the fixture ownership label remained. All original
container IDs, images, and originally running peers were preserved.

Three unrelated peers were already restarting before the fixture and remained
restarting afterward, with each restart counter increasing once. Their exact
before/after states are retained in the log. The report does not claim every
peer timestamp was unchanged. No previously stable peer transition was observed.

## Remaining support boundaries

Only the two observed engine versions above have this real acceptance evidence.
Other version families admitted by the private adapter's provisional version
check have not been accepted. Accounts with roles, unclassified authentication
or grant options, column-only grants, global/foreign-database grants, or write
privileges are rejected. Unescaped database grant wildcards are rejected.

Rows currently require ordinary InnoDB tables, a single integer primary key,
explicit selected-column policy, and supported scalar columns. Generated selected
columns, views, unsupported types, and other pagination shapes are rejected.
Binary-cell presentation, broader engines and privilege models, helper transport,
public authorization, binding persistence, and browser acceptance remain separate
work. Cluster identity is truthfully reported as unverified.
