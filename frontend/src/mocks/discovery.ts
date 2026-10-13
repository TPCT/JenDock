/**
 * Mock of the domain discovery and adoption endpoints (backend/api/routers/discovery.py): three
 * Cloudflare zones with about thirty records, some matched to projects (by Apache's upstream port
 * or by name), some linked already and some that match nothing. Adopting writes into the same
 * per-project domain lists the Domains tab reads, like the real server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type { DomainDetail } from "../api/adminTypes";
import type {
  AdoptBody,
  AdoptSuggestedResult,
  Confidence,
  DiscoveredDomain,
  DiscoveryState,
  NameLookup,
  SourceStateName,
  ZonesResult,
} from "../api/discoveryTypes";
import type { EnvironmentName } from "../api/types";
import { addAdoptedDomain, linkedDomains, type MockContext } from "./admin";
import { integrationsState } from "./integrations";
import { MOCK_CSRF, projectExists } from "./data";
import { listResponse, type MockList } from "./paging";

const MINUTE = 60_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();

export const MOCK_SERVER_IP = "203.0.113.7";
const ZONES = ["example.com", "example.net", "example.org"];

interface Seed {
  hostname: string;
  type?: "A" | "AAAA" | "CNAME" | null;
  content?: string;
  proxied?: boolean;
  /** Apache: file, local upstream port, HTTPS. */
  vhost?: [file: string, port: number | null, ssl: boolean];
  hint?: {
    project: string;
    env: EnvironmentName;
    service?: string;
    containerPort?: number;
    confidence: Confidence;
    reason: string;
  };
}

const PORT_REASON = (service: string, project: string, env: string, port: number): string =>
  `Apache proxies to port ${String(port)}, the published port of ${service} of ${project}/${env}`;

function certain(
  project: string,
  env: EnvironmentName,
  service: string,
  containerPort: number,
  port: number,
): NonNullable<Seed["hint"]> {
  return {
    project,
    env,
    service,
    containerPort,
    confidence: "high",
    reason: PORT_REASON(service, project, env, port),
  };
}

const NAME_REASON = (project: string): string =>
  `The host name contains the project's name (${project}); check the service and port before adopting`;

const SEEDS: Seed[] = [
  // already linked to a project in Jendock (see the Domains tab of shop-api)
  {
    hostname: "shop.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["shop.conf", 20001, true],
  },
  {
    hostname: "api.example.com",
    type: "CNAME",
    content: "shop.example.com",
    vhost: ["api.conf", 20002, true],
  },
  {
    hostname: "staging.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["staging.conf", 20003, false],
  },
  // certain: Apache's upstream port is one container's published port
  {
    hostname: "app.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["app.conf", 20021, true],
    hint: certain("shop-api", "production", "web", 8080, 20021),
  },
  {
    hostname: "marketing.example.org",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["marketing.conf", 20011, true],
    hint: certain("marketing-site", "production", "web", 80, 20011),
  },
  {
    hostname: "www.example.org",
    type: "CNAME",
    content: "marketing.example.org",
    vhost: ["marketing.conf", 20011, true],
    hint: certain("marketing-site", "production", "web", 80, 20011),
  },
  {
    hostname: "pay.example.net",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["pay.conf", 20031, true],
    hint: certain("billing-worker", "production", "api", 3000, 20031),
  },
  {
    hostname: "dev.pay.example.net",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["pay-dev.conf", 20032, true],
    hint: certain("billing-worker", "development", "api", 3000, 20032),
  },
  // by name only: the person has to say which service and port
  {
    hostname: "shop-admin.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["shop-admin.conf", 4100, true],
    hint: {
      project: "shop-api",
      env: "production",
      confidence: "medium",
      reason: NAME_REASON("shop-api"),
    },
  },
  {
    hostname: "tools.example.net",
    type: "A",
    content: MOCK_SERVER_IP,
    hint: {
      project: "internal-tools",
      env: "production",
      confidence: "low",
      reason:
        "The host name shares a word with it (internal-tools); check the service and port before adopting",
    },
  },
  // nothing matches
  {
    hostname: "blog.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["blog.conf", 8081, true],
  },
  {
    hostname: "docs.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["docs.conf", 8082, true],
  },
  {
    hostname: "status.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["status.conf", 8083, true],
  },
  {
    hostname: "grafana.example.net",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["grafana.conf", 3000, true],
  },
  {
    hostname: "wiki.example.net",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["wiki.conf", 8090, true],
  },
  {
    hostname: "git.example.net",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["git.conf", 3001, true],
  },
  {
    hostname: "ci.example.net",
    type: "AAAA",
    content: "2001:db8::7",
    vhost: ["ci.conf", 8085, true],
  },
  { hostname: "chat.example.org", type: "A", content: MOCK_SERVER_IP, proxied: true },
  {
    hostname: "files.example.org",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["files.conf", 8095, false],
  },
  { hostname: "vpn.example.org", type: "A", content: MOCK_SERVER_IP },
  {
    hostname: "demo.example.org",
    type: "A",
    content: MOCK_SERVER_IP,
    proxied: true,
    vhost: ["demo.conf", 8100, true],
  },
  { hostname: "beta.example.org", type: "CNAME", content: "demo.example.org", proxied: true },
  {
    hostname: "kb.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["kb.conf", 8110, true],
  },
  {
    hostname: "intranet.example.com",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["intranet.conf", 8120, true],
  },
  { hostname: "support.example.com", type: "A", content: MOCK_SERVER_IP, proxied: true },
  { hostname: "assets.example.net", type: "CNAME", content: "wiki.example.net", proxied: true },
  {
    hostname: "old.example.org",
    type: "A",
    content: MOCK_SERVER_IP,
    vhost: ["old.conf", 8130, false],
  },
  { hostname: "jobs.example.com", type: "A", content: MOCK_SERVER_IP },
  // Apache only: no Cloudflare record
  { hostname: "legacy.example.org", type: null, vhost: ["legacy.conf", 8140, true] },
  { hostname: "intranet-old.example.com", type: null, vhost: ["intranet-old.conf", 8150, false] },
];

export interface DiscoveryMock {
  tokenRejected: boolean;
  helper: SourceStateName;
  refreshedAt: string | null;
  /** A host name "adopt all suggested" skips with an error (to see the skipped list). */
  failBulkOn: string | null;
}

function initial(): DiscoveryMock {
  return { tokenRejected: false, helper: "ok", refreshedAt: ago(12 * MINUTE), failBulkOn: null };
}

export let discoveryState: DiscoveryMock = initial();

export function resetDiscoveryMock(): void {
  discoveryState = initial();
}

export function setDiscoveryMock(patch: Partial<DiscoveryMock>): void {
  Object.assign(discoveryState, patch);
}

function zoneOf(hostname: string): string | null {
  return ZONES.find((zone) => hostname === zone || hostname.endsWith(`.${zone}`)) ?? null;
}

function rows(): DiscoveredDomain[] {
  const linked = new Map(linkedDomains().map((item) => [item.hostname, item]));
  const helperOk = discoveryState.helper === "ok";
  return SEEDS.map((seed): DiscoveredDomain => {
    const link = linked.get(seed.hostname);
    const hint = seed.hint;
    const port = seed.vhost?.[1] ?? null;
    return {
      hostname: seed.hostname,
      zone: zoneOf(seed.hostname),
      status: link ? "linked" : hint ? "suggested" : "unlinked",
      dns: seed.type
        ? {
            record_type: seed.type,
            content: seed.content ?? MOCK_SERVER_IP,
            proxied: seed.proxied ?? false,
            zone: zoneOf(seed.hostname),
          }
        : null,
      points_to_server: seed.type ? true : null,
      vhost:
        seed.vhost && helperOk
          ? {
              file: seed.vhost[0],
              upstream_host: port ? "127.0.0.1" : null,
              upstream_port: port,
              ssl: seed.vhost[2],
              managed: false,
            }
          : null,
      suggestion: hint
        ? {
            project: hint.project,
            environment: hint.env,
            service: hint.service ?? null,
            container_port: hint.containerPort ?? null,
            published_port: hint.confidence === "high" || port ? port : null,
            confidence: hint.confidence,
            reason: hint.reason,
          }
        : null,
      link: link
        ? {
            domain_id: link.id,
            project: link.slug,
            environment: link.env as EnvironmentName,
            origin: link.origin as "managed" | "adopted",
          }
        : null,
      seen_at: discoveryState.refreshedAt ?? ago(MINUTE),
    };
  });
}

function stateNow(): DiscoveryState {
  const all = rows();
  const unlinked = all.filter((row) => row.link === null);
  const certainOnes = unlinked.filter(
    (row) => row.suggestion?.confidence === "high" && row.suggestion.service !== null,
  );
  const tokenOk = integrationsState.tokenConfigured && !discoveryState.tokenRejected;
  return {
    refreshed_at: discoveryState.refreshedAt,
    cloudflare: tokenOk
      ? { state: "ok", message: null }
      : integrationsState.tokenConfigured
        ? { state: "error", message: "Cloudflare rejected the API token (check its scope)" }
        : { state: "no_token", message: "no Cloudflare API token is stored" },
    helper:
      discoveryState.helper === "ok"
        ? { state: "ok", message: null }
        : {
            state: discoveryState.helper,
            message: "the host helper is not reachable",
          },
    server_ip: MOCK_SERVER_IP,
    ip_source: "setting",
    zones: ZONES,
    hosts: all.length,
    linked: all.length - unlinked.length,
    suggested: unlinked.filter((row) => row.suggestion !== null).length,
    adoptable: certainOnes.length,
  };
}

const LIST: MockList<DiscoveredDomain> = {
  text: (row) => [
    row.hostname,
    row.dns?.content,
    row.suggestion?.project,
    row.vhost?.upstream_host,
  ],
  filters: {
    link: (row, value) => row.status === value,
    zone: (row, value) => row.zone === value,
  },
  sorts: {
    hostname: (a, b) => a.hostname.localeCompare(b.hostname),
    zone: (a, b) =>
      (a.zone ?? "").localeCompare(b.zone ?? "") || a.hostname.localeCompare(b.hostname),
  },
  defaultSort: "hostname",
};

function adoptedFrom(row: DiscoveredDomain, body: AdoptBody): Omit<DomainDetail, "id"> {
  const now = new Date().toISOString();
  return {
    hostname: row.hostname,
    service: body.service,
    container_port: body.container_port,
    published_port: body.published_port,
    dns_record_type: (row.dns?.record_type ?? "A") as "A" | "AAAA" | "CNAME",
    dns_target: row.dns?.content ?? null,
    dns_proxied: row.dns?.proxied ?? false,
    tls_enabled: row.vhost?.ssl ?? row.dns?.proxied ?? true,
    status: "active",
    status_message: null,
    origin: "adopted",
    adopted_at: now,
    vhost_file: row.vhost?.file ?? null,
    applied_at: null,
    last_verified_at: null,
    last_verified_ok: null,
    created_at: now,
    updated_at: now,
    certificate: null,
    check: null,
  };
}

function withoutDetail(domain: DomainDetail): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(domain).filter(([key]) => key !== "certificate" && key !== "check"),
  );
}

export function discoveryHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const guard = (request: Request, mutating: boolean): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    return null;
  };

  const zones = (): ZonesResult =>
    integrationsState.tokenConfigured
      ? { configured: true, zones: ZONES, message: "" }
      : { configured: false, zones: [], message: "no Cloudflare API token is stored" };

  const lookup = (hostname: string): NameLookup => {
    const name = hostname.trim().toLowerCase();
    if (!integrationsState.tokenConfigured) {
      return {
        hostname: name,
        configured: false,
        zone: null,
        records: [],
        in_use_by: null,
        message: "no Cloudflare API token is stored",
      };
    }
    const zone = zoneOf(name);
    const seed = SEEDS.find((item) => item.hostname === name);
    const link = linkedDomains().find((item) => item.hostname === name);
    return {
      hostname: name,
      configured: true,
      zone,
      records: seed?.type
        ? [
            {
              record_type: seed.type,
              content: seed.content ?? MOCK_SERVER_IP,
              proxied: seed.proxied ?? false,
              managed_by_jendock: false,
            },
          ]
        : [],
      in_use_by: link
        ? {
            domain_id: link.id,
            project: link.slug,
            environment: link.env as EnvironmentName,
            origin: link.origin as "managed" | "adopted",
          }
        : null,
      message: zone ? "" : `no Cloudflare zone is reachable with this token for ${name}`,
    };
  };

  const adopt = (slug: string, env: string, body: AdoptBody): Response => {
    const row = rows().find((item) => item.hostname === body.hostname);
    if (!row) {
      return error(
        404,
        "not_found",
        `${body.hostname} was not found by the last discovery; refresh from Cloudflare first`,
      );
    }
    if (row.link) return error(409, "conflict", `hostname is already in use: ${body.hostname}`);
    return HttpResponse.json(withoutDetail(addAdoptedDomain(slug, env, adoptedFrom(row, body))), {
      status: 201,
    });
  };

  const envBase = "/api/projects/:slug/environments/:env/domains";
  return [
    http.get("/api/domains/discovery", ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(stateNow());
    }),
    http.post("/api/domains/discovery/refresh", ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      if (integrationsState.tokenConfigured && !discoveryState.tokenRejected) {
        discoveryState.refreshedAt = new Date().toISOString();
      }
      return HttpResponse.json(stateNow());
    }),
    http.get("/api/domains/discovered", ({ request }) => {
      return guard(request, false) ?? listResponse(rows(), request.url, LIST);
    }),
    http.post("/api/domains/adopt-suggested", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as { hostnames?: string[] };
      const result: AdoptSuggestedResult = { adopted: [], skipped: [] };
      for (const row of rows()) {
        const hint = row.suggestion;
        const certainOne =
          row.link === null &&
          hint?.confidence === "high" &&
          hint.service !== null &&
          hint.container_port !== null &&
          hint.published_port !== null;
        if (!certainOne) continue;
        if (body.hostnames && !body.hostnames.includes(row.hostname)) continue;
        if (!projectExists(hint.project)) {
          result.skipped.push({
            hostname: row.hostname,
            reason: `project not found: ${hint.project}`,
          });
          continue;
        }
        if (row.hostname === discoveryState.failBulkOn) {
          result.skipped.push({
            hostname: row.hostname,
            reason: "service is not part of the environment: web",
          });
          continue;
        }
        const created = addAdoptedDomain(
          hint.project,
          hint.environment,
          adoptedFrom(row, {
            hostname: row.hostname,
            service: hint.service ?? "web",
            container_port: hint.container_port ?? 80,
            published_port: hint.published_port ?? 80,
          }),
        );
        result.adopted.push(
          withoutDetail(created) as unknown as AdoptSuggestedResult["adopted"][number],
        );
      }
      return HttpResponse.json(result);
    }),
    http.get("/api/domains/zones", ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(zones());
    }),
    http.get("/api/domains/lookup", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      return HttpResponse.json(lookup(new URL(request.url).searchParams.get("hostname") ?? ""));
    }),
    // Registered before the domain routes of admin.ts: "zones" is not a domain id.
    http.get(`${envBase}/zones`, ({ request }) => {
      return guard(request, false) ?? HttpResponse.json(zones());
    }),
    http.get(`${envBase}/lookup`, ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      return HttpResponse.json(lookup(new URL(request.url).searchParams.get("hostname") ?? ""));
    }),
    http.post(`${envBase}/adopt`, async ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as AdoptBody;
      return adopt(String(params.slug), String(params.env), body);
    }),
  ];
}
