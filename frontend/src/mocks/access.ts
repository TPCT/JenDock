/**
 * Mock of the member and access endpoints, and of the signed-in user's role: tests and the
 * mock dev server can sign in as an owner, an admin, a viewer/developer/maintainer member, a
 * member with a container-only grant, or a user who must still change a one-time password.
 * Shapes follow docs/api-contract.md and backend/api/routers/{users,access,auth}.py.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type {
  Grant,
  OneTimePassword,
  SetGrantBody,
  UpdateUserBody,
  UserSummary,
} from "../api/accessTypes";
import type {
  AccessRole,
  EnvironmentName,
  EnvironmentPermissions,
  GlobalRole,
  Permissions,
  Session,
} from "../api/types";
import { MOCK_CSRF, MOCK_USERNAME, projectExists } from "./data";
import type { MockContext } from "./admin";

export type Persona =
  "owner" | "admin" | "viewer" | "developer" | "maintainer" | "container" | "pending";

export const MOCK_ONE_TIME_PASSWORD = "Mock-one-time-Pw-0001";
const PROJECT = "shop-api";

let persona: Persona = "owner";

/** Which kind of user the mock session belongs to (reset after every test). */
export function setMockPersona(value: Persona): void {
  persona = value;
}

const VIEWER = [
  "project.read",
  "run.read",
  "pipeline.read",
  "deployment.read",
  "container.read",
  "database.read",
  "domain.read",
  "secret.list",
];
const DEVELOPER = ["run.create", "run.cancel", "run.retry", "pipeline.write", "database.data"];
const MAINTAINER = [
  "deployment.redeploy",
  "container.logs",
  "container.terminal",
  "container.action",
  "container.env_update",
  "container.limits",
  "container.files",
  "domain.manage",
];
const OWNER_GLOBAL = [
  "access.manage",
  "certificate.list",
  "container.env_reveal",
  "database.configure",
  "environment.update",
  "integration.manage",
  "mcp.tools",
  "notification.manage",
  "notification.policy",
  "ingress.readiness",
  "pipeline.tools",
  "project.create",
  "project.list",
  "project.update",
  "run.list",
  "secret.reveal",
  "secret.write",
  "telemetry.read",
  "token.manage",
  "user.manage",
];
const MEMBER_GLOBAL = [
  "ingress.readiness",
  "mcp.tools",
  "notification.policy",
  "pipeline.tools",
  "project.list",
  "run.list",
  "telemetry.read",
  "token.manage",
];

function actionsOn(role: AccessRole, env: EnvironmentName): string[] {
  const rank = ["viewer", "developer", "maintainer"].indexOf(role);
  // Production asks one role more for runs, pipelines and rows, like the server.
  const developer = env === "development" ? rank >= 1 : rank >= 2;
  return [...VIEWER, ...(developer ? DEVELOPER : []), ...(rank >= 2 ? MAINTAINER : [])];
}

function memberPermissions(role: AccessRole | null): Permissions {
  const environment = (env: EnvironmentName): EnvironmentPermissions => {
    if (role) return { role, actions: actionsOn(role, env), services: {} };
    // A container-only grant: visible in development, logs of the web service, nothing else.
    return env === "development"
      ? {
          role: null,
          actions: ["project.read"],
          services: { web: ["container.logs", "container.read", "project.read"] },
        }
      : { role: null, actions: [], services: {} };
  };
  const environments: Permissions["projects"][string]["environments"] = {
    development: environment("development"),
    ...(role ? { production: environment("production") } : {}),
  };
  return {
    role: "member",
    all: false,
    global: MEMBER_GLOBAL,
    projects: { [PROJECT]: { environments } },
  };
}

function permissionsFor(value: Persona): Permissions {
  if (value === "owner" || value === "pending") {
    return { role: "owner", all: true, global: OWNER_GLOBAL, projects: {} };
  }
  if (value === "admin") return { role: "admin", all: true, global: OWNER_GLOBAL, projects: {} };
  return memberPermissions(value === "container" ? null : value);
}

const ROLE_OF: Record<Persona, GlobalRole> = {
  owner: "owner",
  pending: "owner",
  admin: "admin",
  viewer: "member",
  developer: "member",
  maintainer: "member",
  container: "member",
};

/** The session of the current persona (what `GET /api/auth/session` answers). */
export function mockSession(): Session {
  const role = ROLE_OF[persona];
  const id = role === "owner" ? 1 : role === "admin" ? 2 : 3;
  return {
    user: {
      id,
      username: role === "owner" ? MOCK_USERNAME : persona,
      display_name: role === "owner" ? "" : persona,
      email: null,
      role,
    },
    csrf_token: MOCK_CSRF,
    expires_at: new Date(Date.now() + 12 * 3600_000).toISOString(),
    must_change_password: persona === "pending",
    permissions: permissionsFor(persona),
  };
}

const DAY = 86_400_000;

function seedUsers(): UserSummary[] {
  const at = (days: number) => new Date(Date.now() - days * DAY).toISOString();
  return [
    {
      id: 1,
      username: MOCK_USERNAME,
      display_name: "",
      email: null,
      role: "owner",
      is_active: true,
      must_change_password: false,
      created_at: at(90),
    },
    {
      id: 2,
      username: "olivia",
      display_name: "Olivia Admin",
      email: "olivia@example.com",
      role: "admin",
      is_active: true,
      must_change_password: false,
      created_at: at(40),
    },
    {
      id: 3,
      username: "maria",
      display_name: "Maria Lopez",
      email: "maria@example.com",
      role: "member",
      is_active: true,
      must_change_password: false,
      created_at: at(12),
    },
    {
      id: 4,
      username: "dave",
      display_name: "Dave Dev",
      email: null,
      role: "member",
      is_active: false,
      must_change_password: false,
      created_at: at(5),
    },
  ];
}

function seedGrants(): Grant[] {
  const base = {
    project: PROJECT,
    created_at: new Date(Date.now() - 3 * DAY).toISOString(),
  };
  return [
    {
      ...base,
      id: 1,
      user_id: 3,
      username: "maria",
      display_name: "Maria Lopez",
      environment: null,
      service: null,
      role: "viewer",
      permissions: [],
    },
    {
      ...base,
      id: 2,
      user_id: 3,
      username: "maria",
      display_name: "Maria Lopez",
      environment: "development",
      service: "web",
      role: null,
      permissions: ["logs"],
    },
  ];
}

let users = seedUsers();
let grants = seedGrants();
let nextUserId = 10;
let nextGrantId = 10;

export function resetAccessMock(): void {
  persona = "owner";
  users = seedUsers();
  grants = seedGrants();
  nextUserId = 10;
  nextGrantId = 10;
}

export function accessHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const guard = (request: Request, mutating: boolean): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    if (!permissionsFor(persona).global.includes("user.manage")) {
      return error(403, "forbidden", "you do not have permission to user.manage");
    }
    return null;
  };
  const find = (id: unknown) => users.find((user) => user.id === Number(id));
  const owners = () => users.filter((user) => user.role === "owner" && user.is_active).length;
  const self = () => mockSession().user.id;

  return [
    http.get("/api/users", ({ request }) => guard(request, false) ?? HttpResponse.json(users)),
    http.post("/api/users", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as {
        username: string;
        display_name?: string;
        email?: string | null;
        role: GlobalRole;
      };
      if (users.some((user) => user.username === body.username)) {
        return error(409, "conflict", `user already exists: ${body.username}`);
      }
      if (body.role === "owner" && ROLE_OF[persona] !== "owner") {
        return error(403, "forbidden", "only an owner can manage owners");
      }
      const user: UserSummary = {
        id: nextUserId++,
        username: body.username,
        display_name: body.display_name ?? "",
        email: body.email ?? null,
        role: body.role,
        is_active: true,
        must_change_password: true,
        created_at: new Date().toISOString(),
      };
      users = [...users, user];
      const created: OneTimePassword = { user, one_time_password: MOCK_ONE_TIME_PASSWORD };
      return HttpResponse.json(created, { status: 201 });
    }),
    http.patch("/api/users/:id", async ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const user = find(params.id);
      if (!user) return error(404, "not_found", "user not found");
      const body = (await request.json()) as UpdateUserBody;
      if (
        user.id === self() &&
        (body.is_active === false || (body.role && body.role !== user.role))
      ) {
        return error(409, "conflict", "you cannot change your own role or disable yourself");
      }
      const demoting = body.role !== undefined && body.role !== user.role;
      if (
        user.role === "owner" &&
        user.is_active &&
        owners() <= 1 &&
        (demoting || body.is_active === false)
      ) {
        return error(409, "conflict", "the last owner cannot be demoted, disabled or deleted");
      }
      Object.assign(user, body);
      return HttpResponse.json(user);
    }),
    http.delete("/api/users/:id", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const user = find(params.id);
      if (!user) return error(404, "not_found", "user not found");
      if (user.id === self()) return error(409, "conflict", "you cannot delete yourself");
      users = users.filter((item) => item.id !== user.id);
      grants = grants.filter((grant) => grant.user_id !== user.id);
      return new HttpResponse(null, { status: 204 });
    }),
    http.post("/api/users/:id/reset-password", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const user = find(params.id);
      if (!user) return error(404, "not_found", "user not found");
      user.must_change_password = true;
      const reset: OneTimePassword = { user, one_time_password: MOCK_ONE_TIME_PASSWORD };
      return HttpResponse.json(reset);
    }),
    http.get("/api/users/:id/access", ({ params, request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      return HttpResponse.json(grants.filter((grant) => grant.user_id === Number(params.id)));
    }),

    http.get("/api/projects/:slug/access", ({ params, request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      if (!projectExists(String(params.slug))) return error(404, "not_found", "project not found");
      return HttpResponse.json({
        privileged: users.filter((user) => user.role !== "member"),
        grants: grants.filter((grant) => grant.project === params.slug),
      });
    }),
    http.put("/api/projects/:slug/access", async ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      if (!projectExists(String(params.slug))) return error(404, "not_found", "project not found");
      const body = (await request.json()) as SetGrantBody;
      const user = find(body.user_id);
      if (!user) return error(404, "not_found", "user not found");
      if (user.role !== "member") {
        return error(
          422,
          "validation_error",
          "owners and admins already have access to everything",
        );
      }
      const environment = body.environment ?? null;
      const service = body.service ?? null;
      const existing = grants.find(
        (grant) =>
          grant.project === params.slug &&
          grant.user_id === user.id &&
          grant.environment === environment &&
          grant.service === service,
      );
      const grant: Grant = existing ?? {
        id: nextGrantId++,
        user_id: user.id,
        username: user.username,
        display_name: user.display_name,
        project: String(params.slug),
        environment,
        service,
        role: null,
        permissions: [],
        created_at: new Date().toISOString(),
      };
      grant.role = body.role ?? null;
      grant.permissions = body.permissions ?? [];
      if (!existing) grants = [...grants, grant];
      return HttpResponse.json(grant);
    }),
    http.delete("/api/projects/:slug/access/:grantId", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const grant = grants.find((item) => item.id === Number(params.grantId));
      if (!grant) return error(404, "not_found", "grant not found");
      grants = grants.filter((item) => item.id !== grant.id);
      return new HttpResponse(null, { status: 204 });
    }),

    http.post("/api/auth/change-password", async ({ request }) => {
      if (!ctx.authenticated()) return ctx.unauthenticated();
      const body = (await request.json()) as { current_password: string; new_password: string };
      if (body.current_password === body.new_password) {
        return error(422, "validation_error", "the new password must differ from the current one");
      }
      if (body.current_password !== MOCK_ONE_TIME_PASSWORD && persona === "pending") {
        return error(422, "validation_error", "the current password is not correct");
      }
      if (body.new_password.length < 12) {
        return error(422, "validation_error", "password must be 12-1024 characters");
      }
      if (persona === "pending") persona = "owner";
      return HttpResponse.json(mockSession());
    }),
  ];
}
