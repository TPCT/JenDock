/** Wire types of the integrations and MCP endpoints (see docs/api-contract.md). */

export interface IngressSettingsValues {
  acme_email: string | null;
  public_ipv4: string | null;
  public_ipv6: string | null;
  acme_challenge: "dns-01" | "http-01" | null;
}

export interface IngressValues {
  /** What an administrator saved; null means "use the environment default". */
  stored: IngressSettingsValues;
  defaults: IngressSettingsValues;
  effective: IngressSettingsValues;
}

export interface IngressReason {
  code: string;
  message: string;
  /** A blocking reason stops domains from being applied; the others are advice. */
  blocking: boolean;
}

export interface HelperStatus {
  enabled: boolean;
  socket_path: string;
  reachable: boolean;
  reason: string | null;
}

export interface CloudflareStatus {
  token_configured: boolean;
  token_updated_at: string | null;
  settings: IngressValues;
  /** null when the worker could not be asked. */
  helper: HelperStatus | null;
  ready: boolean;
  reasons: IngressReason[];
}

export interface CloudflareTestResult {
  ok: boolean;
  status: string | null;
  zones: string[];
  message: string;
}

export interface IngressReadiness {
  ready: boolean;
  token_configured: boolean;
  reasons: IngressReason[];
}

export interface WebhookDelivery {
  delivery_id: string;
  event: string;
  repository: string | null;
  outcome: string;
  received_at: string;
}

export interface DeployKeyStatus {
  slug: string;
  name: string;
  repository: string;
  git_transport: "ssh" | "https";
  exists: boolean;
  /** The public half only; the private key never leaves the worker. */
  public_key: string | null;
}

export interface GithubIntegration {
  webhook: {
    payload_url: string;
    /** null when the worker could not be asked. */
    secret_configured: boolean | null;
    counts_7d: Record<string, number>;
    recent: WebhookDelivery[];
  };
  deploy_keys: DeployKeyStatus[];
}

export interface McpTool {
  name: string;
  description: string;
  scope: string;
  read_only: boolean;
}

export interface McpTools {
  endpoint: string;
  enabled: boolean;
  tools: McpTool[];
}
