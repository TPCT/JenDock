import { http, HttpResponse } from "msw";
import type { EnvironmentPower } from "../api/environmentPowerHooks";
import { projectExists, projects } from "./data";

const state = new Map<string, EnvironmentPower>();
const key = (slug: string, env: string) => `${slug}/${env}`;
const param = (value: string | readonly string[] | undefined) =>
  typeof value === "string" ? value : (value?.[0] ?? "");
const initial = (slug: string, env: string): EnvironmentPower => {
  const configured = projects()
    .find((project) => project.slug === slug)
    ?.environments.find((environment) => environment.name === env);
  const notDeployed = slug === "internal-tools";
  const running = env === "production";
  const startedAt = running ? new Date().toISOString() : null;
  const hours = configured?.auto_stop_hours ?? null;
  return {
    state: notDeployed ? "NOT_DEPLOYED" : running ? "RUNNING" : "STOPPED",
    running: running ? 2 : 0,
    total: notDeployed ? 0 : 2,
    auto_stop_hours: hours,
    last_deployed_at: null,
    started_at: startedAt,
    auto_stop_due_at:
      startedAt && hours ? new Date(Date.now() + hours * 60 * 60_000).toISOString() : null,
  };
};

export function environmentPowerHandlers(
  authenticated: () => boolean,
  error: (status: number, code: string, message: string) => Response,
) {
  const base = "/api/projects/:slug/environments/:env/power";
  const guard = () =>
    authenticated() ? null : error(401, "unauthenticated", "session expired or invalid");
  return [
    http.get(base, ({ params }) => {
      const denied = guard();
      if (denied) return denied;
      if (params.env !== "development" && params.env !== "production")
        return error(404, "not_found", "environment not found");
      const slug = param(params.slug);
      const env = param(params.env);
      if (!projectExists(slug)) return error(404, "not_found", "project not found");
      const current = state.get(key(slug, env)) ?? initial(slug, env);
      const hours =
        projects()
          .find((project) => project.slug === slug)
          ?.environments.find((environment) => environment.name === env)?.auto_stop_hours ?? null;
      const due =
        current.started_at && hours
          ? new Date(new Date(current.started_at).getTime() + hours * 60 * 60_000).toISOString()
          : null;
      const response = { ...current, auto_stop_hours: hours, auto_stop_due_at: due };
      state.set(key(slug, env), response);
      return HttpResponse.json(response);
    }),
    http.post(`${base}/start`, ({ params }) => {
      const denied = guard();
      if (denied) return denied;
      const id = key(String(params.slug), String(params.env));
      const current = state.get(id) ?? initial(String(params.slug), String(params.env));
      if (current.state === "NOT_DEPLOYED") return error(409, "conflict", "deploy it first");
      const started = new Date().toISOString();
      const next = {
        ...current,
        state: "RUNNING" as const,
        running: current.total,
        started_at: started,
        auto_stop_due_at: current.auto_stop_hours
          ? new Date(Date.now() + current.auto_stop_hours * 60 * 60_000).toISOString()
          : null,
      };
      state.set(id, next);
      return HttpResponse.json(next);
    }),
    http.post(`${base}/stop`, async ({ params, request }) => {
      const denied = guard();
      if (denied) return denied;
      if (params.env === "production") {
        const body = (await request.json()) as { confirm?: string };
        if (body.confirm !== params.slug)
          return error(
            422,
            "validation_error",
            `Type the project slug '${String(params.slug)}' to stop production.`,
          );
      }
      const id = key(String(params.slug), String(params.env));
      const current = state.get(id) ?? initial(String(params.slug), String(params.env));
      const next = {
        ...current,
        state: current.total ? ("STOPPED" as const) : ("NOT_DEPLOYED" as const),
        running: 0,
      };
      state.set(id, next);
      return HttpResponse.json(next);
    }),
  ];
}
