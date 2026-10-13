/**
 * Mock of the resource-limit endpoints. Shapes and rules follow "Resource limits" in
 * docs/api-contract.md: limits are checked against the host (4 CPUs, 8 GiB here), saved per
 * service, and "apply now" updates the mock containers the containers mock serves.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type {
  ContainerUsage,
  ResourceChange,
  ResourceLimits,
  ServiceResources,
} from "../api/resourceTypes";
import { MOCK_CSRF, projectExists } from "./data";
import type { MockContext } from "./admin";
import { containersOf } from "./containers";

const MIB = 1024 ** 2;
const GIB = 1024 ** 3;
export const MOCK_HOST = { cpus: 4, memory_bytes: 8 * GIB };

const keyOf = (slug: unknown, env: unknown, service: unknown) =>
  `${String(slug)}/${String(env)}/${String(service)}`;

/** The seeded web containers already run with 1 CPU and 512 MiB, saved like a real deploy would. */
function seed(): Map<string, { limits: ResourceLimits; updated_at: string }> {
  const web: ResourceLimits = {
    cpus: 1,
    memory_bytes: 512 * MIB,
    memory_reservation_bytes: null,
    cpu_shares: null,
  };
  const at = new Date(Date.now() - 3 * 24 * 3_600_000).toISOString();
  return new Map(
    ["development", "production"].map((env) => [
      keyOf("shop-api", env, "web"),
      { limits: web, updated_at: at },
    ]),
  );
}

let saved = seed();

export function resetResourcesMock(): void {
  saved = seed();
}

function validate(body: Record<string, unknown>): string | null {
  const cpus = body.cpus as number | null | undefined;
  const memory = body.memory_bytes as number | null | undefined;
  const reservation = body.memory_reservation_bytes as number | null | undefined;
  const shares = body.cpu_shares as number | null | undefined;
  if ([cpus, memory, reservation, shares].every((value) => value == null)) {
    return "set at least one limit, or remove the limit instead";
  }
  if (cpus != null) {
    if (cpus <= 0) return "cpus must be greater than 0";
    if (cpus > MOCK_HOST.cpus) {
      return `cpus (${String(cpus)}) cannot exceed the host's ${String(MOCK_HOST.cpus)} CPUs`;
    }
  }
  for (const [label, value] of [
    ["memory", memory],
    ["memory reservation", reservation],
  ] as const) {
    if (value == null) continue;
    if (value < 6 * MIB) return `${label} must be at least 6 MiB`;
    if (value > MOCK_HOST.memory_bytes) {
      return `${label} cannot exceed the host's ${String(MOCK_HOST.memory_bytes / GIB)} GiB of memory`;
    }
  }
  if (memory != null && reservation != null && reservation > memory) {
    return "the memory reservation cannot be larger than the memory limit";
  }
  if (shares != null && (shares < 2 || shares > 262_144)) {
    return "cpu shares must be between 2 and 262144 (default 1024)";
  }
  return null;
}

export function resourcesHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const BASE = "/api/projects/:slug/environments/:env/resources";
  const guard = (request: Request, mutating: boolean, slug: unknown): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    return projectExists(String(slug)) ? null : error(404, "not_found", "project not found");
  };
  const services = (slug: unknown, env: unknown): string[] =>
    [...new Set(containersOf(slug, env).map((item) => item.service))].sort();

  /** Copy the saved limits onto the running containers of the service. */
  const applyLive = (
    slug: unknown,
    env: unknown,
    service: string,
    limits: ResourceLimits | null,
  ): ResourceChange["applied"] =>
    containersOf(slug, env)
      .filter((item) => item.service === service && item.state === "running")
      .map((item) => {
        if (limits === null) {
          item.cpus = MOCK_HOST.cpus;
          item.memory_bytes = MOCK_HOST.memory_bytes;
        } else {
          if (limits.cpus !== null) item.cpus = limits.cpus;
          if (limits.memory_bytes !== null) item.memory_bytes = limits.memory_bytes;
        }
        return { id: item.id, name: item.name, ok: true, error: null };
      });
  const change = (
    service: string,
    limits: ResourceLimits | null,
    applied: ResourceChange["applied"],
    notes: string[] = [],
  ): ResourceChange => ({ service, limits, applied, notes });

  return [
    http.get(BASE, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      const items: ServiceResources[] = services(params.slug, params.env).map((service) => {
        const entry = saved.get(keyOf(params.slug, params.env, service));
        return {
          service,
          limits: entry?.limits ?? null,
          updated_at: entry?.updated_at ?? null,
        };
      });
      return HttpResponse.json({ host: MOCK_HOST, services: items });
    }),

    http.get(`${BASE}/stats`, ({ params, request }) => {
      const denied = guard(request, false, params.slug);
      if (denied) return denied;
      const wave = Math.sin(Date.now() / 20_000);
      const containers: ContainerUsage[] = containersOf(params.slug, params.env)
        .filter((item) => item.state === "running")
        .map((item, index) => ({
          id: item.id,
          name: item.name,
          service: item.service,
          cpu_percent: Math.max(1, 62 + wave * 25 + index * 40),
          memory_used: 210 * MIB + index * 90 * MIB,
          memory_percent: 0,
          cpu_limit: item.cpus,
          memory_limit: item.memory_bytes,
        }));
      return HttpResponse.json({ host: MOCK_HOST, containers });
    }),

    http.put(`${BASE}/:service`, async ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const service = String(params.service);
      if (!services(params.slug, params.env).includes(service)) {
        return error(404, "not_found", `service not found in this environment: ${service}`);
      }
      const body = (await request.json()) as Record<string, unknown>;
      const problem = validate(body);
      if (problem) return error(422, "validation_error", problem);
      const limits: ResourceLimits = {
        cpus: (body.cpus as number | null | undefined) ?? null,
        memory_bytes: (body.memory_bytes as number | null | undefined) ?? null,
        memory_reservation_bytes:
          (body.memory_reservation_bytes as number | null | undefined) ?? null,
        cpu_shares: (body.cpu_shares as number | null | undefined) ?? null,
      };
      saved.set(keyOf(params.slug, params.env, service), {
        limits,
        updated_at: new Date().toISOString(),
      });
      const applied =
        body.apply_now === true ? applyLive(params.slug, params.env, service, limits) : [];
      return HttpResponse.json(change(service, limits, applied));
    }),

    http.delete(`${BASE}/:service`, ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const service = String(params.service);
      const key = keyOf(params.slug, params.env, service);
      if (!saved.has(key)) {
        return error(404, "not_found", "no resource limits are saved for this service");
      }
      saved.delete(key);
      const live = new URL(request.url).searchParams.get("apply_now") === "true";
      const applied = live ? applyLive(params.slug, params.env, service, null) : [];
      const notes = live
        ? [
            "Docker cannot remove a limit from a running container, so it was raised to the host's capacity; the next deploy removes it completely.",
          ]
        : [];
      return HttpResponse.json(change(service, null, applied, notes));
    }),

    http.post(`${BASE}/:service/apply`, ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const service = String(params.service);
      const entry = saved.get(keyOf(params.slug, params.env, service));
      if (!entry) return error(404, "not_found", "no resource limits are saved for this service");
      return HttpResponse.json(
        change(service, entry.limits, applyLive(params.slug, params.env, service, entry.limits)),
      );
    }),
  ];
}
