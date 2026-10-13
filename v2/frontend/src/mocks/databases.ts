/**
 * Mock of the database inspector endpoints (docs/api-contract.md "Database",
 * shiftcodes/api/routers/databases.py): connections, metadata, keyset-paged rows and the
 * read-only console, with the same 409/422/403 rules.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type { MockContext } from "./admin";
import type {
  Cell,
  CreateConnectionBody,
  DbConnection,
  QueryResult,
  ResultColumn,
  RowsPage,
  TableDetail,
  TableInfo,
  TestResult,
} from "../api/dbTypes";
import { MOCK_CSRF, projectExists } from "./data";

const DAY = 86_400_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();
export const PROD_NOTICE =
  "Use a dedicated database user that only has SELECT privileges. Read-only sessions are defence in depth; the user is the strongest guarantee.";

function seed(): Map<string, DbConnection[]> {
  const make = (environment: "development" | "production"): DbConnection => ({
    name: "main",
    engine: "postgresql",
    host: "db",
    port: 5432,
    database: "shop",
    username: "reader",
    tls_mode: "disable",
    password_secret: "DBCONN_MAIN_PASSWORD",
    read_only: true,
    environment,
    notice: environment === "production" ? PROD_NOTICE : null,
    created_at: ago(20 * DAY),
    updated_at: ago(2 * DAY),
    last_tested_at: ago(DAY),
    last_test_ok: true,
  });
  const map = new Map<string, DbConnection[]>();
  map.set("shop-api/development", [make("development")]);
  map.set("shop-api/production", [make("production")]);
  return map;
}
let connections = seed();
export function resetDatabasesMock(): void {
  connections = seed();
}

const key = (slug: unknown, env: unknown) => `${String(slug)}/${String(env)}`;
const listOf = (slug: unknown, env: unknown) => connections.get(key(slug, env)) ?? [];

// ---- data ---------------------------------------------------------------------------------

const TABLES: TableInfo[] = [
  { schema: "public", name: "orders", kind: "table", approx_rows: 250 },
  { schema: "public", name: "customers", kind: "table", approx_rows: 1243 },
  { schema: "public", name: "audit_log", kind: "table", approx_rows: 30 },
  { schema: "public", name: "v_totals", kind: "view", approx_rows: null },
];

const DETAIL: Record<string, TableDetail> = {
  orders: {
    table: "orders",
    columns: [
      {
        name: "id",
        position: 1,
        data_type: "integer",
        nullable: false,
        default: "nextval('orders_id_seq')",
        primary_key_position: 1,
        extra: null,
      },
      {
        name: "customer_id",
        position: 2,
        data_type: "integer",
        nullable: false,
        default: null,
        primary_key_position: null,
        extra: null,
      },
      {
        name: "total",
        position: 3,
        data_type: "numeric(10,2)",
        nullable: false,
        default: "0",
        primary_key_position: null,
        extra: null,
      },
      {
        name: "notes",
        position: 4,
        data_type: "text",
        nullable: true,
        default: null,
        primary_key_position: null,
        extra: null,
      },
      {
        name: "payload",
        position: 5,
        data_type: "bytea",
        nullable: true,
        default: null,
        primary_key_position: null,
        extra: null,
      },
    ],
    indexes: [
      { name: "orders_pkey", columns: ["id"], unique: true, primary: true, method: "btree" },
      {
        name: "orders_customer_idx",
        columns: ["customer_id"],
        unique: false,
        primary: false,
        method: "btree",
      },
    ],
    foreign_keys: [
      {
        name: "orders_customer_fk",
        columns: ["customer_id"],
        ref_schema: "public",
        ref_table: "customers",
        ref_columns: ["id"],
        on_update: "NO ACTION",
        on_delete: "CASCADE",
      },
    ],
  },
  customers: {
    table: "customers",
    columns: [
      {
        name: "id",
        position: 1,
        data_type: "integer",
        nullable: false,
        default: null,
        primary_key_position: 1,
        extra: null,
      },
      {
        name: "email",
        position: 2,
        data_type: "text",
        nullable: false,
        default: null,
        primary_key_position: null,
        extra: null,
      },
    ],
    indexes: [
      { name: "customers_pkey", columns: ["id"], unique: true, primary: true, method: "btree" },
    ],
    foreign_keys: [],
  },
  audit_log: {
    table: "audit_log",
    columns: [
      {
        name: "at",
        position: 1,
        data_type: "timestamp",
        nullable: false,
        default: "now()",
        primary_key_position: null,
        extra: null,
      },
      {
        name: "event",
        position: 2,
        data_type: "text",
        nullable: false,
        default: null,
        primary_key_position: null,
        extra: null,
      },
    ],
    indexes: [],
    foreign_keys: [],
  },
  v_totals: {
    table: "v_totals",
    columns: [
      {
        name: "total",
        position: 1,
        data_type: "numeric",
        nullable: true,
        default: null,
        primary_key_position: null,
        extra: null,
      },
    ],
    indexes: [],
    foreign_keys: [],
  },
};

const ORDER_COLUMNS: ResultColumn[] = [
  { name: "id", type: "integer" },
  { name: "customer_id", type: "integer" },
  { name: "total", type: "numeric" },
  { name: "notes", type: "text" },
  { name: "payload", type: "bytea" },
];
const LONG_NOTE = "long note ".repeat(600).slice(0, 4096);

function orderRow(id: number): Cell[] {
  return [
    id,
    (id % 40) + 1,
    (id * 3.5).toFixed(2),
    id === 7
      ? { truncated: true, text: LONG_NOTE, limit: 4096 }
      : id % 5 === 0
        ? null
        : `note ${String(id)}`,
    id === 3 ? { binary: true, hex: "89504e470d0a1a0a", bytes: 18_321 } : null,
  ];
}

const encode = (value: number): string => btoa(JSON.stringify([value]));

function rowsFor(
  tbl: string,
  limit: number,
  cursor: string | null,
  offset: number,
): RowsPage | null {
  if (tbl === "orders") {
    const after = cursor ? ((JSON.parse(atob(cursor)) as number[])[0] ?? 0) : offset;
    const start = after + 1;
    const ids = Array.from({ length: Math.min(limit, 250 - after) }, (_, i) => start + i);
    const last = ids[ids.length - 1] ?? after;
    const more = last < 250;
    return {
      columns: ORDER_COLUMNS,
      rows: ids.map(orderRow),
      limit,
      offset,
      ordering: "primary_key",
      order_by: ["id"],
      next_cursor: more ? encode(last) : null,
      next_offset: more ? last : null,
      size_limited: false,
    };
  }
  if (tbl === "audit_log") {
    const total = 30;
    const rows = Array.from(
      { length: Math.max(0, Math.min(limit, total - offset)) },
      (_, i) =>
        [
          new Date(Date.UTC(2026, 0, 1, 0, offset + i)).toISOString(),
          `event ${String(offset + i + 1)}`,
        ] as Cell[],
    );
    const more = offset + limit < total;
    return {
      columns: [
        { name: "at", type: "timestamp" },
        { name: "event", type: "text" },
      ],
      rows,
      limit,
      offset,
      ordering: "none",
      order_by: [],
      next_cursor: null,
      next_offset: more ? offset + limit : null,
      size_limited: false,
    };
  }
  if (tbl === "customers") {
    return {
      columns: [
        { name: "id", type: "integer" },
        { name: "email", type: "text" },
      ],
      rows: Array.from(
        { length: Math.min(limit, 5) },
        (_, i) => [i + 1, `user${String(i + 1)}@example.com`] as Cell[],
      ),
      limit,
      offset,
      ordering: "primary_key",
      order_by: ["id"],
      next_cursor: null,
      next_offset: null,
      size_limited: false,
    };
  }
  return null;
}

const FORBIDDEN =
  /\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call)\b/i;
const DENIED_FUNCTIONS =
  /\b(pg_sleep\w*|sleep|benchmark|set_config|nextval|dblink|lo_import|pg_read_file)\s*\(/i;

function runStatement(statementRaw: string): { error: string } | { value: QueryResult } {
  const statement = statementRaw.trim().replace(/;$/, "");
  if (statement === "") return { error: "the statement is empty" };
  if (statement.includes(";")) return { error: "only one statement can be run at a time" };
  const first = /^\s*(\w+)/.exec(statement)?.[1]?.toUpperCase() ?? "";
  if (!["SELECT", "WITH", "SHOW", "EXPLAIN"].includes(first)) {
    return { error: "only SELECT, WITH, SHOW and EXPLAIN statements are allowed" };
  }
  if (first !== "SHOW" && FORBIDDEN.test(statement)) {
    return { error: "the statement contains a keyword that could change data" };
  }
  if (/\binto\b/i.test(statement)) return { error: "SELECT ... INTO is not allowed" };
  if (/explain\s+analy[sz]e/i.test(statement))
    return { error: "EXPLAIN ANALYZE would run the statement and is not allowed" };
  if (DENIED_FUNCTIONS.test(statement))
    return { error: "the statement uses a function that is not allowed" };
  if (/\bslow\b/i.test(statement))
    return { error: "the statement timed out after 5 seconds and was cancelled" };
  const kind = first as QueryResult["statement_kind"];
  if (/\bbig\b/i.test(statement)) {
    return {
      value: {
        columns: [{ name: "n", type: "integer" }],
        rows: Array.from({ length: 500 }, (_, i) => [i + 1]),
        row_count: 500,
        truncated: true,
        size_limited: false,
        limit: 500,
        elapsed_ms: 41,
        statement_kind: kind,
      },
    };
  }
  if (/\borders\b/i.test(statement)) {
    return {
      value: {
        columns: ORDER_COLUMNS,
        rows: [3, 7, 8].map(orderRow),
        row_count: 3,
        truncated: false,
        size_limited: false,
        limit: 500,
        elapsed_ms: 12,
        statement_kind: kind,
      },
    };
  }
  return {
    value: {
      columns: [{ name: "?column?", type: "integer" }],
      rows: [[1]],
      row_count: 1,
      truncated: false,
      size_limited: false,
      limit: 500,
      elapsed_ms: 3,
      statement_kind: kind,
    },
  };
}

// ---- handlers -----------------------------------------------------------------------------

export function databaseHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const guard = (request: Request, mutating: boolean, slug: unknown): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    return projectExists(String(slug)) ? null : error(404, "not_found", "project not found");
  };
  const BASE = "/api/projects/:slug/environments/:env/databases";
  const find = (slug: unknown, env: unknown, name: unknown) =>
    listOf(slug, env).find((item) => item.name === name);
  const missing = () => error(404, "not_found", "connection not found");

  return [
    http.get(
      BASE,
      ({ params, request }) =>
        guard(request, false, params.slug) ?? HttpResponse.json(listOf(params.slug, params.env)),
    ),
    http.post(BASE, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const body = (await request.json()) as CreateConnectionBody;
      if (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(body.name)) {
        return error(422, "validation_error", "a name uses lowercase letters, digits and dashes");
      }
      if (/^(localhost|127\.|0\.0\.0\.0|169\.254\.)/.test(body.host)) {
        return error(
          422,
          "validation_error",
          "loopback, unspecified and link-local hosts are refused",
        );
      }
      if (!body.password) return error(422, "validation_error", "the password must not be empty");
      const list = listOf(params.slug, params.env);
      if (list.some((item) => item.name === body.name)) {
        return error(409, "conflict", `a connection called ${body.name} already exists`);
      }
      if (list.length >= 10)
        return error(409, "conflict", "an environment can have at most 10 connections");
      const env = params.env === "production" ? "production" : "development";
      const now = new Date().toISOString();
      const created: DbConnection = {
        name: body.name,
        engine: body.engine,
        host: body.host,
        port: body.port ?? (body.engine === "postgresql" ? 5432 : 3306),
        database: body.database,
        username: body.username,
        tls_mode: body.tls_mode,
        password_secret: `DBCONN_${body.name.toUpperCase().replaceAll("-", "_")}_PASSWORD`,
        read_only: true,
        environment: env,
        notice: env === "production" ? PROD_NOTICE : null,
        created_at: now,
        updated_at: now,
        last_tested_at: null,
        last_test_ok: null,
      };
      connections.set(key(params.slug, params.env), [...list, created]);
      return HttpResponse.json(created, { status: 201 });
    }),
    http.get(`${BASE}/:name`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.name);
      return found ? HttpResponse.json(found) : missing();
    }),
    http.patch(`${BASE}/:name`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.name);
      if (!found) return missing();
      const body = (await request.json()) as Record<string, unknown>;
      const { password, ...rest } = body;
      if (password === "") return error(422, "validation_error", "the password must not be empty");
      Object.assign(found, rest, { updated_at: new Date().toISOString(), last_test_ok: null });
      return HttpResponse.json(found);
    }),
    http.delete(`${BASE}/:name`, ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const list = listOf(params.slug, params.env);
      const found = list.find((item) => item.name === params.name);
      if (!found) return missing();
      list.splice(list.indexOf(found), 1);
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(`${BASE}/:name/test`, ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.name);
      if (!found) return missing();
      const network = `${String(params.slug)}-${String(params.env)}_default`;
      let result: TestResult;
      if (found.host.includes("gateway")) {
        result = {
          ok: false,
          server: null,
          network,
          error: {
            code: "network_denied",
            message: "the host resolves outside the environment network",
          },
        };
      } else if (found.host.includes("nowhere")) {
        result = {
          ok: false,
          server: null,
          network,
          error: { code: "connect_failed", message: "connection refused" },
        };
      } else if (found.username === "locked") {
        result = {
          ok: false,
          server: null,
          network,
          error: {
            code: "connect_failed",
            message: "password authentication failed for user locked",
          },
        };
      } else {
        result = {
          ok: true,
          network,
          error: null,
          server: {
            version: found.engine === "postgresql" ? "PostgreSQL 16.3" : "MariaDB 11.4",
            read_only: true,
            current_user: found.username,
            database: found.database,
            tls: found.tls_mode === "require",
          },
        };
      }
      found.last_tested_at = new Date().toISOString();
      found.last_test_ok = result.ok;
      return HttpResponse.json(result);
    }),

    http.get(`${BASE}/:name/schemas`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      if (!find(params.slug, params.env, params.name)) return missing();
      const system = new URL(request.url).searchParams.get("include_system") === "true";
      const schemas = [
        { name: "public", system: false },
        { name: "billing", system: false },
        ...(system
          ? [
              { name: "pg_catalog", system: true },
              { name: "information_schema", system: true },
            ]
          : []),
      ];
      return HttpResponse.json({ schemas });
    }),
    http.get(`${BASE}/:name/schemas/:schema/tables`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      if (!find(params.slug, params.env, params.name)) return missing();
      if (params.schema === "billing") return HttpResponse.json({ tables: [] });
      if (params.schema !== "public") return error(404, "not_found", "schema not found");
      return HttpResponse.json({ tables: TABLES });
    }),
    http.get(`${BASE}/:name/schemas/:schema/tables/:table`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      if (!find(params.slug, params.env, params.name)) return missing();
      const detail = DETAIL[String(params.table)];
      return detail ? HttpResponse.json(detail) : error(404, "not_found", "table not found");
    }),
    http.get(`${BASE}/:name/schemas/:schema/tables/:table/rows`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      if (!find(params.slug, params.env, params.name)) return missing();
      const url = new URL(request.url);
      const limit = Number(url.searchParams.get("limit") ?? 100);
      if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
        return error(422, "validation_error", "limit must be between 1 and 500");
      }
      const page = rowsFor(
        String(params.table),
        limit,
        url.searchParams.get("cursor"),
        Number(url.searchParams.get("offset") ?? 0),
      );
      return page ? HttpResponse.json(page) : error(404, "not_found", "table not found");
    }),
    http.post(`${BASE}/:name/query`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.name);
      if (!found) return missing();
      const { statement } = (await request.json()) as { statement: string };
      if (statement.length > 20_000)
        return error(422, "validation_error", "the statement is longer than 20,000 characters");
      if (found.host.includes("nowhere"))
        return error(409, "conflict", "the database is not reachable");
      const result = runStatement(statement);
      return "error" in result
        ? error(422, "validation_error", result.error)
        : HttpResponse.json(result.value);
    }),
  ];
}
