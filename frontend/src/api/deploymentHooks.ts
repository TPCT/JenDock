import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { Deployment, RedeployResult } from "./deploymentTypes";
import { usePagedQuery } from "./pagedHooks";
import type { EnvironmentName } from "./types";

const base = (slug: string, env: EnvironmentName) =>
  `/api/projects/${encodeURIComponent(slug)}/environments/${env}/deployments`;

export const deploymentKeys = {
  list: (slug: string, env: EnvironmentName) => ["deployments", slug, env] as const,
};

export function useDeployments(slug: string, env: EnvironmentName, apiQuery: string) {
  return usePagedQuery<Deployment>(deploymentKeys.list(slug, env), base(slug, env), apiQuery, {
    refetchInterval: 10_000,
  });
}

/** Queues a run that deploys the images of an earlier successful deployment again. */
export function useRedeploy(slug: string, env: EnvironmentName) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (deploymentId: number) =>
      api.post<RedeployResult>(`${base(slug, env)}/${String(deploymentId)}/redeploy`),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: deploymentKeys.list(slug, env) });
      await client.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}
