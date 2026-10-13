/** Wire types of domain discovery and adoption (see docs/api-contract.md, "Domain discovery"). */
import type { Domain, DomainOrigin } from "./adminTypes";
import type { EnvironmentName } from "./types";

export type SourceStateName = "ok" | "no_token" | "error" | "missing" | "outdated" | "unknown";
export type Confidence = "high" | "medium" | "low";
export type LinkStatus = "linked" | "suggested" | "unlinked";

export interface DiscoverySource {
  state: SourceStateName;
  message: string | null;
}

export interface DiscoveryState {
  refreshed_at: string | null;
  cloudflare: DiscoverySource;
  helper: DiscoverySource;
  server_ip: string | null;
  ip_source: "setting" | "origin" | "unknown";
  zones: string[];
  hosts: number;
  linked: number;
  suggested: number;
  /** Suggestions that are certain and complete: what "Adopt all suggested" will take. */
  adoptable: number;
}

export interface DiscoveredDns {
  record_type: string;
  content: string;
  proxied: boolean;
  zone: string | null;
}

export interface DiscoveredVhost {
  file: string | null;
  upstream_host: string | null;
  upstream_port: number | null;
  ssl: boolean | null;
  managed: boolean;
}

export interface Suggestion {
  project: string;
  environment: EnvironmentName;
  service: string | null;
  container_port: number | null;
  published_port: number | null;
  confidence: Confidence;
  reason: string;
}

export interface DomainLink {
  domain_id: number;
  project: string;
  environment: EnvironmentName;
  origin: DomainOrigin;
}

export interface DiscoveredDomain {
  hostname: string;
  zone: string | null;
  status: LinkStatus;
  dns: DiscoveredDns | null;
  points_to_server: boolean | null;
  vhost: DiscoveredVhost | null;
  suggestion: Suggestion | null;
  link: DomainLink | null;
  seen_at: string;
}

export interface AdoptBody {
  hostname: string;
  service: string;
  container_port: number;
  published_port: number;
}

export interface AdoptSuggestedResult {
  adopted: Domain[];
  skipped: { hostname: string; reason: string }[];
}

export interface ZonesResult {
  configured: boolean;
  zones: string[];
  message: string;
}

export interface ExistingRecord {
  record_type: string;
  content: string;
  proxied: boolean;
  managed_by_jendock: boolean;
}

export interface NameLookup {
  hostname: string;
  configured: boolean;
  zone: string | null;
  records: ExistingRecord[];
  in_use_by: DomainLink | null;
  message: string;
}
