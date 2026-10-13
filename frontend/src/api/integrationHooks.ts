import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  CloudflareStatus,
  CloudflareTestResult,
  DeployKeyStatus,
  GithubIntegration,
  IngressReadiness,
  IngressSettingsValues,
  IngressValues,
  McpTools,
} from "./integrationTypes";
import { useDomainZones, type DomainScope } from "./discoveryHooks";
import type { EnvironmentName } from "./types";

const cloudflareKey = ["integrations", "cloudflare"] as const;
const githubKey = ["integrations", "github"] as const;
const zonesKey = ["zones"] as const;

export function useCloudflare() {
  return useQuery({
    queryKey: cloudflareKey,
    queryFn: ({ signal }) => api.get<CloudflareStatus>("/api/integrations/cloudflare", signal),
  });
}

function useRefreshCloudflare() {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: ["integrations"] });
    await client.invalidateQueries({ queryKey: ["ingress-readiness"] });
    await client.invalidateQueries({ queryKey: zonesKey });
  };
}

/** The token is write-only: it goes up here and never comes back. */
export function useSetCloudflareToken() {
  const refresh = useRefreshCloudflare();
  return useMutation({
    mutationFn: (token: string) =>
      api.put<CloudflareStatus>("/api/integrations/cloudflare/token", { token }),
    onSuccess: refresh,
  });
}

export function useClearCloudflareToken() {
  const refresh = useRefreshCloudflare();
  return useMutation({
    mutationFn: () => api.delete<CloudflareStatus>("/api/integrations/cloudflare/token"),
    onSuccess: refresh,
  });
}

export function useTestCloudflare() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<CloudflareTestResult>("/api/integrations/cloudflare/test"),
    onSuccess: (result) => {
      client.setQueryData(zonesKey, result);
    },
  });
}

/**
 * The zones the stored token can see: the result of the Integrations "Test connection",
 * cached for ten minutes so opening a form does not call Cloudflare every time.
 */
export function useCloudflareZones(enabled: boolean) {
  return useQuery({
    queryKey: zonesKey,
    queryFn: () => api.post<CloudflareTestResult>("/api/integrations/cloudflare/test"),
    enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

export type IngressSettingsPatch = Partial<IngressSettingsValues>;

export function useSaveIngressSettings() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (patch: IngressSettingsPatch) =>
      api.put<IngressValues>("/api/integrations/cloudflare/settings", patch),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: cloudflareKey });
    },
  });
}

export function useIngressReadiness(slug: string, env: EnvironmentName) {
  return useQuery({
    queryKey: ["ingress-readiness", slug, env] as const,
    queryFn: ({ signal }) =>
      api.get<IngressReadiness>(
        `/api/ingress/readiness?project=${encodeURIComponent(slug)}&environment=${env}`,
        signal,
      ),
  });
}

export function useGithubIntegration() {
  return useQuery({
    queryKey: githubKey,
    queryFn: ({ signal }) => api.get<GithubIntegration>("/api/integrations/github", signal),
  });
}

export function useCreateDeployKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) =>
      api.post<Pick<DeployKeyStatus, "slug" | "public_key">>(
        `/api/integrations/github/projects/${encodeURIComponent(slug)}/deploy-key`,
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: githubKey });
    },
  });
}

export function useMcpTools(scopes: readonly string[]) {
  const query = [...scopes].sort().join(",");
  return useQuery({
    queryKey: ["mcp-tools", query] as const,
    queryFn: ({ signal }) =>
      api.get<McpTools>(`/api/mcp/tools?scopes=${encodeURIComponent(query)}`, signal),
    staleTime: 60_000,
  });
}

export interface ZonePicker {
  /** The Cloudflare status or the zone list is still loading. */
  loading: boolean;
  /** Whether a platform token is stored; undefined until known. */
  configured: boolean | undefined;
  zones: string[];
  /** Why the zones could not be listed although a token is stored. */
  problem: string | null;
}

/**
 * Zones for the host name pickers. With a project environment the zones come from that
 * environment's own endpoint (a maintainer may use it); without one (the New Project wizard,
 * which only administrators reach) from the stored token's test connection.
 */
export function useZonePicker(scope: DomainScope = {}): ZonePicker {
  const scoped = scope.slug !== undefined && scope.env !== undefined;
  const status = useCloudflare();
  const configured = status.data?.token_configured;
  const global = useCloudflareZones(!scoped && configured === true);
  const own = useDomainZones(scope, scoped);
  if (scoped) {
    return {
      loading: own.isPending,
      configured: own.data?.configured,
      zones: own.data?.zones ?? [],
      problem: own.data?.configured && own.data.message !== "" ? own.data.message : null,
    };
  }
  const failed = global.data && !global.data.ok ? global.data.message : null;
  const broken = global.isError ? global.error.message : null;
  return {
    loading: status.isPending || (configured === true && global.isPending),
    configured,
    zones: global.data?.ok ? global.data.zones : [],
    problem: failed ?? broken,
  };
}
