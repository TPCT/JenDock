import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { containerKeys } from "./containerHooks";
import type {
  ResourceChange,
  ResourceLimitsInput,
  ResourceOverview,
  ResourceUsage,
} from "./resourceTypes";
import type { EnvironmentName } from "./types";

const base = (slug: string, env: EnvironmentName) =>
  `/api/projects/${encodeURIComponent(slug)}/environments/${env}/resources`;

export const resourceKeys = {
  all: (slug: string, env: EnvironmentName) => ["resources", slug, env] as const,
  limits: (slug: string, env: EnvironmentName) => ["resources", slug, env, "limits"] as const,
  usage: (slug: string, env: EnvironmentName) => ["resources", slug, env, "usage"] as const,
};

/** Saved limits of every service and the host's capacity. */
export function useResources(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: resourceKeys.limits(slug, env),
    queryFn: ({ signal }) => api.get<ResourceOverview>(base(slug, env), signal),
    retry: false,
  });
}

/** Live usage of the running containers (a `docker stats --no-stream` on the server). */
export function useResourceUsage(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: resourceKeys.usage(slug, env),
    queryFn: ({ signal }) => api.get<ResourceUsage>(`${base(slug, env)}/stats`, signal),
    refetchInterval: 10_000,
    retry: false,
  });
}

function useRefresh(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: resourceKeys.all(slug, env) });
    await client.invalidateQueries({ queryKey: containerKeys.list(slug, env) });
  };
}

export function useSaveResources(slug: string, env: EnvironmentName, service: string) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: (body: ResourceLimitsInput) =>
      api.put<ResourceChange>(`${base(slug, env)}/${encodeURIComponent(service)}`, body),
    onSuccess: refresh,
  });
}

export function useRemoveResources(slug: string, env: EnvironmentName, service: string) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: (applyNow: boolean) =>
      api.delete<ResourceChange>(
        `${base(slug, env)}/${encodeURIComponent(service)}?apply_now=${String(applyNow)}`,
      ),
    onSuccess: refresh,
  });
}

export function useApplyResources(slug: string, env: EnvironmentName, service: string) {
  const refresh = useRefresh(slug, env);
  return useMutation({
    mutationFn: () =>
      api.post<ResourceChange>(`${base(slug, env)}/${encodeURIComponent(service)}/apply`),
    onSuccess: refresh,
  });
}
