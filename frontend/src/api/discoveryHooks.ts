import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { usePagedQuery } from "./pagedHooks";
import type {
  AdoptBody,
  AdoptSuggestedResult,
  DiscoveredDomain,
  DiscoveryState,
  NameLookup,
  ZonesResult,
} from "./discoveryTypes";
import type { Domain } from "./adminTypes";
import type { EnvironmentName } from "./types";

const stateKey = ["discovery", "state"] as const;

export function useDiscoveryState() {
  return useQuery({
    queryKey: stateKey,
    queryFn: ({ signal }) => api.get<DiscoveryState>("/api/domains/discovery", signal),
  });
}

export function useDiscovered(apiQuery: string) {
  return usePagedQuery<DiscoveredDomain>(
    ["discovery", "list"],
    "/api/domains/discovered",
    apiQuery,
  );
}

function useRefreshDiscoveryViews() {
  const client = useQueryClient();
  return async () => {
    await client.invalidateQueries({ queryKey: ["discovery"] });
    await client.invalidateQueries({ queryKey: ["domains"] });
  };
}

/** Reads Cloudflare and Apache again (read-only) and replaces the discovered list. */
export function useRefreshDiscovery() {
  const refresh = useRefreshDiscoveryViews();
  return useMutation({
    mutationFn: () => api.post<DiscoveryState>("/api/domains/discovery/refresh"),
    onSuccess: refresh,
  });
}

const envDomains = (slug: string, env: EnvironmentName) =>
  `/api/projects/${encodeURIComponent(slug)}/environments/${env}/domains`;

/** Links a discovered domain to a project environment; DNS and Apache are not touched. */
export function useAdoptDomain() {
  const refresh = useRefreshDiscoveryViews();
  return useMutation({
    mutationFn: (input: { slug: string; env: EnvironmentName; body: AdoptBody }) =>
      api.post<Domain>(`${envDomains(input.slug, input.env)}/adopt`, input.body),
    onSuccess: refresh,
  });
}

export function useAdoptSuggested() {
  const refresh = useRefreshDiscoveryViews();
  return useMutation({
    mutationFn: (hostnames?: string[]) =>
      api.post<AdoptSuggestedResult>(
        "/api/domains/adopt-suggested",
        hostnames ? { hostnames } : {},
      ),
    onSuccess: refresh,
  });
}

/** Where the zone list and the name lookup are asked: one environment, or (wizard) globally. */
export interface DomainScope {
  slug?: string;
  env?: EnvironmentName;
}

function scopePath(scope: DomainScope): string {
  return scope.slug && scope.env ? envDomains(scope.slug, scope.env) : "/api/domains";
}

/** The Cloudflare zones for the pickers of the "new domain" forms. */
export function useDomainZones(scope: DomainScope, enabled = true) {
  return useQuery({
    queryKey: ["zones", scope.slug ?? "", scope.env ?? ""] as const,
    queryFn: ({ signal }) => api.get<ZonesResult>(`${scopePath(scope)}/zones`, signal),
    enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

/** What already exists for a host name: the conflict warning. Only asks for a complete name. */
export function useNameLookup(scope: DomainScope, hostname: string, enabled: boolean) {
  return useQuery({
    queryKey: ["name-lookup", scope.slug ?? "", scope.env ?? "", hostname] as const,
    queryFn: ({ signal }) =>
      api.get<NameLookup>(
        `${scopePath(scope)}/lookup?hostname=${encodeURIComponent(hostname)}`,
        signal,
      ),
    enabled,
    staleTime: 15_000,
    retry: false,
  });
}
