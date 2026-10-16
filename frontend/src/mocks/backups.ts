import { http, HttpResponse, type HttpHandler } from "msw";
import type { DatabaseBackup, S3BackupSettings, S3BackupSettingsBody } from "../api/backupTypes";
import { mockSession } from "./access";
import { MOCK_CSRF, patchEnvironment, projects } from "./data";
import type { MockContext } from "./admin";

const date = (daysAgo: number) => new Date(Date.now() - daysAgo * 86_400_000).toISOString();
let nextId = 4;
const iamPolicy = JSON.stringify(
  {
    Version: "2012-10-17",
    Statement: [
      {
        Effect: "Allow",
        Action: [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts",
        ],
        Resource: "arn:aws:s3:::aic-consultation/backups/*",
      },
      {
        Effect: "Allow",
        Action: "s3:ListBucket",
        Resource: "arn:aws:s3:::aic-consultation",
        Condition: { StringLike: { "s3:prefix": "backups/*" } },
      },
    ],
  },
  null,
  2,
);
export let backupRows: DatabaseBackup[] = [
  {
    id: 3,
    project: "shop-api",
    environment: "production",
    connection: "main",
    trigger: "schedule",
    status: "succeeded",
    started_at: date(0),
    finished_at: date(0),
    size_bytes: 245760,
    object_key: "backups/shop-api/production/main/2026-10-16T030000.dump",
    error: null,
  },
  {
    id: 2,
    project: "shop-api",
    environment: "development",
    connection: "main",
    trigger: "manual",
    status: "failed",
    started_at: date(1),
    finished_at: date(1),
    size_bytes: null,
    object_key: null,
    error: "database backup could not be completed",
  },
];

export let s3Settings: S3BackupSettings = {
  access_key_id: "AKIAEXAMPLE",
  bucket: "aic-consultation",
  region: "eu-west-3",
  prefix: "backups",
  hour: 3,
  enabled: false,
  mysql_image: "mysql:8.4",
  mariadb_image: "mariadb:11.4",
  postgres_image: "postgres:17",
  secret_configured: true,
  iam_policy: iamPolicy,
};
export let backupSecretsSent: string[] = [];
let testResult = {
  ok: true,
  message: "S3 connection verified; the temporary test object was deleted.",
};

export function resetBackupsMock() {
  nextId = 4;
  backupSecretsSent = [];
  testResult = {
    ok: true,
    message: "S3 connection verified; the temporary test object was deleted.",
  };
  backupRows = [
    {
      id: 3,
      project: "shop-api",
      environment: "production",
      connection: "main",
      trigger: "schedule",
      status: "succeeded",
      started_at: date(0),
      finished_at: date(0),
      size_bytes: 245760,
      object_key: "backups/shop-api/production/main/2026-10-16T030000.dump",
      error: null,
    },
    {
      id: 2,
      project: "shop-api",
      environment: "development",
      connection: "main",
      trigger: "manual",
      status: "failed",
      started_at: date(1),
      finished_at: date(1),
      size_bytes: null,
      object_key: null,
      error: "database backup could not be completed",
    },
  ];
  s3Settings = { ...s3Settings, secret_configured: true };
}

function denied(request: Request, context: MockContext, adminOnly = false) {
  if (!context.authenticated()) return context.unauthenticated();
  if (request.method !== "GET" && request.headers.get("X-CSRF-Token") !== MOCK_CSRF) {
    return context.error(403, "forbidden", "invalid CSRF token");
  }
  const role = mockSession().user.role;
  if (adminOnly && role !== "admin" && role !== "owner")
    return context.error(403, "forbidden", "you do not have permission");
  return null;
}

export function backupHandlers(context: MockContext): HttpHandler[] {
  return [
    http.get("/api/backups", ({ request }) => {
      const issue = denied(request, context);
      if (issue) return issue;
      return HttpResponse.json(
        mockSession().user.role === "admin" || mockSession().user.role === "owner"
          ? backupRows
          : backupRows.filter((row) => row.project === "shop-api"),
      );
    }),
    http.get("/api/projects/:slug/environments/:env/backups", ({ params, request }) => {
      const issue = denied(request, context);
      if (issue) return issue;
      if (!projects().some((project) => project.slug === params.slug))
        return context.error(404, "not_found", "project not found");
      return HttpResponse.json(
        backupRows.filter((row) => row.project === params.slug && row.environment === params.env),
      );
    }),
    http.post(
      "/api/projects/:slug/environments/:env/databases/:connection/backups",
      ({ params, request }) => {
        const issue = denied(request, context);
        if (issue) return issue;
        const session = mockSession();
        const permissions = session.permissions;
        const scoped =
          permissions.all ||
          permissions.projects[String(params.slug)]?.environments[
            params.env as "development" | "production"
          ]?.actions.includes("backup.run");
        if (!scoped) return context.error(403, "forbidden", "you do not have permission");
        const row: DatabaseBackup = {
          id: nextId++,
          project: String(params.slug),
          environment: params.env as DatabaseBackup["environment"],
          connection: String(params.connection),
          trigger: "manual",
          status: "running",
          started_at: new Date().toISOString(),
          finished_at: null,
          size_bytes: null,
          object_key: null,
          error: null,
        };
        backupRows = [row, ...backupRows];
        return HttpResponse.json(row, { status: 202 });
      },
    ),
    http.patch("/api/projects/:slug/environments/:env/backups", async ({ params, request }) => {
      const issue = denied(request, context);
      if (issue) return issue;
      const permissions = mockSession().permissions;
      const scoped =
        permissions.all ||
        permissions.projects[String(params.slug)]?.environments[
          params.env as "development" | "production"
        ]?.actions.includes("backup.run");
      if (!scoped) return context.error(403, "forbidden", "you do not have permission");
      const body = (await request.json()) as { include_in_backups: boolean };
      const result = patchEnvironment(
        String(params.slug),
        params.env as "development" | "production",
        body,
      );
      if (!result) return context.error(404, "not_found", "project not found");
      return HttpResponse.json(result);
    }),
    http.get("/api/integrations/s3-backups", ({ request }) => {
      const issue = denied(request, context, true);
      return issue ?? HttpResponse.json(s3Settings);
    }),
    http.put("/api/integrations/s3-backups", async ({ request }) => {
      const issue = denied(request, context, true);
      if (issue) return issue;
      const body = (await request.json()) as S3BackupSettingsBody;
      if (body.secret_key) {
        backupSecretsSent.push(body.secret_key);
        s3Settings.secret_configured = true;
      }
      delete body.secret_key;
      s3Settings = { ...s3Settings, ...body };
      return HttpResponse.json(s3Settings);
    }),
    http.post("/api/integrations/s3-backups/test", ({ request }) => {
      const issue = denied(request, context, true);
      return issue ?? HttpResponse.json(testResult);
    }),
  ];
}
