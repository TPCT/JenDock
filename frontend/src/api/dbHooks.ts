import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  CreateConnectionBody,
  DbConnection,
  DetectResult,
  QueryResult,
  RowsPage,
  SchemaInfo,
  TableDetail,
  TableInfo,
  TestResult,
  UpdateConnectionBody,
} from "./dbTypes";
import type { EnvironmentName } from "./types";

const base = (slug: string, env: EnvironmentName) =>
  `/api/projects/${encodeURIComponent(slug)}/environments/${env}/databases`;
const conn = (slug: string, env: EnvironmentName, name: string) =>
  `${base(slug, env)}/${encodeURIComponent(name)}`;
const table = (slug: string, env: EnvironmentName, name: string, schema: string, tbl: string) =>
  `${conn(slug, env, name)}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(tbl)}`;

const listKey = (slug: string, env: EnvironmentName) => ["databases", slug, env] as const;

export function useConnections(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: listKey(slug, env),
    queryFn: ({ signal }) => api.get<DbConnection[]>(base(slug, env), signal),
  });
}

export function useConnection(slug: string, env: EnvironmentName, name: string) {
  return useQuery({
    queryKey: [...listKey(slug, env), name] as const,
    queryFn: ({ signal }) => api.get<DbConnection>(conn(slug, env, name), signal),
    retry: false,
  });
}

function useRefresh(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: listKey(slug, env) });
  };
}

export function useCreateConnection(slug: string, env: EnvironmentName) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: (body: CreateConnectionBody) => api.post<DbConnection>(base(slug, env), body),
    onSuccess: refresh,
  });
}

export function useUpdateConnection(slug: string, env: EnvironmentName, name: string) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: (body: UpdateConnectionBody) =>
      api.patch<DbConnection>(conn(slug, env, name), body),
    onSuccess: refresh,
  });
}

export function useDeleteConnection(slug: string, env: EnvironmentName) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: (name: string) => api.delete<undefined>(conn(slug, env, name)),
    onSuccess: refresh,
  });
}

/** Always answers 200; `ok: false` carries the reason. */
export function useTestConnection(slug: string, env: EnvironmentName) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: (name: string) => api.post<TestResult>(`${conn(slug, env, name)}/test`),
    onSettled: refresh,
  });
}

export function useDetectDatabases(slug: string, env: EnvironmentName) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: (prune = false) => api.post<DetectResult>(`${base(slug, env)}/detect?prune=${prune}`),
    onSuccess: refresh,
  });
}

export function useSchemas(slug: string, env: EnvironmentName, name: string, system: boolean) {
  return useQuery({
    queryKey: ["db-schemas", slug, env, name, system] as const,
    queryFn: ({ signal }) =>
      api.get<{ schemas: SchemaInfo[] }>(
        `${conn(slug, env, name)}/schemas?include_system=${String(system)}`,
        signal,
      ),
    retry: false,
  });
}

export function useTables(slug: string, env: EnvironmentName, name: string, schema: string | null) {
  return useQuery({
    queryKey: ["db-tables", slug, env, name, schema] as const,
    queryFn: ({ signal }) =>
      api.get<{ tables: TableInfo[] }>(
        `${conn(slug, env, name)}/schemas/${encodeURIComponent(schema ?? "")}/tables`,
        signal,
      ),
    enabled: schema !== null,
    retry: false,
  });
}

export function useTableDetail(
  slug: string,
  env: EnvironmentName,
  name: string,
  schema: string | null,
  tbl: string | null,
) {
  return useQuery({
    queryKey: ["db-table", slug, env, name, schema, tbl] as const,
    queryFn: ({ signal }) =>
      api.get<TableDetail>(table(slug, env, name, schema ?? "", tbl ?? ""), signal),
    enabled: schema !== null && tbl !== null,
    retry: false,
  });
}

export function useRows(
  slug: string,
  env: EnvironmentName,
  name: string,
  schema: string | null,
  tbl: string | null,
  paging: { limit: number; cursor: string | null; offset: number },
  enabled: boolean,
) {
  const query = new URLSearchParams({ limit: String(paging.limit), offset: String(paging.offset) });
  if (paging.cursor !== null) query.set("cursor", paging.cursor);
  return useQuery({
    queryKey: ["db-rows", slug, env, name, schema, tbl, paging] as const,
    queryFn: ({ signal }) =>
      api.get<RowsPage>(
        `${table(slug, env, name, schema ?? "", tbl ?? "")}/rows?${query.toString()}`,
        signal,
      ),
    enabled: enabled && schema !== null && tbl !== null,
    retry: false,
    placeholderData: keepPreviousData,
  });
}

export function useRunQuery(slug: string, env: EnvironmentName, name: string) {
  return useMutation({
    mutationFn: (statement: string) =>
      api.post<QueryResult>(`${conn(slug, env, name)}/query`, { statement }),
  });
}
