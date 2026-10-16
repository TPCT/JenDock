/** Wire types of the database inspector endpoints (docs/api-contract.md, "Database"). */

export type DbEngine = "postgresql" | "mysql" | "mariadb";
export type TlsMode = "disable" | "require";

export interface DbConnection {
  name: string;
  engine: DbEngine;
  host: string;
  port: number;
  database: string;
  username: string;
  tls_mode: TlsMode;
  network: string | null;
  auto_detected: boolean;
  password_secret: string;
  read_only: boolean;
  environment: "development" | "production";
  /** Set on production: the read-only-user recommendation. */
  notice: string | null;
  created_at: string;
  updated_at: string;
  last_tested_at: string | null;
  last_test_ok: boolean | null;
}

export interface CreateConnectionBody {
  name: string;
  engine: DbEngine;
  host: string;
  port?: number;
  database: string;
  username: string;
  password: string;
  tls_mode: TlsMode;
}

export type UpdateConnectionBody = Partial<Omit<CreateConnectionBody, "name">>;

export type TestErrorCode =
  | "connect_failed"
  | "network_denied"
  | "network_unavailable"
  | "secret_missing"
  | "helper_unavailable"
  | "timeout"
  | (string & {});

export interface TestResult {
  ok: boolean;
  server: {
    version: string;
    read_only: boolean;
    current_user: string;
    database: string;
    tls: boolean;
  } | null;
  error: { code: TestErrorCode; message: string } | null;
  network: string;
}

export interface DetectResult {
  found: number;
  removed: string[];
  kept: { name: string; reason: string }[];
  results: {
    service: string;
    container: string;
    image: string;
    engine: DbEngine | null;
    status: "created" | "existing" | "skipped";
    reason: string | null;
    connection: DbConnection | null;
    test: TestResult | null;
  }[];
}

export interface SchemaInfo {
  name: string;
  system: boolean;
}

export type TableKind =
  "table" | "view" | "materialized_view" | "partitioned_table" | "foreign_table" | "sequence";

export interface TableInfo {
  schema: string;
  name: string;
  kind: TableKind;
  approx_rows: number | null;
}

export interface ColumnInfo {
  name: string;
  position: number;
  data_type: string;
  nullable: boolean;
  default: string | null;
  primary_key_position: number | null;
  extra: string | null;
}

export interface IndexInfo {
  name: string;
  columns: string[];
  unique: boolean;
  primary: boolean;
  method: string | null;
}

export interface ForeignKeyInfo {
  name: string;
  columns: string[];
  ref_schema: string;
  ref_table: string;
  ref_columns: string[];
  on_update: string | null;
  on_delete: string | null;
}

export interface TableDetail {
  table: string;
  columns: ColumnInfo[];
  indexes: IndexInfo[];
  foreign_keys: ForeignKeyInfo[];
}

export interface TruncatedCell {
  truncated: true;
  text: string;
  limit: number;
}
export interface BinaryCell {
  binary: true;
  hex: string;
  bytes: number;
}
export type Cell = string | number | boolean | null | TruncatedCell | BinaryCell;

export interface ResultColumn {
  name: string;
  type: string;
}

export interface RowsPage {
  columns: ResultColumn[];
  rows: Cell[][];
  limit: number;
  offset: number;
  ordering: "primary_key" | "columns" | "none";
  order_by: string[];
  next_cursor: string | null;
  next_offset: number | null;
  size_limited: boolean;
}

export interface QueryResult {
  columns: ResultColumn[];
  rows: Cell[][];
  row_count: number;
  truncated: boolean;
  size_limited: boolean;
  limit: number;
  elapsed_ms: number;
  statement_kind: "SELECT" | "WITH" | "SHOW" | "EXPLAIN";
}
