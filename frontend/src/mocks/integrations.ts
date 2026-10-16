/**
 * Mock of the Integrations and MCP endpoints. Shapes follow docs/api-contract.md and the routers
 * in backend/api/routers/integrations.py and mcp_tools.py. The token is write-only here too:
 * the mock keeps a flag, never the value, and no response contains it.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type {
  CloudflareStatus,
  CloudflareTestResult,
  DeployKeyStatus,
  GithubIntegration,
  IngressReason,
  IngressSettingsValues,
  McpTool,
} from "../api/integrationTypes";
import { MOCK_CSRF, projects } from "./data";
import type { MockContext } from "./admin";

const MINUTE = 60_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();

export interface IntegrationsMockState {
  tokenConfigured: boolean;
  /** When true, Cloudflare answers the test with "rejected". */
  tokenRejected: boolean;
  enabled: boolean;
  helperReachable: boolean;
  stored: IngressSettingsValues;
  defaults: IngressSettingsValues;
  zones: string[];
  webhookSecret: boolean;
  keys: Map<string, string>;
  /** Everything a test may want to assert was never rendered. */
  tokensSent: string[];
}

const NO_VALUES: IngressSettingsValues = {
  acme_email: null,
  public_ipv4: null,
  public_ipv6: null,
  acme_challenge: null,
};

function initial(): IntegrationsMockState {
  return {
    tokenConfigured: true,
    tokenRejected: false,
    enabled: true,
    helperReachable: true,
    stored: { ...NO_VALUES },
    defaults: { ...NO_VALUES, public_ipv4: "203.0.113.7", acme_challenge: "dns-01" },
    zones: ["acme.dev", "example.com"],
    webhookSecret: true,
    keys: new Map([
      [
        "acme/shop-api",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockShopApiKey jendock:acme/shop-api",
      ],
    ]),
    tokensSent: [],
  };
}

export let integrationsState: IntegrationsMockState = initial();

export function resetIntegrationsMock(): void {
  integrationsState = initial();
}

/** Tests flip the platform into the situation they need (not configured, helper down...). */
export function setIntegrationsMock(patch: Partial<IntegrationsMockState>): void {
  Object.assign(integrationsState, patch);
}

const HELPER_SOCKET = "/run/jendock-ingress/host.sock";

function effective(): IngressSettingsValues {
  const { stored, defaults } = integrationsState;
  return {
    acme_email: stored.acme_email ?? defaults.acme_email,
    public_ipv4: stored.public_ipv4 ?? defaults.public_ipv4,
    public_ipv6: stored.public_ipv6 ?? defaults.public_ipv6,
    acme_challenge: stored.acme_challenge ?? defaults.acme_challenge,
  };
}

function reasons(): IngressReason[] {
  const state = integrationsState;
  const found: IngressReason[] = [];
  if (!state.enabled) {
    found.push({
      code: "ingress_disabled",
      message:
        "Ingress is switched off on the worker (JENDOCK_INGRESS_ENABLED is not true). Start the stack with the compose.ingress.yml overlay.",
      blocking: true,
    });
  }
  if (!state.helperReachable) {
    found.push({
      code: "helper_unreachable",
      message: `Host helper: the socket ${HELPER_SOCKET} does not exist in the worker.`,
      blocking: true,
    });
  }
  if (!state.tokenConfigured) {
    found.push({
      code: "no_token",
      message: "No Cloudflare API token is stored yet.",
      blocking: true,
    });
  }
  if (!effective().acme_email) {
    found.push({
      code: "no_acme_email",
      message: "Set an ACME contact email before requesting TLS certificates.",
      blocking: false,
    });
  }
  return found;
}

function cloudflare(): CloudflareStatus {
  const state = integrationsState;
  const list = reasons();
  return {
    token_configured: state.tokenConfigured,
    token_updated_at: state.tokenConfigured ? ago(3 * 24 * 60 * MINUTE) : null,
    settings: {
      stored: { ...state.stored },
      defaults: { ...state.defaults },
      effective: effective(),
    },
    helper: {
      enabled: state.enabled,
      socket_path: HELPER_SOCKET,
      reachable: state.helperReachable,
      reason: state.helperReachable
        ? null
        : `the socket ${HELPER_SOCKET} does not exist in the worker`,
    },
    ready: !list.some((item) => item.blocking),
    reasons: list,
  };
}

function testResult(): CloudflareTestResult {
  const state = integrationsState;
  if (!state.tokenConfigured) {
    return { ok: false, status: null, zones: [], message: "no token is stored" };
  }
  if (state.tokenRejected) {
    return {
      ok: false,
      status: null,
      zones: [],
      message: "Cloudflare rejected the API token (check its scope)",
    };
  }
  return { ok: true, status: "active", zones: [...state.zones].sort(), message: "" };
}

function github(): GithubIntegration {
  const state = integrationsState;
  const keys: DeployKeyStatus[] = projects().map((project) => {
    const key = state.keys.get(project.repository) ?? null;
    return {
      slug: project.slug,
      name: project.name,
      repository: project.repository,
      git_transport: project.git_transport,
      exists: key !== null,
      public_key: key,
    };
  });
  return {
    webhook: {
      payload_url: `${window.location.origin}/api/github/webhook`,
      secret_configured: state.webhookSecret,
      counts_7d: { queued: 14, recorded: 5, ignored: 3 },
      recent: [
        {
          delivery_id: "7f1c0a52-0001",
          event: "push",
          repository: "acme/shop-api",
          outcome: "queued",
          received_at: ago(12 * MINUTE),
        },
        {
          delivery_id: "7f1c0a52-0002",
          event: "push",
          repository: "acme/marketing-site",
          outcome: "recorded",
          received_at: ago(95 * MINUTE),
        },
        {
          delivery_id: "7f1c0a52-0003",
          event: "ping",
          repository: null,
          outcome: "pong",
          received_at: ago(26 * 60 * MINUTE),
        },
      ],
    },
    deploy_keys: keys,
  };
}

const TOOLS: McpTool[] = [
  ["read", "list_projects", "List the projects with their environments.", true],
  ["read", "get_run", "Get a run: status, stages and steps.", true],
  ["read", "read_step_log", "Read a step's log from a byte offset.", true],
  ["read", "list_secret_names", "List the names of an environment's secrets (never values).", true],
  ["read", "list_backups", "List database backup history for an environment.", true],
  ["admin", "run_backup", "Queue a manual database backup.", false],
  ["run", "trigger_run", "Manual run, step 2 of 2: queue a run of the previewed commit.", false],
  ["run", "retry_run", "Queue a new run with the same commit as a finished run.", false],
  ["run", "cancel_run", "Cancel a queued run or request cancellation of a running one.", false],
  ["deploy", "redeploy", "Redeploy the images of an earlier successful deployment.", false],
  ["pipelines", "save_pipeline", "Store a new version of the environment's pipeline.", false],
  ["containers", "tail_container_logs", "Read the last lines of a container's log.", true],
  ["admin", "preview_domain_change", "Step 1 of 2: show the DNS, Apache and TLS changes.", false],
  ["admin", "apply_domain", "Step 2 of 2: apply the previewed plan.", false],
].map(([scope, name, description, read_only]) => ({
  scope: String(scope),
  name: String(name),
  description: String(description),
  read_only: Boolean(read_only),
}));

export function integrationHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const guard = (request: Request, mutating: boolean): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    return null;
  };

  return [
    http.get("/api/integrations/cloudflare", ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(cloudflare());
    }),
    http.put("/api/integrations/cloudflare/token", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as { token?: string };
      const token = (body.token ?? "").trim();
      if (token === "" || /\s/.test(token) || token.length > 512) {
        return error(
          422,
          "validation_error",
          "the token must be a single value of at most 512 characters",
        );
      }
      integrationsState.tokensSent.push(token);
      integrationsState.tokenConfigured = true;
      return HttpResponse.json(cloudflare());
    }),
    http.delete("/api/integrations/cloudflare/token", ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      integrationsState.tokenConfigured = false;
      return HttpResponse.json(cloudflare());
    }),
    http.post("/api/integrations/cloudflare/test", ({ request }) => {
      return guard(request, true) ?? HttpResponse.json(testResult());
    }),
    http.put("/api/integrations/cloudflare/settings", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as Partial<IngressSettingsValues>;
      if (body.acme_email && !/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(body.acme_email)) {
        return error(422, "validation_error", "acme_email: enter a valid email address");
      }
      if (body.public_ipv4 && !/^(\d{1,3}\.){3}\d{1,3}$/.test(body.public_ipv4)) {
        return error(422, "validation_error", "public_ipv4: not a valid IPv4 address");
      }
      for (const key of Object.keys(body) as (keyof IngressSettingsValues)[]) {
        const value = body[key];
        integrationsState.stored = {
          ...integrationsState.stored,
          [key]: value === undefined || value === "" ? null : value,
        };
      }
      return HttpResponse.json(cloudflare().settings);
    }),

    http.get("/api/integrations/github", ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(github());
    }),
    http.post("/api/integrations/github/projects/:slug/deploy-key", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const project = projects().find((item) => item.slug === params.slug);
      if (!project) return error(404, "not_found", "project not found");
      if (integrationsState.keys.has(project.repository)) {
        return error(
          409,
          "conflict",
          `a deploy key for ${project.repository} already exists; rotate it from the worker`,
        );
      }
      const key = `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINew${project.slug}Key jendock:${project.repository}`;
      integrationsState.keys.set(project.repository, key);
      return HttpResponse.json({ slug: project.slug, public_key: key }, { status: 201 });
    }),

    http.get("/api/ingress/readiness", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      const list = reasons();
      return HttpResponse.json({
        ready: !list.some((item) => item.blocking),
        token_configured: integrationsState.tokenConfigured,
        reasons: list,
      });
    }),

    http.get("/api/mcp/tools", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      const scopes = (new URL(request.url).searchParams.get("scopes") ?? "")
        .split(",")
        .filter(Boolean);
      const tools = scopes.includes("admin")
        ? TOOLS
        : TOOLS.filter((t) => scopes.includes(t.scope));
      return HttpResponse.json({
        endpoint: `${window.location.origin}/mcp`,
        enabled: true,
        tools,
      });
    }),
  ];
}
