/**
 * Mock of the containers, telemetry and terminal-ticket endpoints. Shapes and rules follow the
 * "Containers" and "Terminal" sections of docs/api-contract.md and shiftcodes/api/routers.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type {
  ActionPlan,
  ContainerStats,
  ContainerView,
  DirectoryView,
  EnvVarView,
  HostSample,
  LifecycleAction,
  StatsSample,
} from "../api/containerTypes";
import { MOCK_CSRF, projectExists } from "./data";
import type { MockContext } from "./admin";

const MINUTE = 60_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();
const hex = (n: number): string => n.toString(16).padStart(8, "0").repeat(8);
const GIB = 1024 ** 3;

export const MOCK_WEB_ID = hex(0xa1);

function seed(): Map<string, ContainerView[]> {
  const web = (id: number, env: string): ContainerView => ({
    id: hex(id),
    name: `shop-${env}-web-1`,
    service: "web",
    image: `sc/shop-${env}-web:3f9c2ab41d0e-r12`,
    image_id: `sha256:${hex(id + 1000)}`,
    revision: "3f9c2ab41d0e5a7788b0c1d2e3f40516273849aa",
    commit: "3f9c2ab41d0e5a7788b0c1d2e3f40516273849aa",
    state: "running",
    health: "healthy",
    restart_count: 0,
    started_at: ago(26 * 60 * MINUTE),
    exit_code: null,
    cpus: 1,
    memory_bytes: 512 * 1024 ** 2,
    ports: [{ container_port: 8080, host_ip: "127.0.0.1", host_port: 20001 }],
    mounts: [{ type: "volume", source: "shop-data", destination: "/data", read_only: false }],
  });
  const map = new Map<string, ContainerView[]>();
  map.set("shop-api/development", [
    web(0xa1, "development"),
    {
      ...web(0xa2, "development"),
      name: "shop-development-worker-1",
      service: "worker",
      image: "sc/shop-development-worker:3f9c2ab41d0e-r12",
      health: null,
      restart_count: 3,
      started_at: ago(12 * MINUTE),
      ports: [],
      mounts: [],
    },
    {
      ...web(0xa3, "development"),
      name: "shop-development-cache-1",
      service: "cache",
      image: "redis:7",
      revision: null,
      commit: null,
      state: "exited",
      health: null,
      started_at: null,
      exit_code: 137,
      cpus: null,
      memory_bytes: null,
      ports: [],
      mounts: [],
    },
  ]);
  map.set("shop-api/production", [web(0xb1, "production")]);
  return map;
}

let containers = seed();
const confirmations = new Map<string, { id: string; action: LifecycleAction }>();
let overrides = new Map<string, Record<string, string>>();
let counter = 0;

export function resetContainersMock(): void {
  containers = seed();
  confirmations.clear();
  overrides = new Map();
  counter = 0;
}

const key = (slug: unknown, env: unknown) => `${String(slug)}/${String(env)}`;
const list = (slug: unknown, env: unknown): ContainerView[] => containers.get(key(slug, env)) ?? [];
const find = (slug: unknown, env: unknown, id: unknown) =>
  list(slug, env).find((item) => item.id === id);

function envOf(container: ContainerView): EnvVarView[] {
  const extra = overrides.get(container.id) ?? {};
  const base: EnvVarView[] = [
    { name: "NODE_ENV", sensitive: false, overridden: false, masked_value: "********" },
    { name: "PORT", sensitive: false, overridden: false, masked_value: "********" },
    { name: "DATABASE_URL", sensitive: true, overridden: false, masked_value: "********" },
    { name: "SESSION_SECRET", sensitive: true, overridden: false, masked_value: "********" },
  ];
  for (const name of Object.keys(extra)) {
    const existing = base.find((item) => item.name === name);
    if (existing) existing.overridden = true;
    else base.push({ name, sensitive: false, overridden: true, masked_value: "********" });
  }
  return base;
}

const TREE: Record<string, DirectoryView["entries"]> = {
  "/": [
    { name: "app", kind: "directory" },
    { name: "data", kind: "directory" },
    { name: "etc", kind: "directory" },
  ],
  "/app": [
    { name: "public", kind: "directory" },
    { name: "package.json", kind: "file" },
    { name: "server.js", kind: "file" },
    { name: "logo.png", kind: "file" },
  ],
  "/app/public": [{ name: "index.html", kind: "file" }],
  "/data": [],
  "/etc": [{ name: "hosts", kind: "file" }],
};

const FILES: Record<string, { text: string | null; size: number }> = {
  "/app/package.json": { text: '{\n  "name": "shop-api",\n  "version": "1.0.0"\n}\n', size: 48 },
  "/app/server.js": {
    text: "const port = process.env.PORT;\nconsole.log(`listening on ${port}`);\n".repeat(40),
    size: 2600,
  },
  "/app/logo.png": { text: null, size: 18_321 },
  "/app/public/index.html": { text: "<!doctype html>\n<title>Shop</title>\n", size: 36 },
  "/etc/hosts": { text: "127.0.0.1 localhost\n", size: 20 },
};

const CREDENTIAL_LIKE = /(^\.env|\.pem$|\.key$|id_rsa|shadow)/;

function sampleAt(index: number, total: number): StatsSample {
  const wave = Math.sin(index / 9);
  const memory = 180 * 1024 ** 2 + Math.round(wave * 30 * 1024 ** 2) + index * 40_000;
  return {
    timestamp: new Date(Date.now() - (total - index) * 15_000).toISOString(),
    cpu_percent: Math.max(0.5, 12 + wave * 9 + (index % 7)),
    memory_used: memory,
    memory_limit: 512 * 1024 ** 2,
    memory_percent: (memory / (512 * 1024 ** 2)) * 100,
    net_rx: index * 9000,
    net_tx: index * 4000,
    block_read: 0,
    block_write: index * 100,
    pids: 12,
  };
}

function stats(): ContainerStats {
  const history = Array.from({ length: 60 }, (_, index) => sampleAt(index, 60));
  return { latest: history[history.length - 1] as StatsSample, history };
}

function hostSample(index: number, total: number): HostSample {
  const wave = Math.sin(index / 11);
  return {
    timestamp: new Date(Date.now() - (total - index) * 15_000).toISOString(),
    cpu_percent: index === 0 ? null : 20 + wave * 12,
    cpu_count: 4,
    load_1: 0.8 + wave * 0.3,
    load_5: 0.7,
    load_15: 0.6,
    memory_total: 8 * GIB,
    memory_used: 3.4 * GIB + wave * 0.2 * GIB,
    memory_available: 4.2 * GIB,
    swap_total: 2 * GIB,
    swap_free: 2 * GIB,
    disk_total: 120 * GIB,
    disk_used: 74 * GIB,
    disk_free: 46 * GIB,
  };
}

const LOG_LINES = [
  "GET /healthz 200 2ms",
  "listening on 8080",
  "connected to the database",
  "POST /orders 201 48ms",
  "WARN slow query (312ms) on orders",
  "token=****** rejected for user 4",
];

function logLines(count: number): { timestamp: string; text: string }[] {
  return Array.from({ length: count }, (_, index) => ({
    timestamp: new Date(Date.now() - (count - index) * 4000).toISOString(),
    text: `${LOG_LINES[index % LOG_LINES.length] ?? ""} #${index + 1}`,
  }));
}

export function containerHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const guard = (request: Request, mutating: boolean, slug: unknown): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    return projectExists(String(slug)) ? null : error(404, "not_found", "project not found");
  };
  const BASE = "/api/projects/:slug/environments/:env/containers";
  const missing = () => error(404, "not_found", "container not found");

  return [
    http.get("/api/telemetry/host", () => {
      if (!ctx.authenticated()) return ctx.unauthenticated();
      const history = Array.from({ length: 60 }, (_, index) => hostSample(index, 60));
      return HttpResponse.json({ latest: history[history.length - 1], history });
    }),

    http.get(
      BASE,
      ({ params, request }) =>
        guard(request, false, params.slug) ?? HttpResponse.json(list(params.slug, params.env)),
    ),
    http.get(`${BASE}/:id`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      return found ? HttpResponse.json(found) : missing();
    }),

    http.post(`${BASE}/:id/actions`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      if (!found) return missing();
      const { action } = (await request.json()) as { action: LifecycleAction };
      const running = found.state === "running";
      if (action === "start" && running)
        return error(409, "conflict", "the container is already running");
      if (action === "stop" && !running)
        return error(409, "conflict", "the container is not running");
      const confirmation = `confirm-${String(counter++)}-${action}`;
      confirmations.set(confirmation, { id: found.id, action });
      const effects: Record<LifecycleAction, string[]> = {
        start: [`Starts ${found.name} (service ${found.service}).`],
        stop: [
          `Stops ${found.name} (service ${found.service}); it gets 10 seconds to shut down cleanly.`,
          "Anything it serves is unavailable until you start it again.",
        ],
        restart: [
          `Stops and starts ${found.name} (service ${found.service}).`,
          "Requests in flight are dropped; it is unavailable for a few seconds.",
        ],
      };
      const plan: ActionPlan = {
        action,
        container: found,
        effects: effects[action],
        confirmation,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
      };
      return HttpResponse.json(plan);
    }),
    http.post(`${BASE}/:id/actions/confirm`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      if (!found) return missing();
      const { confirmation } = (await request.json()) as { confirmation: string };
      const pending = confirmations.get(confirmation);
      if (pending?.id !== found.id) {
        return error(422, "validation_error", "the confirmation is not valid or has expired");
      }
      confirmations.delete(confirmation);
      const previous = found.state;
      found.state = pending.action === "stop" ? "exited" : "running";
      found.started_at = found.state === "running" ? new Date().toISOString() : found.started_at;
      return HttpResponse.json({
        action: pending.action,
        previous_state: previous,
        container: found,
      });
    }),

    http.get(`${BASE}/:id/logs`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      if (!find(params.slug, params.env, params.id)) return missing();
      const tail = Number(new URL(request.url).searchParams.get("tail") ?? 200);
      const lines = logLines(Math.min(tail, 60));
      return HttpResponse.json({
        lines,
        cursor: { since: lines[lines.length - 1]?.timestamp ?? "", skip: 1 },
        dropped: 0,
      });
    }),

    http.get(`${BASE}/:id/env`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      return found ? HttpResponse.json(envOf(found)) : missing();
    }),
    http.post(`${BASE}/:id/env/:name/reveal`, ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      if (!found) return missing();
      const name = String(params.name);
      if (!envOf(found).some((item) => item.name === name))
        return error(404, "not_found", "variable not found");
      const value = overrides.get(found.id)?.[name] ?? `value-of-${name}`;
      return HttpResponse.json({ name, value });
    }),
    http.put(`${BASE}/:id/env`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      if (!found) return missing();
      const body = (await request.json()) as { set?: Record<string, string>; remove?: string[] };
      const set = body.set ?? {};
      if (Object.values(set).some((value) => value.includes("BREAK"))) {
        return error(
          409,
          "conflict",
          "the service did not start with the new values; the previous configuration was restored",
        );
      }
      const current = { ...(overrides.get(found.id) ?? {}), ...set };
      for (const name of body.remove ?? []) Reflect.deleteProperty(current, name);
      const replaced: ContainerView = {
        ...found,
        id: hex(0xc0 + counter++),
        started_at: new Date().toISOString(),
      };
      overrides.set(replaced.id, current);
      const all = list(params.slug, params.env);
      all[all.indexOf(found)] = replaced;
      return HttpResponse.json({
        service: found.service,
        restarted: found.state === "running",
        containers: [replaced],
      });
    }),

    http.put(`${BASE}/:id/limits`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      if (!found) return missing();
      const body = (await request.json()) as { cpus?: number; memory?: string };
      if (body.cpus === undefined && body.memory === undefined) {
        return error(422, "validation_error", "set cpus or memory");
      }
      if (body.cpus !== undefined && body.cpus > 8) {
        return error(422, "validation_error", "cpus is above the server maximum of 8");
      }
      if (body.memory !== undefined) {
        const match = /^(\d+)([mg])$/i.exec(body.memory);
        if (!match) return error(422, "validation_error", "memory must look like 512m or 2g");
        const bytes = Number(match[1]) * (match[2]?.toLowerCase() === "g" ? 1024 ** 3 : 1024 ** 2);
        if (bytes > 16 * 1024 ** 3)
          return error(422, "validation_error", "memory is above the server maximum of 16g");
        found.memory_bytes = bytes;
      }
      if (body.cpus !== undefined) found.cpus = body.cpus;
      return HttpResponse.json(found);
    }),

    http.get(`${BASE}/:id/files`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      if (!find(params.slug, params.env, params.id)) return missing();
      const path = new URL(request.url).searchParams.get("path") ?? "/";
      const entries = TREE[path];
      if (!entries)
        return error(422, "validation_error", "that folder does not exist or may not be listed");
      return HttpResponse.json({
        path,
        entries: entries.filter((entry) => !CREDENTIAL_LIKE.test(entry.name)),
        truncated: path === "/data",
      });
    }),
    http.get(`${BASE}/:id/files/content`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      if (!find(params.slug, params.env, params.id)) return missing();
      const url = new URL(request.url);
      const path = url.searchParams.get("path") ?? "";
      const maxBytes = Number(url.searchParams.get("max_bytes") ?? 65_536);
      if (CREDENTIAL_LIKE.test(path.split("/").pop() ?? "")) {
        return error(422, "validation_error", "that file may hold credentials and cannot be shown");
      }
      const file = FILES[path];
      if (!file) return error(422, "validation_error", "that is not a regular file");
      if (file.text === null) {
        return HttpResponse.json({
          path,
          size: file.size,
          truncated: false,
          binary: true,
          text: null,
        });
      }
      const text = file.text.slice(0, maxBytes);
      return HttpResponse.json({
        path,
        size: file.size,
        truncated: text.length < file.text.length,
        binary: false,
        text,
      });
    }),

    http.get(`${BASE}/:id/stats`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      if (!found) return missing();
      if (found.state !== "running") return error(409, "conflict", "the container is not running");
      return HttpResponse.json(stats());
    }),

    http.post(`${BASE}/:id/terminal/tickets`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const found = find(params.slug, params.env, params.id);
      if (!found) return missing();
      const body = (await request.json().catch(() => ({}))) as { terminal_id?: string };
      if (!body.terminal_id && found.state !== "running") {
        return error(409, "conflict", "the container is not running");
      }
      return HttpResponse.json({
        ticket: `ticket-${String(counter++)}`,
        expires_at: new Date(Date.now() + 30_000).toISOString(),
        websocket_path: "/api/terminal/ws",
        container_id: found.id,
      });
    }),
  ];
}
