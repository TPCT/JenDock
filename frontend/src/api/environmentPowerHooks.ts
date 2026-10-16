import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { EnvironmentName } from "./types";

export interface EnvironmentPower {
  state: "RUNNING" | "STOPPED" | "NOT_DEPLOYED";
  running: number;
  total: number;
  auto_stop_hours: number | null;
  last_deployed_at: string | null;
  started_at: string | null;
  auto_stop_due_at: string | null;
}

const path = (slug: string, env: EnvironmentName) =>
  `/api/projects/${encodeURIComponent(slug)}/environments/${env}/power`;
export const powerKey = (slug: string, env: EnvironmentName) =>
  ["environment-power", slug, env] as const;

export function useEnvironmentPower(slug: string, env: EnvironmentName, enabled = true) {
  return useQuery({
    queryKey: powerKey(slug, env),
    queryFn: ({ signal }) => api.get<EnvironmentPower>(path(slug, env), signal),
    retry: false,
    enabled,
  });
}

export function useEnvironmentPowerAction(
  slug: string,
  env: EnvironmentName,
  action: "start" | "stop",
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (confirm?: string) =>
      api.post<EnvironmentPower>(
        `${path(slug, env)}/${action}`,
        action === "stop" ? { confirm } : {},
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: powerKey(slug, env) });
      await client.invalidateQueries({ queryKey: ["projects"] });
      await client.invalidateQueries({ queryKey: ["resources", slug, env] });
      await client.invalidateQueries({ queryKey: ["containers", slug, env] });
    },
  });
}
