/**
 * Mock of the account endpoints (`/api/me/*`), the audit log and the dashboard. Shapes and
 * rules follow docs/api-contract.md: wrong password and wrong code are 422 (never 401, which
 * would sign the user out), the recovery codes appear only in the response that creates them.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import type {
  AccountSession,
  AuditEvent,
  Dashboard,
  Profile,
  TwoFactorStatus,
} from "../api/accountTypes";
import { MOCK_CSRF, MOCK_PASSWORD, MOCK_USERNAME } from "./data";
import type { MockContext } from "./admin";

export const MOCK_TOTP_CODE = "123456";
export const MOCK_TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP";
export const MOCK_RECOVERY_CODES = [
  "abcd-efgh-2345-6723",
  "kjhg-fdsa-2345-6724",
  "qwer-tyui-2345-6725",
  "zxcv-bnmq-2345-6726",
  "plmo-knij-2345-6727",
  "ijnu-hbyg-2345-6728",
  "vftc-rdxe-2345-6729",
  "szaw-qwer-2345-6732",
  "tyui-opas-2345-6733",
  "dfgh-jklz-2345-6734",
];

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();
const ahead = (ms: number): string => new Date(Date.now() + ms).toISOString();

interface AccountState {
  profile: Profile;
  password: string;
  sessions: AccountSession[];
  twoFactor: TwoFactorStatus;
  pending: boolean;
  audit: AuditEvent[];
  dashboard: Dashboard;
}

function initialAudit(): AuditEvent[] {
  const events: AuditEvent[] = [];
  const add = (
    action: string,
    description: string,
    actor: string,
    actor_type: AuditEvent["actor_type"],
    target_type: string,
    target_id: string | null,
    details: Record<string, unknown>,
    age: number,
  ) => {
    events.unshift({
      id: events.length + 1,
      occurred_at: ago(age),
      actor_type,
      actor_id: actor_type === "user" ? "1" : null,
      actor,
      action,
      description,
      target_type,
      target_id,
      details,
    });
  };
  add(
    "auth.login",
    "Signed in",
    MOCK_USERNAME,
    "user",
    "user",
    "1",
    { username: "admin" },
    30 * DAY,
  );
  add(
    "project.create",
    "Created the project shop-api for acme/shop-api",
    MOCK_USERNAME,
    "user",
    "project",
    "shop-api",
    { slug: "shop-api" },
    29 * DAY,
  );
  add(
    "secret.create",
    "Created the secret DATABASE_URL",
    MOCK_USERNAME,
    "user",
    "environment",
    "1",
    { name: "DATABASE_URL" },
    28 * DAY,
  );
  add(
    "token.create",
    'Created the API token "CI deploy" with scopes read, run',
    MOCK_USERNAME,
    "user",
    "api_token",
    "1",
    { name: "CI deploy", scopes: ["read", "run"] },
    20 * DAY,
  );
  add(
    "run.create",
    "Started run #12 (push) on development at a1b2c3d",
    "GitHub",
    "system",
    "run",
    "12",
    { number: 12, trigger: "push" },
    3 * DAY,
  );
  for (let index = 0; index < 60; index += 1) {
    add(
      "auth.login",
      "Signed in",
      MOCK_USERNAME,
      "user",
      "user",
      "1",
      { username: "admin" },
      index * HOUR + HOUR,
    );
  }
  add(
    "secret.reveal",
    "Revealed the secret DATABASE_URL",
    MOCK_USERNAME,
    "user",
    "environment",
    "1",
    { name: "DATABASE_URL" },
    10 * MINUTE,
  );
  return events
    .sort((a, b) => Date.parse(b.occurred_at) - Date.parse(a.occurred_at))
    .map((event, index, all) => ({ ...event, id: all.length - index }));
}

function initialDashboard(): Dashboard {
  return {
    project_count: 2,
    domain_count: 2,
    deployments: [
      {
        id: 7,
        project: "shop-api",
        project_name: "Shop API",
        environment: "production",
        status: "succeeded",
        commit_sha: "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        run_id: 30,
        run_number: 12,
        trigger: "push",
        triggered_by: MOCK_USERNAME,
        created_at: ago(2 * HOUR),
        started_at: ago(2 * HOUR),
        finished_at: ago(2 * HOUR - 83_000),
        duration_seconds: 83,
      },
      {
        id: 6,
        project: "shop-api",
        project_name: "Shop API",
        environment: "development",
        status: "rolled_back",
        commit_sha: "0123456789abcdef0123456789abcdef01234567",
        run_id: 25,
        run_number: 9,
        trigger: "manual",
        triggered_by: "token CI deploy",
        created_at: ago(5 * HOUR),
        started_at: ago(5 * HOUR),
        finished_at: ago(5 * HOUR - 41_000),
        duration_seconds: 41,
      },
    ],
    attention: [
      {
        run_id: 31,
        project: "shop-api",
        project_name: "Shop API",
        environment: "development",
        number: 13,
        status: "running",
        branch: "development",
        commit_sha: "feedfacefeedfacefeedfacefeedfacefeedface",
        trigger: "push",
        created_at: ago(2 * MINUTE),
        started_at: ago(2 * MINUTE),
        finished_at: null,
        message: null,
      },
      {
        run_id: 28,
        project: "blog",
        project_name: "Blog",
        environment: "production",
        number: 4,
        status: "failed",
        branch: "production",
        commit_sha: "badc0ffee0ddf00dbadc0ffee0ddf00dbadc0ffe",
        trigger: "manual",
        created_at: ago(DAY),
        started_at: ago(DAY),
        finished_at: ago(DAY - 12_000),
        message: "Stage Build failed",
      },
    ],
    manual_deploys: [
      {
        project: "blog",
        project_name: "Blog",
        environment: "development",
        branch: "development",
        pushes: 3,
        last_push_at: ago(3 * HOUR),
      },
    ],
    certificates: [
      {
        hostname: "shop.example.com",
        project: "shop-api",
        environment: "production",
        not_after: ahead(9 * DAY),
        expired: false,
      },
    ],
    host: {
      timestamp: ago(5_000),
      cpu_percent: 37.5,
      cpu_count: 8,
      load_1: 1.25,
      memory_used: 4 * 1024 ** 3,
      memory_total: 16 * 1024 ** 3,
      memory_percent: 25,
      disk_used: 80 * 1024 ** 3,
      disk_total: 100 * 1024 ** 3,
      disk_percent: 80,
    },
    host_error: null,
  };
}

function initialState(): AccountState {
  return {
    profile: {
      id: 1,
      username: MOCK_USERNAME,
      display_name: "",
      email: null,
      created_at: ago(30 * DAY),
      two_factor_enabled: false,
    },
    password: MOCK_PASSWORD,
    sessions: [
      {
        id: 1,
        created_at: ago(HOUR),
        last_seen_at: ago(MINUTE),
        expires_at: ahead(11 * HOUR),
        ip_address: "203.0.113.7",
        user_agent: "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
        client: "Firefox 130 on Linux",
        current: true,
      },
      {
        id: 2,
        created_at: ago(5 * HOUR),
        last_seen_at: ago(40 * MINUTE),
        expires_at: ahead(7 * HOUR),
        ip_address: "198.51.100.23",
        user_agent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Safari/604.1",
        client: "Safari 17 on iOS",
        current: false,
      },
    ],
    twoFactor: { enabled: false, enabled_at: null, recovery_codes_left: 0 },
    pending: false,
    audit: initialAudit(),
    dashboard: initialDashboard(),
  };
}

let state = initialState();

export function resetAccountMock(): void {
  state = initialState();
}

/** Tests shape the dashboard (empty states, failures) through this. */
export function setMockDashboard(change: (dashboard: Dashboard) => void): void {
  change(state.dashboard);
}

export function mockTwoFactorEnabled(): boolean {
  return state.twoFactor.enabled;
}

function matches(event: AuditEvent, params: URLSearchParams): boolean {
  const q = params.get("q")?.toLowerCase();
  if (q && !`${event.action} ${event.description} ${event.actor}`.toLowerCase().includes(q)) {
    return false;
  }
  const exact = (name: string, value: string | null) =>
    !params.get(name) || params.get(name) === value;
  if (!exact("actor_type", event.actor_type)) return false;
  if (!exact("target_type", event.target_type)) return false;
  if (!exact("target_id", event.target_id)) return false;
  const action = params.get("action");
  if (action) {
    const ok = action.endsWith("*")
      ? event.action.startsWith(action.slice(0, -1))
      : event.action === action;
    if (!ok) return false;
  }
  const since = params.get("since");
  if (since && Date.parse(event.occurred_at) < Date.parse(since)) return false;
  const until = params.get("until");
  if (until && Date.parse(event.occurred_at) >= Date.parse(until)) return false;
  return true;
}

function pageOf(events: AuditEvent[], params: URLSearchParams) {
  const limit = Math.min(200, Number(params.get("limit") ?? 50));
  const cursor = params.get("cursor");
  const before = cursor ? Number(cursor.replace(/^c/, "")) : Infinity;
  const rows = events.filter((event) => event.id < before);
  const items = rows.slice(0, limit);
  const last = items[items.length - 1];
  return { items, next_cursor: rows.length > limit && last ? `c${String(last.id)}` : null };
}

export function accountHandlers(ctx: MockContext): HttpHandler[] {
  const { error } = ctx;
  const guard = (request: Request, mutating: boolean): Response | null => {
    if (!ctx.authenticated()) return ctx.unauthenticated();
    if (mutating && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
      return error(403, "forbidden", "invalid CSRF token");
    }
    return null;
  };
  const log = (action: string, description: string, details: Record<string, unknown> = {}) => {
    const id = Math.max(0, ...state.audit.map((event) => event.id)) + 1;
    state.audit.unshift({
      id,
      occurred_at: new Date().toISOString(),
      actor_type: "user",
      actor_id: "1",
      actor: MOCK_USERNAME,
      action,
      description,
      target_type: "user",
      target_id: "1",
      details,
    });
  };

  return [
    http.get("/api/me", ({ request }) => guard(request, false) ?? HttpResponse.json(state.profile)),
    http.patch("/api/me", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as { display_name: string | null; email: string | null };
      const email = body.email?.trim() ?? "";
      if (email !== "" && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
        return error(422, "validation_error", "enter a valid email address");
      }
      state.profile = {
        ...state.profile,
        display_name: body.display_name?.trim() ?? "",
        email: email || null,
      };
      log("user.profile_update", "Updated the profile");
      return HttpResponse.json(state.profile);
    }),
    http.get(
      "/api/me/sessions",
      ({ request }) => guard(request, false) ?? HttpResponse.json(state.sessions),
    ),
    http.post("/api/me/sessions/revoke-others", ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const ended = state.sessions.filter((session) => !session.current).length;
      state.sessions = state.sessions.filter((session) => session.current);
      return HttpResponse.json({ sessions_ended: ended });
    }),
    http.delete("/api/me/sessions/:id", ({ params, request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const target = state.sessions.find((session) => session.id === Number(params.id));
      if (!target) return error(404, "not_found", "session not found");
      if (target.current) {
        return error(422, "validation_error", "this is your current session; sign out instead");
      }
      state.sessions = state.sessions.filter((session) => session !== target);
      return new HttpResponse(null, { status: 204 });
    }),

    http.get(
      "/api/me/two-factor",
      ({ request }) => guard(request, false) ?? HttpResponse.json(state.twoFactor),
    ),
    http.post("/api/me/two-factor/enroll", ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      if (state.twoFactor.enabled) {
        return error(409, "conflict", "two-factor authentication is already enabled");
      }
      state.pending = true;
      return HttpResponse.json({
        secret: MOCK_TOTP_SECRET,
        uri: `otpauth://totp/Jendock%3A${MOCK_USERNAME}?secret=${MOCK_TOTP_SECRET}&issuer=Jendock&algorithm=SHA1&digits=6&period=30`,
      });
    }),
    http.post("/api/me/two-factor/enable", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as { code: string };
      if (!state.pending) return error(422, "validation_error", "start enrollment first");
      if (body.code.replace(/\s/g, "") !== MOCK_TOTP_CODE) {
        return error(422, "validation_error", "the code is incorrect");
      }
      state.pending = false;
      state.twoFactor = {
        enabled: true,
        enabled_at: new Date().toISOString(),
        recovery_codes_left: 10,
      };
      state.profile = { ...state.profile, two_factor_enabled: true };
      log("two_factor.enable", "Turned on two-factor authentication");
      return HttpResponse.json({ recovery_codes: MOCK_RECOVERY_CODES });
    }),
    http.post("/api/me/two-factor/recovery-codes", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as { code: string };
      if (body.code !== MOCK_TOTP_CODE)
        return error(422, "validation_error", "the code is incorrect");
      state.twoFactor = { ...state.twoFactor, recovery_codes_left: 10 };
      return HttpResponse.json({ recovery_codes: [...MOCK_RECOVERY_CODES].reverse() });
    }),
    http.post("/api/me/two-factor/disable", async ({ request }) => {
      const denied = guard(request, true);
      if (denied) return denied;
      const body = (await request.json()) as { password: string; code: string };
      if (body.password !== state.password) {
        return error(422, "validation_error", "the password is incorrect");
      }
      if (body.code !== MOCK_TOTP_CODE)
        return error(422, "validation_error", "the code is incorrect");
      state.twoFactor = { enabled: false, enabled_at: null, recovery_codes_left: 0 };
      state.profile = { ...state.profile, two_factor_enabled: false };
      log("two_factor.disable", "Turned off two-factor authentication");
      return new HttpResponse(null, { status: 204 });
    }),

    http.get("/api/me/security-log", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      const params = new URL(request.url).searchParams;
      return HttpResponse.json(
        pageOf(
          state.audit.filter((event) => event.actor_type === "user"),
          params,
        ),
      );
    }),
    http.get("/api/audit/actions", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      const actions = [...new Set(state.audit.map((event) => event.action))].sort();
      return HttpResponse.json({ actions });
    }),
    http.get("/api/audit", ({ request }) => {
      const denied = guard(request, false);
      if (denied) return denied;
      const params = new URL(request.url).searchParams;
      return HttpResponse.json(
        pageOf(
          state.audit.filter((event) => matches(event, params)),
          params,
        ),
      );
    }),
    http.get(
      "/api/dashboard",
      ({ request }) => guard(request, false) ?? HttpResponse.json(state.dashboard),
    ),
  ];
}
