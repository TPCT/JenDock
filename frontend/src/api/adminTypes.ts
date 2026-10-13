/** Wire types of the project-setup, secrets, tokens and domains endpoints (see docs/api-contract.md). */
import type { EnvironmentName } from "./types";

export type GitTransport = "ssh" | "https";

export interface CreateProjectBody {
  slug: string;
  name: string;
  repository: string;
  development_branch: string;
  production_branch: string;
  git_transport: GitTransport;
}

export interface PipelineTemplate {
  name: string;
  title: string;
  description: string;
  jenkinsfile: string;
}

export interface SecretInfo {
  name: string;
  created_at: string;
  updated_at: string;
}

export const TOKEN_SCOPES = [
  "read",
  "run",
  "deploy",
  "containers",
  "terminal",
  "pipelines",
  "admin",
] as const;
export type TokenScope = (typeof TOKEN_SCOPES)[number];

export interface TokenInfo {
  id: number;
  name: string;
  prefix: string;
  scopes: TokenScope[];
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface CreatedToken {
  token: string;
  info: TokenInfo;
}

export interface CreateTokenBody {
  name: string;
  scopes: TokenScope[];
  expires_at: string | null;
}

// ---- domains -----------------------------------------------------------------------------

export type DnsRecordType = "A" | "AAAA" | "CNAME";
export type DomainStatus = "draft" | "active" | "error";
export type DomainOrigin = "managed" | "adopted";
export type CertificateState = "ok" | "renewal_due" | "warning" | "critical" | "expired";

export interface Certificate {
  not_after: string;
  days_remaining: number;
  state: CertificateState;
  last_attempt_at: string | null;
  last_error: string | null;
}

export interface CheckResult {
  ok: boolean;
  detail: string;
  checked_at: string;
  /** The part could not be checked (no token, no host helper); it does not fail the check. */
  skipped?: boolean;
  proxied?: boolean;
  record_type?: string;
  file?: string;
  upstream_port?: number | null;
  ssl?: boolean;
  addresses?: string[];
  expected?: string[];
  status_code?: number;
  certificate_not_after?: string;
  port?: number;
}

export interface Check {
  checked_at: string;
  ok: boolean;
  /** `vhost` is only present for adopted domains (Apache is not Jendock's to plan). */
  results: { dns: CheckResult; https: CheckResult; backend: CheckResult; vhost?: CheckResult };
}

export interface Domain {
  id: number;
  hostname: string;
  service: string;
  container_port: number;
  published_port: number;
  dns_record_type: DnsRecordType;
  dns_target: string | null;
  dns_proxied: boolean;
  tls_enabled: boolean;
  status: DomainStatus;
  status_message: string | null;
  /** `adopted` domains existed before Jendock and are managed outside it. */
  origin: DomainOrigin;
  adopted_at: string | null;
  vhost_file: string | null;
  applied_at: string | null;
  last_verified_at: string | null;
  last_verified_ok: boolean | null;
  created_at: string;
  updated_at: string;
}

export interface DomainDetail extends Domain {
  certificate: Certificate | null;
  check: Check | null;
}

export interface CreateDomainBody {
  hostname: string;
  service: string;
  container_port: number;
  published_port: number;
  dns_record_type: DnsRecordType;
  dns_target?: string | null;
  dns_proxied: boolean;
  tls_enabled: boolean;
}

export interface DnsRecord {
  record_type: string;
  name: string;
  content: string;
  proxied: boolean;
}

export type PlanOperation = "apply" | "remove";

export interface Plan {
  domain_id: number;
  hostname: string;
  operation: PlanOperation;
  blocked: boolean;
  dns: {
    action: "create" | "update" | "delete" | "noop" | "conflict";
    zone: string;
    current: DnsRecord | null;
    desired: DnsRecord | null;
    removes: DnsRecord[];
    message: string;
  };
  vhost: {
    action: "create" | "update" | "delete" | "noop";
    current: string | null;
    proposed: string | null;
    diff: string;
  };
  tls: {
    action: "issue" | "keep" | "remove" | "none";
    challenge: "dns-01" | "http-01" | null;
    not_after: string | null;
  };
  steps: string[];
  plan_hash: string;
}

export interface CertificateOverview extends Certificate {
  domain_id: number;
  hostname: string;
}

export type EnvPathArgs = { slug: string; env: EnvironmentName };
