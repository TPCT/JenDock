/**
 * Mock of the project-administration endpoints: templates, project create/update, secrets,
 * API tokens and domains. Shapes follow docs/api-contract.md and the routers in
 * jendock/api/routers exactly; the rules (409/422 cases) are the ones the server enforces.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type {
  Certificate,
  Check,
  CreateDomainBody,
  DomainDetail,
  Plan,
  PlanOperation,
  SecretInfo,
  TokenInfo,
  TokenScope,
} from "../api/adminTypes";
import {
  MOCK_CSRF,
  createProject,
  patchEnvironment,
  patchProject,
  projectExists,
  retryRun,
} from "./data";

export interface MockContext {
  authenticated: () => boolean;
  error: (status: number, code: string, message: string) => Response;
  unauthenticated: () => Response;
}

const DAY = 86_400_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();
const ahead = (ms: number): string => new Date(Date.now() + ms).toISOString();

// ---- templates ---------------------------------------------------------------------------

function template(name: string, title: string, description: string, build: string, run: string) {
  const jenkinsfile = `// ${title}: change the placeholders marked CHANGE ME before the first deploy.
pipeline {
  agent any
  stages {
    stage('Build') {
      steps {
        dockerBuild(image: 'app', dockerfile: 'Dockerfile')
      }
    }
    stage('Test') {
      agent { docker 'node:22' }
      steps {
        sh '${build}'
      }
    }
    stage('Deploy') {
      steps {
        deploy(service: 'app', image: 'app', port: ${run})
      }
    }
  }
}
`;
  return { name, title, description, jenkinsfile };
}

export const TEMPLATES = [
  template(
    "laravel",
    "Laravel",
    "PHP app: install with Composer, run the test suite, deploy the image.",
    "composer install && php artisan test",
    "8000",
  ),
  template(
    "nextjs",
    "Next.js",
    "Node app: install, build, test and deploy a standalone server.",
    "npm ci && npm run build && npm test",
    "3000",
  ),
  template(
    "node-api",
    "Node API",
    "Express/Fastify style API: install, test, deploy.",
    "npm ci && npm test",
    "3000",
  ),
  template(
    "python-fastapi",
    "Python / FastAPI",
    "FastAPI app: install requirements, run pytest, deploy.",
    "pip install -r requirements.txt && pytest",
    "8000",
  ),
  template(
    "static-site",
    "Static site",
    "Build static files and serve them from a small web server image.",
    "npm ci && npm run build",
    "80",
  ),
  template(
    "dockerfile",
    "Any project with a Dockerfile",
    "Builds the Dockerfile in the repository and deploys it as is.",
    "echo no tests configured",
    "8080",
  ),
];

// ---- secrets -----------------------------------------------------------------------------

const SECRET_NAME = /^[A-Z_][A-Z0-9_]{0,127}$/;
let secrets = new Map<string, SecretInfo[]>();
const secretKey = (slug: unknown, env: unknown) => `${String(slug)}/${String(env)}`;

function secretsOf(slug: unknown, env: unknown): SecretInfo[] {
  const key = secretKey(slug, env);
  let list = secrets.get(key);
  if (!list) {
    list = ["DB_PASSWORD", "REGISTRY_TOKEN"].map((name) => ({
      name,
      created_at: ago(9 * DAY),
      updated_at: ago(2 * DAY),
    }));
    secrets.set(key, list);
  }
  return list;
}

// ---- tokens ------------------------------------------------------------------------------

function initialTokens(): TokenInfo[] {
  return [
    {
      id: 1,
      name: "CI deploy",
      prefix: "sc_k3j9",
      scopes: ["read", "run", "deploy"],
      created_at: ago(30 * DAY),
      last_used_at: ago(2 * 3_600_000),
      expires_at: ahead(60 * DAY),
      revoked_at: null,
    },
    {
      id: 2,
      name: "Old laptop",
      prefix: "sc_x81q",
      scopes: ["read"],
      created_at: ago(200 * DAY),
      last_used_at: null,
      expires_at: null,
      revoked_at: ago(10 * DAY),
    },
  ];
}
let tokens = initialTokens();
let nextTokenId = 10;
const ALL_SCOPES: TokenScope[] = [
  "read",
  "run",
  "deploy",
  "containers",
  "terminal",
  "pipelines",
  "admin",
];

// ---- domains -----------------------------------------------------------------------------

function certificate(daysRemaining: number): Certificate {
  const state =
    daysRemaining <= 0
      ? "expired"
      : daysRemaining < 3
        ? "critical"
        : daysRemaining < 14
          ? "warning"
          : daysRemaining <= 30
            ? "renewal_due"
            : "ok";
  return {
    not_after: ahead(daysRemaining * DAY),
    days_remaining: daysRemaining,
    state,
    last_attempt_at: null,
    last_error: null,
  };
}

function checkOf(hostname: string, tls: boolean, port: number): Check {
  const stamp = new Date().toISOString();
  const dnsOk = !hostname.includes("broken");
  return {
    checked_at: stamp,
    ok: dnsOk,
    results: {
      dns: {
        ok: dnsOk,
        detail: dnsOk ? "Resolves to 203.0.113.7, this server." : "No address found for this name.",
        checked_at: stamp,
        addresses: dnsOk ? ["203.0.113.7"] : [],
        expected: ["203.0.113.7"],
      },
      https: {
        ok: dnsOk,
        detail: dnsOk
          ? tls
            ? "Answered 200 with a valid certificate."
            : "Answered 200 over plain HTTP."
          : "The connection failed.",
        checked_at: stamp,
        status_code: 200,
      },
      backend: { ok: true, detail: `Port ${port} accepts connections.`, checked_at: stamp, port },
    },
  };
}

function seedDomains(): Map<string, DomainDetail[]> {
  const base = {
    dns_target: null,
    dns_proxied: false,
    tls_enabled: true,
    updated_at: ago(DAY),
  };
  const map = new Map<string, DomainDetail[]>();
  map.set("shop-api/development", [
    {
      ...base,
      id: 1,
      hostname: "shop.example.com",
      service: "web",
      container_port: 8080,
      published_port: 20001,
      dns_record_type: "A",
      status: "active",
      status_message: null,
      applied_at: ago(5 * DAY),
      last_verified_at: ago(3_600_000),
      last_verified_ok: true,
      created_at: ago(6 * DAY),
      certificate: certificate(62),
      check: checkOf("shop.example.com", true, 20001),
    },
    {
      ...base,
      id: 2,
      hostname: "api.example.com",
      service: "api",
      container_port: 3000,
      published_port: 20002,
      dns_record_type: "CNAME",
      dns_target: "lb.example.net",
      status: "error",
      status_message: "configtest failed, change reverted: AH00526: Syntax error on line 14",
      applied_at: null,
      last_verified_at: null,
      last_verified_ok: null,
      created_at: ago(4 * DAY),
      certificate: certificate(2),
      check: null,
    },
    {
      ...base,
      id: 3,
      hostname: "staging.example.com",
      service: "web",
      container_port: 8080,
      published_port: 20003,
      dns_record_type: "A",
      status: "draft",
      status_message: null,
      applied_at: null,
      last_verified_at: null,
      last_verified_ok: null,
      created_at: ago(DAY),
      certificate: null,
      check: null,
    },
  ]);
  return map;
}
let domains = seedDomains();
let nextDomainId = 10;

function domainsOf(slug: unknown, env: unknown): DomainDetail[] {
  const key = secretKey(slug, env);
  let list = domains.get(key);
  if (!list) {
    list = [];
    domains.set(key, list);
  }
  return list;
}

function findDomain(slug: unknown, env: unknown, id: unknown): DomainDetail | undefined {
  return domainsOf(slug, env).find((item) => item.id === Number(id));
}

function hashOf(domain: DomainDetail, operation: PlanOperation): string {
  const text = JSON.stringify([
    domain.id,
    domain.hostname,
    domain.service,
    domain.published_port,
    domain.status,
    domain.tls_enabled,
    domain.dns_record_type,
    operation,
  ]);
  let h = 0;
  for (const ch of text) h = (Math.imul(h, 31) + ch.charCodeAt(0)) >>> 0;
  return h.toString(16).padStart(8, "0").repeat(8);
}

function vhost(domain: DomainDetail): string {
  return `<VirtualHost *:80>
    ServerName ${domain.hostname}
    ProxyPass / http://127.0.0.1:${String(domain.published_port)}/
    ProxyPassReverse / http://127.0.0.1:${String(domain.published_port)}/
</VirtualHost>
`;
}

function planFor(domain: DomainDetail, operation: PlanOperation): Plan {
  const zone = domain.hostname.split(".").slice(-2).join(".");
  const desired = {
    record_type: domain.dns_record_type,
    name: domain.hostname,
    content: domain.dns_target ?? "203.0.113.7",
    proxied: domain.dns_proxied,
  };
  const conflict = operation === "apply" && domain.hostname.includes("taken");
  const proposed = vhost(domain);
  const applying = operation === "apply";
  const exists = domain.status === "active";
  const diff = applying
    ? proposed
        .split("\n")
        .filter(Boolean)
        .map((line) => (exists ? ` ${line}` : `+${line}`))
        .join("\n")
    : proposed
        .split("\n")
        .filter(Boolean)
        .map((line) => `-${line}`)
        .join("\n");
  const dnsAction = conflict ? "conflict" : applying ? (exists ? "noop" : "create") : "delete";
  return {
    domain_id: domain.id,
    hostname: domain.hostname,
    operation,
    blocked: conflict,
    dns: {
      action: dnsAction,
      zone,
      current: conflict
        ? { record_type: "A", name: domain.hostname, content: "198.51.100.20", proxied: false }
        : applying
          ? null
          : desired,
      desired: applying ? desired : null,
      removes: [],
      message: conflict
        ? "A DNS record for this name already exists and was not created by Jendock."
        : "",
    },
    vhost: {
      action: applying ? (exists ? "noop" : "create") : "delete",
      current: applying ? null : proposed,
      proposed: applying ? proposed : null,
      diff,
    },
    tls: {
      action: !domain.tls_enabled ? "none" : applying ? (exists ? "keep" : "issue") : "remove",
      challenge: domain.tls_enabled ? "http-01" : null,
      not_after: null,
    },
    steps: applying
      ? [
          "DNS: create",
          "Apache: HTTP vhost, then reload",
          ...(domain.tls_enabled ? ["TLS: issue certificate (http-01)"] : []),
        ]
      : [
          "Apache: remove vhost, then reload",
          "TLS: remove certificate",
          "DNS: delete the record Jendock created",
        ],
    plan_hash: hashOf(domain, operation),
  };
}

const HOSTNAME = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

export function resetAdminMock(): void {
  secrets = new Map();
  tokens = initialTokens();
  nextTokenId = 10;
  domains = seedDomains();
  nextDomainId = 10;
}

// ---- handlers ----------------------------------------------------------------------------

export function adminHandlers(ctx: MockContext): HttpHandler[] {
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

  return [
    http.get("/api/pipelines/templates", ({ request }) => {
      const denied = guard(request, false);
      return denied ?? HttpResponse.json(TEMPLATES);
    }),

    // ---- projects
    http.post("/api/projects", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as Parameters<typeof createProject>[0];
      if (projectExists(body.slug))
        return error(409, "conflict", `project "${body.slug}" already exists`);
      if (!/^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(body.slug)) {
        return error(
          422,
          "validation_error",
          "the slug may use lowercase letters, digits and dashes",
        );
      }
      if (!/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/.test(body.repository)) {
        return error(422, "validation_error", "the repository must look like owner/name");
      }
      if (body.development_branch === body.production_branch) {
        return error(422, "validation_error", "the two environments must track different branches");
      }
      return HttpResponse.json(createProject(body), { status: 201 });
    }),

    http.patch("/api/projects/:slug", async ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as Parameters<typeof patchProject>[1];
      if (Object.keys(body).length === 0)
        return error(422, "validation_error", "nothing to change");
      const result = patchProject(String(params.slug), body);
      return result ? HttpResponse.json(result) : error(404, "not_found", "project not found");
    }),

    http.patch("/api/projects/:slug/environments/:env", async ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as Parameters<typeof patchEnvironment>[2];
      if (params.env !== "development" && params.env !== "production") {
        return error(404, "not_found", "environment not found");
      }
      const result = patchEnvironment(String(params.slug), params.env, body);
      if (result === null) return error(404, "not_found", "project not found");
      if ("error" in result) return error(422, "validation_error", result.error);
      return HttpResponse.json(result.value);
    }),

    http.post("/api/runs/:id/retry", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const result = retryRun(Number(params.id));
      if (result === "missing") return error(404, "not_found", "run not found");
      if (result === "active") return error(409, "conflict", "the run has not finished yet");
      return HttpResponse.json(result, { status: 201 });
    }),

    // ---- secrets
    http.get("/api/projects/:slug/environments/:env/secrets", ({ params, request }) => {
      const denied = guard(request, false) ?? noProject(params.slug);
      return denied ?? HttpResponse.json(secretsOf(params.slug, params.env));
    }),
    http.put("/api/projects/:slug/environments/:env/secrets/:name", async ({ params, request }) => {
      const denied = guard(request, true) ?? noProject(params.slug);
      if (denied) return denied;
      const name = String(params.name);
      const body = (await request.json()) as { value?: string };
      if (!SECRET_NAME.test(name)) {
        return error(
          422,
          "validation_error",
          "a secret name uses capital letters, digits and underscores",
        );
      }
      if (!body.value) return error(422, "validation_error", "the value must not be empty");
      const list = secretsOf(params.slug, params.env);
      const now = new Date().toISOString();
      const existing = list.find((item) => item.name === name);
      if (existing) existing.updated_at = now;
      else list.push({ name, created_at: now, updated_at: now });
      return HttpResponse.json(list.find((item) => item.name === name));
    }),
    http.delete("/api/projects/:slug/environments/:env/secrets/:name", ({ params, request }) => {
      const denied = guard(request, true) ?? noProject(params.slug);
      if (denied) return denied;
      const list = secretsOf(params.slug, params.env);
      const index = list.findIndex((item) => item.name === params.name);
      if (index < 0) return error(404, "not_found", "secret not found");
      list.splice(index, 1);
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(
      "/api/projects/:slug/environments/:env/secrets/:name/reveal",
      ({ params, request }) => {
        const denied = guard(request, true) ?? noProject(params.slug);
        if (denied) return denied;
        if (!secretsOf(params.slug, params.env).some((item) => item.name === params.name)) {
          return error(404, "not_found", "secret not found");
        }
        return HttpResponse.json({ name: params.name, value: `value-of-${String(params.name)}` });
      },
    ),

    // ---- tokens
    http.get("/api/tokens", ({ request }) => guard(request, false) ?? HttpResponse.json(tokens)),
    http.post("/api/tokens", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as {
        name: string;
        scopes: TokenScope[];
        expires_at: string | null;
      };
      if (!body.name || body.name.length > 120)
        return error(422, "validation_error", "invalid name");
      if (body.scopes.length === 0 || body.scopes.some((scope) => !ALL_SCOPES.includes(scope))) {
        return error(422, "validation_error", "choose at least one valid scope");
      }
      const id = nextTokenId++;
      const value = `sc_${id.toString(16).padStart(4, "0")}${"f3a9c1d7e5b20486".repeat(2)}`;
      const info: TokenInfo = {
        id,
        name: body.name,
        prefix: value.slice(0, 7),
        scopes: body.scopes,
        created_at: new Date().toISOString(),
        last_used_at: null,
        expires_at: body.expires_at,
        revoked_at: null,
      };
      tokens = [info, ...tokens];
      return HttpResponse.json({ token: value, info }, { status: 201 });
    }),
    http.delete("/api/tokens/:id", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const token = tokens.find((item) => item.id === Number(params.id));
      if (!token) return error(404, "not_found", "token not found");
      token.revoked_at ??= new Date().toISOString();
      return HttpResponse.json(token);
    }),

    // ---- domains
    http.get("/api/projects/:slug/environments/:env/domains", ({ params, request }) => {
      const denied = guard(request, false) ?? noProject(params.slug);
      return (
        denied ??
        HttpResponse.json(
          [...domainsOf(params.slug, params.env)].sort((a, b) =>
            a.hostname.localeCompare(b.hostname),
          ),
        )
      );
    }),
    http.post("/api/projects/:slug/environments/:env/domains", async ({ params, request }) => {
      const denied = guard(request, true) ?? noProject(params.slug);
      if (denied) return denied;
      const body = (await request.json()) as CreateDomainBody;
      const hostname = body.hostname.trim().toLowerCase().replace(/\.$/, "");
      if (!HOSTNAME.test(hostname))
        return error(422, "validation_error", "that is not a valid host name");
      if ([...domains.values()].flat().some((item) => item.hostname === hostname)) {
        return error(409, "conflict", `${hostname} is already used by another domain`);
      }
      if (body.dns_record_type === "CNAME" && !body.dns_target) {
        return error(422, "validation_error", "a CNAME record needs a target host name");
      }
      for (const port of [body.published_port]) {
        if (port < 1024 || port > 65535)
          return error(
            422,
            "validation_error",
            "the published port must be between 1024 and 65535",
          );
      }
      const now = new Date().toISOString();
      const created: DomainDetail = {
        id: nextDomainId++,
        hostname,
        service: body.service,
        container_port: body.container_port,
        published_port: body.published_port,
        dns_record_type: body.dns_record_type,
        dns_target: body.dns_target ?? null,
        dns_proxied: body.dns_proxied,
        tls_enabled: body.tls_enabled,
        status: "draft",
        status_message: null,
        applied_at: null,
        last_verified_at: null,
        last_verified_ok: null,
        created_at: now,
        updated_at: now,
        certificate: null,
        check: null,
      };
      domainsOf(params.slug, params.env).push(created);
      const domain = Object.fromEntries(
        Object.entries(created).filter(([key]) => key !== "certificate" && key !== "check"),
      );
      return HttpResponse.json(domain, { status: 201 });
    }),
    http.delete("/api/projects/:slug/environments/:env/domains/:id", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const list = domainsOf(params.slug, params.env);
      const domain = list.find((item) => item.id === Number(params.id));
      if (!domain) return error(404, "not_found", "domain not found");
      if (domain.status !== "draft")
        return error(409, "conflict", "remove the domain from the server first");
      list.splice(list.indexOf(domain), 1);
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(
      "/api/projects/:slug/environments/:env/domains/:id/preview",
      async ({ params, request }) => {
        const denied = guard(request, true);
        if (denied) return denied;
        const domain = findDomain(params.slug, params.env, params.id);
        if (!domain) return error(404, "not_found", "domain not found");
        const body = (await request.json()) as { operation?: PlanOperation };
        return HttpResponse.json(planFor(domain, body.operation ?? "apply"));
      },
    ),
    http.post(
      "/api/projects/:slug/environments/:env/domains/:id/apply",
      async ({ params, request }) => {
        const denied = guard(request, true);
        if (denied) return denied;
        const domain = findDomain(params.slug, params.env, params.id);
        if (!domain) return error(404, "not_found", "domain not found");
        const body = (await request.json()) as { plan_hash: string };
        const plan = planFor(domain, "apply");
        if (plan.blocked)
          return error(409, "conflict", "a DNS record that Jendock did not create is in the way");
        if (plan.plan_hash !== body.plan_hash)
          return error(409, "conflict", "the plan changed since the preview");
        if (domain.hostname.startsWith("fail")) {
          domain.status = "error";
          domain.status_message = "Cloudflare rejected the API token (check its scope)";
          return error(502, "upstream_error", domain.status_message);
        }
        domain.status = "active";
        domain.status_message = null;
        domain.applied_at = new Date().toISOString();
        if (domain.tls_enabled) domain.certificate = certificate(89);
        return HttpResponse.json(plan);
      },
    ),
    http.post(
      "/api/projects/:slug/environments/:env/domains/:id/remove",
      async ({ params, request }) => {
        const denied = guard(request, true);
        if (denied) return denied;
        const domain = findDomain(params.slug, params.env, params.id);
        if (!domain) return error(404, "not_found", "domain not found");
        const body = (await request.json()) as { plan_hash: string };
        const plan = planFor(domain, "remove");
        if (plan.plan_hash !== body.plan_hash)
          return error(409, "conflict", "the plan changed since the preview");
        domain.status = "draft";
        domain.applied_at = null;
        domain.certificate = null;
        domain.check = null;
        return HttpResponse.json(plan);
      },
    ),
    http.post("/api/projects/:slug/environments/:env/domains/:id/check", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const domain = findDomain(params.slug, params.env, params.id);
      if (!domain) return error(404, "not_found", "domain not found");
      const check = checkOf(domain.hostname, domain.tls_enabled, domain.published_port);
      domain.check = check;
      domain.last_verified_at = check.checked_at;
      domain.last_verified_ok = check.ok;
      return HttpResponse.json(check);
    }),
    http.post(
      "/api/projects/:slug/environments/:env/domains/:id/certificate",
      ({ params, request }) => {
        const denied = guard(request, true);
        if (denied) return denied;
        const domain = findDomain(params.slug, params.env, params.id);
        if (!domain) return error(404, "not_found", "domain not found");
        if (domain.status !== "active" || !domain.tls_enabled) {
          return error(409, "conflict", "the domain must be active with HTTPS enabled");
        }
        domain.certificate = certificate(89);
        return HttpResponse.json(domain.certificate);
      },
    ),
    http.get("/api/ingress/certificates", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      const rows = [...domains.values()]
        .flat()
        .filter((item) => item.certificate)
        .map((item) => ({
          domain_id: item.id,
          hostname: item.hostname,
          ...(item.certificate as Certificate),
        }))
        .sort((a, b) => a.days_remaining - b.days_remaining);
      return HttpResponse.json(rows);
    }),
  ];
}
