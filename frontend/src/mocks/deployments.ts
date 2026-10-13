/** Mock of the deployment history and redeploy endpoints (docs/api-contract.md). */
import { http, HttpResponse, type HttpHandler } from "msw";
import type { Deployment, DeploymentStatus } from "../api/deploymentTypes";
import { MOCK_CSRF, projectExists } from "./data";
import type { MockContext } from "./admin";
import { byTime, listResponse, type MockList } from "./paging";

const MINUTE = 60_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();

function deployment(id: number, status: DeploymentStatus, createdAgo: number): Deployment {
  const sha = (id * 2654435761).toString(16).padStart(8, "0").repeat(5).slice(0, 40);
  return {
    id,
    run_id: 200 + id,
    run_number: id,
    status,
    commit_sha: sha,
    services: ["web", "worker"].map((service) => ({
      service,
      previous_image: id > 1 ? `jendock/shop-${service}:${String(id - 1)}` : null,
      previous_image_id: id > 1 ? `sha256:${String(id - 1).padStart(12, "0")}` : null,
      new_image: `jendock/shop-${service}:${String(id)}`,
      new_image_id: `sha256:${String(id).padStart(12, "0")}`,
    })),
    message:
      status === "failed"
        ? "the new containers did not become healthy"
        : status === "rolled_back"
          ? "rolled back: web did not pass its health check"
          : null,
    redeploy_of: null,
    created_at: ago(createdAgo),
    started_at: status === "pending" ? null : ago(createdAgo - 5_000),
    finished_at: status === "pending" || status === "deploying" ? null : ago(createdAgo - 65_000),
  };
}

const STATUSES: DeploymentStatus[] = ["succeeded", "succeeded", "failed", "rolled_back"];

function seed(bulk: number): Deployment[] {
  const count = 6 + bulk;
  return Array.from({ length: count }, (_, index) => {
    const id = count - index;
    const status = index === 0 && bulk === 0 ? "deploying" : (STATUSES[index % 4] ?? "succeeded");
    return deployment(id, status, (index + 1) * 95 * MINUTE);
  });
}

let history = seed(0);
let nextId = 500;

export function resetDeploymentsMock(): void {
  history = seed(0);
  nextId = 500;
}

/** Sixty more deployments of shop-api so the history pages (dev mock mode only). */
export function seedBulkDeployments(): void {
  history = seed(60);
}

const LIST: MockList<Deployment> = {
  text: (item) => [item.commit_sha, item.message, `#${String(item.run_number)}`],
  filters: { status: (item, value) => item.status === value },
  sorts: {
    created: (a, b) => a.id - b.id,
    finished: (a, b) => byTime(a.finished_at, b.finished_at),
  },
  defaultSort: "created",
  descending: ["created", "finished"],
};

export function deploymentHandlers(ctx: MockContext): HttpHandler[] {
  const BASE = "/api/projects/:slug/environments/:env/deployments";
  const guard = (request: Request, mutating: boolean, slug: unknown): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return ctx.error(403, "forbidden", "invalid CSRF token");
    }
    return projectExists(String(slug)) ? null : ctx.error(404, "not_found", "project not found");
  };
  // Only shop-api's development environment has a history; the others are empty.
  const rowsOf = (slug: unknown, env: unknown): Deployment[] =>
    slug === "shop-api" && env === "development" ? history : [];

  return [
    http.get(
      BASE,
      ({ params, request }) =>
        guard(request, false, params.slug) ??
        listResponse(rowsOf(params.slug, params.env), request.url, LIST),
    ),
    http.post(`${BASE}/:id/redeploy`, ({ params, request }) => {
      const denied = guard(request, true, params.slug);
      if (denied) return denied;
      const source = rowsOf(params.slug, params.env).find((item) => item.id === Number(params.id));
      if (!source) return ctx.error(404, "not_found", "deployment not found");
      if (source.status !== "succeeded") {
        return ctx.error(409, "conflict", "only a succeeded deployment can be redeployed");
      }
      const created: Deployment = {
        ...deployment(nextId++, "pending", 0),
        commit_sha: source.commit_sha,
        services: source.services,
        redeploy_of: source.id,
      };
      history = [created, ...history];
      return HttpResponse.json(
        {
          run: {
            id: 900 + created.id,
            number: created.id,
            project: String(params.slug),
            environment: String(params.env),
            trigger: "redeploy",
            branch: "develop",
            commit_sha: source.commit_sha,
            pipeline_source: "platform",
            status: "queued",
            message: null,
            cancel_requested_at: null,
            created_at: new Date().toISOString(),
            started_at: null,
            finished_at: null,
          },
          deployment_id: created.id,
        },
        { status: 201 },
      );
    }),
  ];
}
