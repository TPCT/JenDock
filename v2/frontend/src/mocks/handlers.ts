import { http, HttpResponse, sse, type HttpHandler } from "msw";
import type { EnvironmentName, Page, RunSummary, Session } from "../api/types";
import type { Model } from "../api/pipelineTypes";
import { adminHandlers } from "./admin";
import { containerHandlers } from "./containers";
import { databaseHandlers } from "./databases";
import { integrationHandlers } from "./integrations";
import { CATALOG, getStored, parseText, renderModel, saveStored, validateText } from "./pipelines";
import {
  MOCK_CSRF,
  MOCK_LOCKED_USERNAME,
  MOCK_PASSWORD,
  MOCK_USERNAME,
  allRuns,
  cancelRun,
  detailFor,
  head,
  logChunk,
  logText,
  projects,
  runsFor,
  startRun,
} from "./data";

const STORAGE_KEY = "mock-authenticated";

function readStored(): boolean {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

let authenticated = readStored();

/** Tests and the mock login flow switch the fake session on and off here. */
export function setMockAuthenticated(value: boolean): void {
  authenticated = value;
  try {
    if (value) window.sessionStorage.setItem(STORAGE_KEY, "1");
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage blocked: the fake session lasts until reload */
  }
}

function error(status: number, code: string, message: string, headers?: Record<string, string>) {
  return HttpResponse.json(
    { error: { code, message } },
    { status, ...(headers ? { headers } : {}) },
  );
}

const unauthenticated = () => error(401, "unauthenticated", "session expired or invalid");

function session(): Session {
  return {
    user: { id: 1, username: MOCK_USERNAME },
    csrf_token: MOCK_CSRF,
    expires_at: new Date(Date.now() + 12 * 3600_000).toISOString(),
  };
}

function paginate(items: RunSummary[], url: string): Page<RunSummary> {
  const params = new URL(url).searchParams;
  const limit = Math.min(100, Math.max(1, Number(params.get("limit") ?? 20)));
  const offset = Math.max(0, Number(params.get("offset") ?? 0));
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset };
}

function isEnvironment(value: unknown): value is EnvironmentName {
  return value === "development" || value === "production";
}

const SIMULATED_LINES = 10;

export const handlers: HttpHandler[] = [
  ...adminHandlers({ authenticated: () => authenticated, error, unauthenticated }),
  ...containerHandlers({ authenticated: () => authenticated, error, unauthenticated }),
  ...databaseHandlers({ authenticated: () => authenticated, error, unauthenticated }),
  ...integrationHandlers({ authenticated: () => authenticated, error, unauthenticated }),

  http.get("/api/auth/session", () =>
    authenticated ? HttpResponse.json(session()) : unauthenticated(),
  ),

  http.post("/api/auth/login", async ({ request }) => {
    const body = (await request.json()) as { username?: string; password?: string };
    if (!body.username || !body.password) {
      return HttpResponse.json(
        {
          error: {
            code: "validation_error",
            message: "invalid request",
            fields: [
              ...(body.username
                ? []
                : [{ loc: ["body", "username"], msg: "String should have at least 1 character" }]),
              ...(body.password
                ? []
                : [{ loc: ["body", "password"], msg: "String should have at least 1 character" }]),
            ],
          },
        },
        { status: 422 },
      );
    }
    if (body.username === MOCK_LOCKED_USERNAME) {
      return error(429, "rate_limited", "too many login attempts", { "Retry-After": "420" });
    }
    if (body.username !== MOCK_USERNAME || body.password !== MOCK_PASSWORD) {
      return error(401, "unauthenticated", "invalid username or password");
    }
    setMockAuthenticated(true);
    return HttpResponse.json(session());
  }),

  http.post("/api/auth/logout", () => {
    setMockAuthenticated(false);
    return new HttpResponse(null, { status: 204 });
  }),

  http.get("/api/projects", () =>
    authenticated ? HttpResponse.json(projects()) : unauthenticated(),
  ),

  http.get("/api/projects/:slug", ({ params }) => {
    if (!authenticated) return unauthenticated();
    const project = projects().find((item) => item.slug === params.slug);
    return project ? HttpResponse.json(project) : error(404, "not_found", "project not found");
  }),

  http.get("/api/projects/:slug/environments/:env/runs", ({ params, request }) => {
    if (!authenticated) return unauthenticated();
    const slug = String(params.slug);
    if (!projects().some((item) => item.slug === slug) || !isEnvironment(params.env)) {
      return error(404, "not_found", "environment not found");
    }
    return HttpResponse.json(paginate(runsFor(slug, params.env), request.url));
  }),

  http.get("/api/projects/:slug/environments/:env/head", ({ request }) => {
    if (!authenticated) return unauthenticated();
    const branch = new URL(request.url).searchParams.get("branch") ?? "";
    const result = head(branch);
    return result
      ? HttpResponse.json(result)
      : error(404, "not_found", `branch "${branch}" was not found`);
  }),

  http.post("/api/projects/:slug/environments/:env/runs", async ({ params, request }) => {
    if (!authenticated) return unauthenticated();
    if (request.headers.get("X-CSRF-Token") !== MOCK_CSRF)
      return error(403, "forbidden", "invalid CSRF token");
    const body = (await request.json()) as { branch: string; commit_sha: string };
    if (!isEnvironment(params.env)) return error(404, "not_found", "environment not found");
    return HttpResponse.json(
      startRun(String(params.slug), params.env, body.branch, body.commit_sha),
      { status: 201 },
    );
  }),

  http.get("/api/pipelines/catalog", () =>
    authenticated ? HttpResponse.json(CATALOG) : unauthenticated(),
  ),

  http.post("/api/pipelines/parse", async ({ request }) => {
    if (!authenticated) return unauthenticated();
    const body = (await request.json()) as { jenkinsfile: string };
    return HttpResponse.json(parseText(body.jenkinsfile));
  }),

  http.post("/api/pipelines/render", async ({ request }) => {
    if (!authenticated) return unauthenticated();
    const body = (await request.json()) as { model: Model };
    const jenkinsfile = renderModel(body.model);
    return HttpResponse.json({ jenkinsfile, diagnostics: validateText(jenkinsfile) });
  }),

  http.post("/api/pipelines/validate", async ({ request }) => {
    if (!authenticated) return unauthenticated();
    const body = (await request.json()) as { jenkinsfile: string };
    return HttpResponse.json({ diagnostics: validateText(body.jenkinsfile) });
  }),

  http.get("/api/projects/:slug/environments/:env/pipeline", ({ params }) => {
    if (!authenticated) return unauthenticated();
    if (!projects().some((item) => item.slug === params.slug))
      return error(404, "not_found", "project not found");
    const stored = getStored(String(params.slug), String(params.env));
    const latest = stored.versions[stored.versions.length - 1];
    return HttpResponse.json({
      source: stored.source,
      version: latest?.version ?? 0,
      jenkinsfile: latest?.jenkinsfile ?? "",
      updated_at: latest?.created_at ?? null,
      updated_by: latest?.created_by ?? null,
    });
  }),

  http.put("/api/projects/:slug/environments/:env/pipeline", async ({ params, request }) => {
    if (!authenticated) return unauthenticated();
    if (request.headers.get("X-CSRF-Token") !== MOCK_CSRF)
      return error(403, "forbidden", "invalid CSRF token");
    const slug = String(params.slug);
    const env = String(params.env);
    const stored = getStored(slug, env);
    if (stored.source === "repository")
      return error(409, "conflict", "the pipeline is defined in the repository");
    const body = (await request.json()) as { jenkinsfile: string; base_version: number };
    const current = stored.versions[stored.versions.length - 1]?.version ?? 0;
    if (body.base_version !== current) {
      return error(
        409,
        "conflict",
        `the pipeline changed: version ${current} exists, you edited version ${body.base_version}`,
      );
    }
    if (body.jenkinsfile.includes("INVALID")) {
      return HttpResponse.json(
        {
          error: {
            code: "validation_error",
            message: "the pipeline is not valid",
            diagnostics: [
              { severity: "error", message: "INVALID is not a known step", line: 3, column: 5 },
            ],
          },
        },
        { status: 422 },
      );
    }
    const saved = saveStored(slug, env, body.jenkinsfile, MOCK_USERNAME);
    return HttpResponse.json({
      source: "platform",
      version: saved.version,
      jenkinsfile: saved.jenkinsfile,
      updated_at: saved.created_at,
      updated_by: saved.created_by,
    });
  }),

  http.get("/api/projects/:slug/environments/:env/pipeline/versions", ({ params }) => {
    if (!authenticated) return unauthenticated();
    const stored = getStored(String(params.slug), String(params.env));
    return HttpResponse.json(
      [...stored.versions]
        .reverse()
        .map(({ version, created_at, created_by }) => ({ version, created_at, created_by })),
    );
  }),

  http.get("/api/projects/:slug/environments/:env/pipeline/versions/:version", ({ params }) => {
    if (!authenticated) return unauthenticated();
    const stored = getStored(String(params.slug), String(params.env));
    const found = stored.versions.find((v) => v.version === Number(params.version));
    return found ? HttpResponse.json(found) : error(404, "not_found", "version not found");
  }),

  http.get("/api/runs", ({ request }) =>
    authenticated ? HttpResponse.json(paginate(allRuns(), request.url)) : unauthenticated(),
  ),

  http.get("/api/runs/:id", ({ params }) => {
    if (!authenticated) return unauthenticated();
    const run = detailFor(Number(params.id));
    return run ? HttpResponse.json(run) : error(404, "not_found", "run not found");
  }),

  http.post("/api/runs/:id/cancel", ({ params, request }) => {
    if (!authenticated) return unauthenticated();
    if (request.headers.get("X-CSRF-Token") !== MOCK_CSRF)
      return error(403, "forbidden", "invalid CSRF token");
    const id = Number(params.id);
    const outcome = cancelRun(id);
    if (outcome === "missing") return error(404, "not_found", "run not found");
    if (outcome === "finished") return error(409, "conflict", "run already finished");
    return HttpResponse.json({ outcome, run: detailFor(id) });
  }),

  http.get("/api/runs/:id/steps/:stepId/log", ({ params, request }) => {
    if (!authenticated) return unauthenticated();
    const run = detailFor(Number(params.id));
    const step = run?.stages
      .flatMap((stage) => stage.steps)
      .find((item) => item.id === Number(params.stepId));
    if (!step) return error(404, "not_found", "step not found");
    const offset = Number(new URL(request.url).searchParams.get("offset") ?? 0);
    return HttpResponse.json(logChunk(step.id, step.status, offset));
  }),

  // MSW can only build SSE handlers where EventSource exists (browsers); tests use a fake.
  ...(typeof EventSource === "undefined"
    ? []
    : [
        sse<{ status: string; log: string; end: string }>(
          "/api/runs/:id/events",
          ({ params, client }) => {
            const run = detailFor(Number(params.id));
            if (!authenticated || !run) {
              client.error();
              return;
            }
            client.send({ event: "status", data: JSON.stringify(run) });
            const running = run.stages
              .flatMap((stage) => stage.steps)
              .find((step) => step.status === "running");
            if (!running) return;
            const encoder = new TextEncoder();
            let offset = encoder.encode(logText(running.id, "running")).length;
            let count = 0;
            const timer = setInterval(() => {
              if (count >= SIMULATED_LINES) {
                clearInterval(timer);
                return;
              }
              count += 1;
              const data = `\u001b[36mstreaming\u001b[0m line ${count} of ${SIMULATED_LINES}\n`;
              const next = offset + encoder.encode(data).length;
              client.send({
                event: "log",
                data: JSON.stringify({
                  step_id: running.id,
                  offset,
                  text: data,
                }),
              });
              offset = next;
            }, 1000);
          },
        ),
      ]),
];
