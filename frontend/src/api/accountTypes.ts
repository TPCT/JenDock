/** Wire types of the account, audit-log and dashboard endpoints (docs/api-contract.md). */
import type { EnvironmentName, RunStatus, RunTrigger } from "./types";

export interface Profile {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  created_at: string;
  two_factor_enabled: boolean;
}

export interface UpdateProfileBody {
  display_name: string | null;
  email: string | null;
}

export interface AccountSession {
  id: number;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  ip_address: string | null;
  user_agent: string | null;
  /** "Firefox 130 on Linux". */
  client: string;
  current: boolean;
}

export interface TwoFactorStatus {
  enabled: boolean;
  enabled_at: string | null;
  recovery_codes_left: number;
}

export interface TwoFactorEnrollment {
  /** Base32 key for manual entry; shown once. */
  secret: string;
  /** `otpauth://totp/...`, rendered as a QR code in the browser. */
  uri: string;
}

export interface RecoveryCodes {
  recovery_codes: string[];
}

/** Login answer for users with two-factor: the password was right, a code is still needed. */
export interface MfaRequired {
  mfa_required: true;
  challenge: string;
}

// ---- audit log -----------------------------------------------------------------------------

export interface AuditEvent {
  id: number;
  occurred_at: string;
  actor_type: "user" | "token" | "system";
  actor_id: string | null;
  actor: string;
  action: string;
  description: string;
  target_type: string;
  target_id: string | null;
  details: Record<string, unknown>;
}

export interface AuditPage {
  items: AuditEvent[];
  next_cursor: string | null;
}

/** Filters of the audit log; empty strings mean "any". */
export interface AuditFilters {
  q: string;
  actor_type: string;
  action: string;
  target_type: string;
  target_id: string;
  project: string;
  since: string;
  until: string;
}

export const EMPTY_AUDIT_FILTERS: AuditFilters = {
  q: "",
  actor_type: "",
  action: "",
  target_type: "",
  target_id: "",
  project: "",
  since: "",
  until: "",
};

// ---- dashboard -----------------------------------------------------------------------------

export type DeploymentStatus =
  "pending" | "deploying" | "succeeded" | "failed" | "rolled_back" | "rollback_failed";

export interface DashboardDeployment {
  id: number;
  project: string;
  project_name: string;
  environment: EnvironmentName;
  status: DeploymentStatus;
  commit_sha: string;
  run_id: number;
  run_number: number;
  trigger: RunTrigger;
  triggered_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
}

export interface DashboardRun {
  run_id: number;
  project: string;
  project_name: string;
  environment: EnvironmentName;
  number: number;
  status: RunStatus;
  branch: string;
  commit_sha: string;
  trigger: RunTrigger;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
}

export interface DashboardManualDeploy {
  project: string;
  project_name: string;
  environment: EnvironmentName;
  branch: string;
  pushes: number;
  last_push_at: string;
}

export interface DashboardCertificate {
  hostname: string;
  project: string;
  environment: EnvironmentName;
  not_after: string;
  expired: boolean;
}

export interface DashboardHost {
  timestamp: string;
  cpu_percent: number | null;
  cpu_count: number;
  load_1: number;
  memory_used: number;
  memory_total: number;
  memory_percent: number;
  disk_used: number;
  disk_total: number;
  disk_percent: number;
}

export interface Dashboard {
  project_count: number;
  domain_count: number;
  deployments: DashboardDeployment[];
  attention: DashboardRun[];
  manual_deploys: DashboardManualDeploy[];
  certificates: DashboardCertificate[];
  host: DashboardHost | null;
  host_error: string | null;
}
