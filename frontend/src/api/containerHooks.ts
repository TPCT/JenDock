import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { usePagedQuery } from "./pagedHooks";
import type {
  ActionPlan,
  ActionResult,
  ContainerStats,
  ContainerView,
  DirectoryView,
  EnvUpdateResult,
  EnvVarView,
  FileView,
  HostTelemetry,
  LifecycleAction,
  LogPage,
  TerminalTicket,
} from "./containerTypes";
import type { EnvironmentName } from "./types";

const base = (slug: string, env: EnvironmentName) =>
  `/api/projects/${encodeURIComponent(slug)}/environments/${env}/containers`;
const one = (slug: string, env: EnvironmentName, id: string) => `${base(slug, env)}/${id}`;

export const containerKeys = {
  list: (slug: string, env: EnvironmentName) => ["containers", slug, env] as const,
  one: (slug: string, env: EnvironmentName, id: string) => ["containers", slug, env, id] as const,
};

export function containerStreamUrl(
  slug: string,
  env: EnvironmentName,
  id: string,
  tail: number,
): string {
  return `${one(slug, env, id)}/logs/stream?tail=${tail}`;
}

export function useContainers(slug: string, env: EnvironmentName, apiQuery: string) {
  return usePagedQuery<ContainerView>(containerKeys.list(slug, env), base(slug, env), apiQuery, {
    refetchInterval: 10_000,
  });
}

export function useContainer(slug: string, env: EnvironmentName, id: string) {
  return useQuery({
    queryKey: containerKeys.one(slug, env, id),
    queryFn: ({ signal }) => api.get<ContainerView>(one(slug, env, id), signal),
    refetchInterval: 10_000,
    retry: false,
  });
}

function useRefreshContainers(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: containerKeys.list(slug, env) });
  };
}

/** Step one: nothing changes, the answer says what will happen and holds the confirmation. */
export function useRequestAction(slug: string, env: EnvironmentName, id: string) {
  return useMutation({
    mutationFn: (action: LifecycleAction) =>
      api.post<ActionPlan>(`${one(slug, env, id)}/actions`, { action }),
  });
}

export function useConfirmAction(slug: string, env: EnvironmentName, id: string) {
  const refresh = useRefreshContainers(slug, env);
  return useMutation({
    mutationFn: (confirmation: string) =>
      api.post<ActionResult>(`${one(slug, env, id)}/actions/confirm`, { confirmation }),
    onSettled: refresh,
  });
}

export function useLogsOnce(
  slug: string,
  env: EnvironmentName,
  id: string,
  tail: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["container-logs", slug, env, id, tail] as const,
    queryFn: ({ signal }) => api.get<LogPage>(`${one(slug, env, id)}/logs?tail=${tail}`, signal),
    enabled,
    retry: false,
  });
}

export function useContainerEnv(slug: string, env: EnvironmentName, id: string) {
  return useQuery({
    queryKey: ["container-env", slug, env, id] as const,
    queryFn: ({ signal }) => api.get<EnvVarView[]>(`${one(slug, env, id)}/env`, signal),
  });
}

/** Not cached: a revealed value must not linger. */
export function useRevealEnv(slug: string, env: EnvironmentName, id: string) {
  return useMutation({
    mutationFn: (name: string) =>
      api.post<{ name: string; value: string }>(
        `${one(slug, env, id)}/env/${encodeURIComponent(name)}/reveal`,
      ),
    gcTime: 0,
  });
}

export function useUpdateEnv(slug: string, env: EnvironmentName, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { set: Record<string, string>; remove: string[] }) =>
      api.put<EnvUpdateResult>(`${one(slug, env, id)}/env`, body),
    onSettled: async () => {
      // exact: the old container id no longer exists after a successful edit, so its own query
      // must not refetch (it would answer 404 and flash "not found"); the new id loads fresh.
      await client.invalidateQueries({ queryKey: containerKeys.list(slug, env), exact: true });
    },
  });
}

export function useUpdateLimits(slug: string, env: EnvironmentName, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { cpus?: number; memory?: string }) =>
      api.put<ContainerView>(`${one(slug, env, id)}/limits`, body),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: containerKeys.one(slug, env, id) });
      await client.invalidateQueries({ queryKey: containerKeys.list(slug, env) });
    },
  });
}

export function useDirectory(slug: string, env: EnvironmentName, id: string, path: string) {
  return useQuery({
    queryKey: ["container-files", slug, env, id, path] as const,
    queryFn: ({ signal }) =>
      api.get<DirectoryView>(
        `${one(slug, env, id)}/files?path=${encodeURIComponent(path)}`,
        signal,
      ),
    retry: false,
    placeholderData: keepPreviousData,
  });
}

export function useFile(
  slug: string,
  env: EnvironmentName,
  id: string,
  path: string | null,
  maxBytes: number,
) {
  return useQuery({
    queryKey: ["container-file", slug, env, id, path, maxBytes] as const,
    queryFn: ({ signal }) =>
      api.get<FileView>(
        `${one(slug, env, id)}/files/content?path=${encodeURIComponent(path ?? "")}&max_bytes=${maxBytes}`,
        signal,
      ),
    enabled: path !== null,
    retry: false,
  });
}

export function useContainerStats(
  slug: string,
  env: EnvironmentName,
  id: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["container-stats", slug, env, id] as const,
    queryFn: ({ signal }) => api.get<ContainerStats>(`${one(slug, env, id)}/stats`, signal),
    enabled,
    refetchInterval: 15_000,
    retry: false,
  });
}

export function useHostTelemetry() {
  return useQuery({
    queryKey: ["host-telemetry"] as const,
    queryFn: ({ signal }) => api.get<HostTelemetry>("/api/telemetry/host", signal),
    refetchInterval: 15_000,
    retry: false,
  });
}

export function requestTerminalTicket(
  slug: string,
  env: EnvironmentName,
  id: string,
  terminalId: string | null,
): Promise<TerminalTicket> {
  return api.post<TerminalTicket>(
    `${one(slug, env, id)}/terminal/tickets`,
    terminalId === null ? {} : { terminal_id: terminalId },
  );
}
