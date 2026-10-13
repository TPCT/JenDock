/**
 * Mock of the notification endpoints. Shapes and rules follow docs/api-contract.md and
 * backend/api/routers/notifications.py: channel secrets are write-only (only `configured` and
 * the last 4 characters of a webhook URL come back), global-only events cannot be subscribed
 * below the default scope, and a failed test delivery is a 200 with `ok: false`.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type {
  ChannelConfig,
  ChannelKind,
  ChannelSummary,
  Delivery,
  EventInfo,
  NotificationChannel,
  NotificationPolicy,
  RuleItem,
  RuleSet,
} from "../api/notificationTypes";
import { MOCK_CSRF, projectExists } from "./data";
import type { MockContext } from "./admin";

const MINUTE = 60_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();

const EVENTS: EventInfo[] = [
  { event: "run.failed", label: "Run failed", severity: "critical", project_scoped: true },
  {
    event: "run.fixed",
    label: "Run succeeded after a failure",
    severity: "info",
    project_scoped: true,
  },
  { event: "run.interrupted", label: "Run interrupted", severity: "warning", project_scoped: true },
  {
    event: "deployment.succeeded",
    label: "Deployment succeeded",
    severity: "info",
    project_scoped: true,
  },
  {
    event: "deployment.rolled_back",
    label: "Deployment rolled back",
    severity: "critical",
    project_scoped: true,
  },
  {
    event: "deployment.rollback_failed",
    label: "Rollback failed",
    severity: "critical",
    project_scoped: true,
  },
  {
    event: "certificate.expiring",
    label: "Certificate expires within 14 days",
    severity: "warning",
    project_scoped: true,
  },
  {
    event: "certificate.renewal_failed",
    label: "Certificate renewal failed",
    severity: "critical",
    project_scoped: true,
  },
  {
    event: "webhook.signature_spike",
    label: "Webhook signature failures spike",
    severity: "warning",
    project_scoped: false,
  },
  {
    event: "disk.usage_high",
    label: "Disk usage above the threshold",
    severity: "warning",
    project_scoped: false,
  },
];

const POLICY: NotificationPolicy = {
  enabled: true,
  dedupe_minutes: 30,
  max_attempts: 7,
  disk_threshold_percent: 90,
  webhook_failure_threshold: 20,
  webhook_failure_window_minutes: 10,
  events: EVENTS,
};

function initialChannels(): NotificationChannel[] {
  return [
    {
      id: 1,
      name: "#deploys",
      kind: "slack",
      enabled: true,
      configured: true,
      hint: "x7Qa",
      summary: {},
      last_test_at: ago(2 * 60 * MINUTE),
      last_test_ok: true,
      created_at: ago(5 * 24 * 60 * MINUTE),
      updated_at: ago(5 * 24 * 60 * MINUTE),
    },
  ];
}

function initialDeliveries(): Delivery[] {
  return [
    {
      id: 2,
      channel_id: 1,
      channel_name: "#deploys",
      event: "run.failed",
      title: "Run #41 failed: shop-api/production",
      project: "shop-api",
      status: "failed",
      attempts: 7,
      suppressed: 0,
      is_test: false,
      last_error: "the endpoint answered HTTP 404",
      created_at: ago(3 * 60 * MINUTE),
      next_attempt_at: null,
      delivered_at: null,
    },
    {
      id: 1,
      channel_id: 1,
      channel_name: "#deploys",
      event: "deployment.succeeded",
      title: "Deployment succeeded: shop-api/development",
      project: "shop-api",
      status: "sent",
      attempts: 1,
      suppressed: 2,
      is_test: false,
      last_error: null,
      created_at: ago(5 * 60 * MINUTE),
      next_attempt_at: null,
      delivered_at: ago(5 * 60 * MINUTE - 3000),
    },
  ];
}

let channels = initialChannels();
let deliveries = initialDeliveries();
let nextChannelId = 10;
let nextDeliveryId = 10;
let rules = new Map<string, RuleSet>();

export function resetNotificationsMock(): void {
  channels = initialChannels();
  deliveries = initialDeliveries();
  nextChannelId = 10;
  nextDeliveryId = 10;
  rules = new Map();
}

const scopeKey = (slug: string | undefined, env: string | undefined) =>
  `${slug ?? ""}/${env ?? ""}`;

function ruleSet(slug: string | undefined, env: string | undefined): RuleSet {
  const scope = slug === undefined ? "default" : env === undefined ? "project" : "environment";
  const own = rules.get(scopeKey(slug, env));
  if (own) return own;
  let inherited: RuleSet["inherited_from"] = null;
  if (env !== undefined && (rules.get(scopeKey(slug, undefined))?.rules.length ?? 0) > 0) {
    inherited = "project";
  } else if (
    slug !== undefined &&
    (rules.get(scopeKey(undefined, undefined))?.rules.length ?? 0) > 0
  ) {
    inherited = "default";
  }
  return { scope, rules: [], inherited_from: inherited };
}

function summaryOf(kind: ChannelKind, config: ChannelConfig): ChannelSummary {
  if (kind === "webhook") return { signed: Boolean(config.signing_secret) };
  if (kind !== "email") return {};
  const security = config.security as ChannelSummary["security"];
  return {
    host: String(config.host ?? ""),
    port: Number(config.port ?? 587),
    security: security ?? "starttls",
    from_address: String(config.from_address ?? ""),
    to_addresses: (config.to_addresses as string[] | undefined) ?? [],
    authenticated: Boolean(config.username),
  };
}

function configProblem(kind: ChannelKind, config: ChannelConfig): string | null {
  if (kind === "slack") {
    return String(config.webhook_url ?? "").startsWith("https://")
      ? null
      : "webhook_url must be an https URL without credentials";
  }
  if (kind === "webhook") {
    return /^https?:\/\//.test(String(config.url ?? ""))
      ? null
      : "url must be an http(s) URL without credentials";
  }
  if (!config.host) return "host is required (at most 253 characters)";
  const to = config.to_addresses;
  if (!Array.isArray(to) || to.length === 0) return "to_addresses needs 1-10 addresses";
  if (!String(config.from_address ?? "").includes("@")) {
    return "from_address is not a valid email address";
  }
  return null;
}

function hintOf(kind: ChannelKind, config: ChannelConfig): string | null {
  const url = kind === "slack" ? config.webhook_url : kind === "webhook" ? config.url : null;
  return typeof url === "string" ? url.slice(-4) : null;
}

export function notificationHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const guard = (request: Request, mutating: boolean): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    return null;
  };
  const noProject = (slug: unknown): Response | null =>
    projectExists(String(slug)) ? null : error(404, "not_found", "project not found");
  const find = (id: unknown) => channels.find((item) => item.id === Number(id));

  const putRules = async (
    request: Request,
    slug: string | undefined,
    env: string | undefined,
  ): Promise<Response> => {
    const denied = guard(request, true) ?? (slug === undefined ? null : noProject(slug));
    if (denied) return denied;
    const body = (await request.json()) as { rules: RuleItem[] };
    const projectScoped = slug !== undefined;
    for (const item of body.rules) {
      if (!find(item.channel_id)) {
        return error(404, "not_found", `notification channel not found: ${item.channel_id}`);
      }
      for (const name of item.events) {
        const info = EVENTS.find((entry) => entry.event === name);
        if (!info) return error(422, "validation_error", `unknown event: ${name}`);
        if (projectScoped && !info.project_scoped) {
          return error(422, "validation_error", `${name} can only be set in the default rule`);
        }
      }
    }
    const scope = slug === undefined ? "default" : env === undefined ? "project" : "environment";
    if (body.rules.length === 0) rules.delete(scopeKey(slug, env));
    else rules.set(scopeKey(slug, env), { scope, rules: body.rules, inherited_from: null });
    return HttpResponse.json(ruleSet(slug, env));
  };

  return [
    http.get("/api/notifications/policy", ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(POLICY);
    }),
    http.get("/api/notifications/channels", ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(channels);
    }),
    http.post("/api/notifications/channels", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as {
        name: string;
        kind: ChannelKind;
        config: ChannelConfig;
      };
      const problem = configProblem(body.kind, body.config);
      if (problem) return error(422, "validation_error", problem);
      if (channels.some((item) => item.name === body.name)) {
        return error(409, "conflict", `a channel called ${body.name} already exists`);
      }
      const now = new Date().toISOString();
      const created: NotificationChannel = {
        id: nextChannelId++,
        name: body.name,
        kind: body.kind,
        enabled: true,
        configured: true,
        hint: hintOf(body.kind, body.config),
        summary: summaryOf(body.kind, body.config),
        last_test_at: null,
        last_test_ok: null,
        created_at: now,
        updated_at: now,
      };
      channels = [...channels, created].sort((a, b) => a.name.localeCompare(b.name));
      return HttpResponse.json(created, { status: 201 });
    }),
    http.patch("/api/notifications/channels/:id", async ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const channel = find(params.id);
      if (!channel) return error(404, "not_found", "notification channel not found");
      const body = (await request.json()) as {
        name?: string;
        enabled?: boolean;
        config?: ChannelConfig;
      };
      const updated: NotificationChannel = {
        ...channel,
        ...(body.name === undefined ? {} : { name: body.name }),
        ...(body.enabled === undefined ? {} : { enabled: body.enabled }),
        ...(body.config
          ? {
              hint: hintOf(channel.kind, body.config) ?? channel.hint,
              summary: { ...channel.summary, ...summaryOf(channel.kind, body.config) },
            }
          : {}),
        updated_at: new Date().toISOString(),
      };
      channels = channels.map((item) => (item.id === updated.id ? updated : item));
      return HttpResponse.json(updated);
    }),
    http.delete("/api/notifications/channels/:id", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      if (!find(params.id)) return error(404, "not_found", "notification channel not found");
      channels = channels.filter((item) => item.id !== Number(params.id));
      for (const [key, set] of rules) {
        rules.set(key, {
          ...set,
          rules: set.rules.filter((r) => r.channel_id !== Number(params.id)),
        });
      }
      return new HttpResponse(null, { status: 204 });
    }),
    http.post("/api/notifications/channels/:id/test", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const channel = find(params.id);
      if (!channel) return error(404, "not_found", "notification channel not found");
      const ok = !channel.name.includes("broken");
      const now = new Date().toISOString();
      const lastError = ok ? null : "the endpoint answered HTTP 404";
      deliveries = [
        {
          id: nextDeliveryId++,
          channel_id: channel.id,
          channel_name: channel.name,
          event: "run.fixed",
          title: "Test notification from Jendock",
          project: null,
          status: ok ? "sent" : "failed",
          attempts: 1,
          suppressed: 0,
          is_test: true,
          last_error: lastError,
          created_at: now,
          next_attempt_at: null,
          delivered_at: ok ? now : null,
        },
        ...deliveries,
      ];
      channels = channels.map((item) =>
        item.id === channel.id ? { ...item, last_test_at: now, last_test_ok: ok } : item,
      );
      return HttpResponse.json({ ok, error: lastError });
    }),
    http.get("/api/notifications/deliveries", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      const params = new URL(request.url).searchParams;
      const project = params.get("project");
      const limit = Number(params.get("limit") ?? 50);
      const items = deliveries.filter((item) => project === null || item.project === project);
      return HttpResponse.json({
        items: items.slice(0, limit),
        total: items.length,
        limit,
        offset: 0,
      });
    }),
    http.get("/api/notifications/rules", ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(ruleSet(undefined, undefined));
    }),
    http.put("/api/notifications/rules", ({ request }) => putRules(request, undefined, undefined)),
    http.get("/api/projects/:slug/notifications/rules", ({ params, request }) => {
      const denied = guard(request, false) ?? noProject(params.slug);
      return denied ?? HttpResponse.json(ruleSet(String(params.slug), undefined));
    }),
    http.put("/api/projects/:slug/notifications/rules", ({ params, request }) =>
      putRules(request, String(params.slug), undefined),
    ),
    http.get("/api/projects/:slug/environments/:env/notifications/rules", ({ params, request }) => {
      const denied = guard(request, false) ?? noProject(params.slug);
      return denied ?? HttpResponse.json(ruleSet(String(params.slug), String(params.env)));
    }),
    http.put("/api/projects/:slug/environments/:env/notifications/rules", ({ params, request }) =>
      putRules(request, String(params.slug), String(params.env)),
    ),
  ];
}
