/** Wire types of the notification endpoints (see docs/api-contract.md, "Notifications"). */

export type ChannelKind = "slack" | "webhook" | "email";
export type Severity = "info" | "warning" | "critical";
export type DeliveryStatus = "pending" | "sending" | "sent" | "failed";
export type RuleScopeName = "default" | "project" | "environment";

export interface ChannelSummary {
  /** Webhook channels: whether outgoing requests carry an HMAC signature. */
  signed?: boolean;
  /** Email channels (never the username or password). */
  host?: string;
  port?: number;
  security?: "none" | "starttls" | "tls";
  from_address?: string;
  to_addresses?: string[];
  authenticated?: boolean;
}

/** A channel as the API shows it: configured, plus the last 4 characters of a webhook URL. */
export interface NotificationChannel {
  id: number;
  name: string;
  kind: ChannelKind;
  enabled: boolean;
  configured: boolean;
  hint: string | null;
  summary: ChannelSummary;
  last_test_at: string | null;
  last_test_ok: boolean | null;
  created_at: string;
  updated_at: string;
}

/** Write-only configuration: sent to create or change a channel, never returned. */
export type ChannelConfig = Record<string, string | number | boolean | string[] | null>;

export interface CreateChannelBody {
  name: string;
  kind: ChannelKind;
  config: ChannelConfig;
}

export interface UpdateChannelBody {
  name?: string;
  enabled?: boolean;
  config?: ChannelConfig;
}

export interface TestResult {
  ok: boolean;
  error: string | null;
}

export interface EventInfo {
  event: string;
  label: string;
  severity: Severity;
  project_scoped: boolean;
}

export interface NotificationPolicy {
  enabled: boolean;
  dedupe_minutes: number;
  max_attempts: number;
  disk_threshold_percent: number;
  webhook_failure_threshold: number;
  webhook_failure_window_minutes: number;
  events: EventInfo[];
}

export interface RuleItem {
  channel_id: number;
  events: string[];
  enabled: boolean;
}

export interface RuleSet {
  scope: RuleScopeName;
  rules: RuleItem[];
  /** The wider scope that applies while this scope has no rules of its own. */
  inherited_from: "project" | "default" | null;
}

export interface Delivery {
  id: number;
  channel_id: number;
  channel_name: string;
  event: string;
  title: string;
  project: string | null;
  status: DeliveryStatus;
  attempts: number;
  suppressed: number;
  is_test: boolean;
  last_error: string | null;
  created_at: string;
  next_attempt_at: string | null;
  delivered_at: string | null;
}

export interface DeliveryPage {
  items: Delivery[];
  total: number;
  limit: number;
  offset: number;
}
