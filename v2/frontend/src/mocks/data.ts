import { resetPipelines } from "./pipelines";
import type {
  EnvironmentName,
  LogChunk,
  Project,
  RunDetail,
  RunStatus,
  RunSummary,
  StageRun,
  StepRun,
} from "../api/types";

/** Fake credentials accepted by the mock API (development only). */
export const MOCK_USERNAME = "admin";
export const MOCK_PASSWORD = "mock-password-12345";
export const MOCK_CSRF = "mock-csrf-token";
export const MOCK_LOCKED_USERNAME = "locked";

const MINUTE = 60_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();

interface ProjectSeed {
  slug: string;
  name: string;
  repository: string;
  development: { branch: string; autoDeploy: boolean };
  production: { branch: string; autoDeploy: boolean };
}

const SEEDS: ProjectSeed[] = [
  {
    slug: "shop-api",
    name: "Shop API",
    repository: "acme/shop-api",
    development: { branch: "develop", autoDeploy: true },
    production: { branch: "main", autoDeploy: false },
  },
  {
    slug: "marketing-site",
    name: "Marketing site",
    repository: "acme/marketing-site",
    development: { branch: "main", autoDeploy: true },
    production: { branch: "release", autoDeploy: true },
  },
  {
    slug: "billing-worker",
    name: "Billing worker",
    repository: "acme/billing-worker",
    development: { branch: "develop", autoDeploy: true },
    production: { branch: "main", autoDeploy: false },
  },
  {
    slug: "internal-tools",
    name: "Internal tools",
    repository: "acme/internal-tools",
    development: { branch: "main", autoDeploy: false },
    production: { branch: "main", autoDeploy: false },
  },
];

const sha = (n: number): string =>
  (n.toString(16).padStart(8, "0") + "a1b2c3d4e5f60718293a4b5c6d7e8f90").slice(0, 40);

interface RunSeed {
  id: number;
  slug: string;
  env: EnvironmentName;
  number: number;
  status: RunStatus;
  trigger: RunSummary["trigger"];
  createdAgo: number;
  durationMs: number | null;
  branch?: string;
  commit?: string;
}

function branchOf(slug: string, env: EnvironmentName): string {
  const seed = SEEDS.find((item) => item.slug === slug);
  return seed ? seed[env].branch : "main";
}

function summary(seed: RunSeed): RunSummary {
  const created = ago(seed.createdAgo);
  const started = seed.status === "queued" ? null : ago(seed.createdAgo - 5000);
  const finished =
    seed.durationMs === null || seed.status === "queued" || seed.status === "running"
      ? null
      : new Date(Date.parse(created) + 5000 + seed.durationMs).toISOString();
  return {
    id: seed.id,
    number: seed.number,
    project: seed.slug,
    environment: seed.env,
    trigger: seed.trigger,
    branch: seed.branch ?? branchOf(seed.slug, seed.env),
    commit_sha: seed.commit ?? sha(seed.id * 7919),
    pipeline_source: seed.id % 2 === 0 ? "repository" : "platform",
    status: seed.status,
    message: seed.status === "failed" ? "Stage Test failed" : null,
    cancel_requested_at: null,
    created_at: created,
    started_at: started,
    finished_at: finished,
  };
}

function seedRuns(): RunSeed[] {
  const featured: RunSeed[] = [
    {
      id: 101,
      slug: "shop-api",
      env: "development",
      number: 42,
      status: "running",
      trigger: "push",
      createdAgo: 2 * MINUTE,
      durationMs: null,
    },
    {
      id: 102,
      slug: "shop-api",
      env: "production",
      number: 17,
      status: "succeeded",
      trigger: "manual",
      createdAgo: 3 * 60 * MINUTE,
      durationMs: 4 * MINUTE + 12_000,
    },
    {
      id: 103,
      slug: "marketing-site",
      env: "development",
      number: 8,
      status: "failed",
      trigger: "push",
      createdAgo: 25 * MINUTE,
      durationMs: 95_000,
    },
    {
      id: 104,
      slug: "marketing-site",
      env: "production",
      number: 5,
      status: "cancelled",
      trigger: "manual",
      createdAgo: 26 * 60 * MINUTE,
      durationMs: 31_000,
    },
    {
      id: 105,
      slug: "billing-worker",
      env: "development",
      number: 3,
      status: "queued",
      trigger: "push",
      createdAgo: 40_000,
      durationMs: null,
    },
    {
      id: 106,
      slug: "billing-worker",
      env: "production",
      number: 9,
      status: "interrupted",
      trigger: "redeploy",
      createdAgo: 2 * 24 * 60 * MINUTE,
      durationMs: 62_000,
    },
  ];
  const history: RunSeed[] = Array.from({ length: 45 }, (_, index): RunSeed => ({
    id: 200 + index,
    slug: "shop-api",
    env: "development",
    number: 41 - index,
    status: index % 5 === 3 ? "failed" : "succeeded",
    trigger: index % 4 === 0 ? "manual" : "push",
    createdAgo: (index + 1) * 90 * MINUTE,
    durationMs: 120_000 + index * 3000,
  })).filter((run) => run.number > 0);
  return [...featured, ...history];
}

let runSeeds = seedRuns();

export function resetMockData(): void {
  resetPipelines();
  runSeeds = seedRuns();
  cancelled.clear();
}

const cancelled = new Map<number, "cancelled" | "requested">();

export function allRuns(): RunSummary[] {
  return runSeeds
    .map(summary)
    .map((run) => {
      const state = cancelled.get(run.id);
      if (state === "cancelled") return { ...run, status: "cancelled" as const };
      if (state === "requested") return { ...run, cancel_requested_at: new Date().toISOString() };
      return run;
    })
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
}

export function cancelRun(id: number): "cancelled" | "requested" | "missing" | "finished" {
  const run = allRuns().find((item) => item.id === id);
  if (!run) return "missing";
  if (run.status !== "queued" && run.status !== "running") return "finished";
  const outcome = run.status === "queued" ? "cancelled" : "requested";
  cancelled.set(id, outcome);
  return outcome;
}

let nextRunId = 900;
export function startRun(
  slug: string,
  env: EnvironmentName,
  branch: string,
  commit: string,
): RunSummary {
  const number =
    Math.max(0, ...runSeeds.filter((r) => r.slug === slug && r.env === env).map((r) => r.number)) +
    1;
  const seed: RunSeed = {
    id: nextRunId++,
    slug,
    env,
    number,
    status: "queued",
    trigger: "manual",
    createdAgo: 0,
    durationMs: null,
    branch,
    commit,
  };
  runSeeds = [seed, ...runSeeds];
  return summary(seed);
}

export function runsFor(slug: string, env: EnvironmentName): RunSummary[] {
  return allRuns().filter((run) => run.project === slug && run.environment === env);
}

export function projects(): Project[] {
  return SEEDS.map((seed) => ({
    slug: seed.slug,
    name: seed.name,
    repository: seed.repository,
    created_at: ago(40 * 24 * 60 * MINUTE),
    environments: (["development", "production"] as const).map((name) => ({
      name,
      branch: seed[name].branch,
      auto_deploy: seed[name].autoDeploy,
      last_run: runsFor(seed.slug, name)[0] ?? null,
    })),
  }));
}

export function head(branch: string): {
  branch: string;
  commit_sha: string;
  message: string;
  author: string;
  committed_at: string;
} | null {
  if (branch === "missing") return null;
  return {
    branch,
    commit_sha: sha(branch.length * 104729),
    message: "Fix rounding of order totals",
    author: "Dana Reyes",
    committed_at: ago(35 * MINUTE),
  };
}

// ---- run details ------------------------------------------------------------------------

interface StageSpec {
  name: string;
  column: number;
  status: RunStatus;
  steps: { type: string; label: string; status: RunStatus; exit?: number }[];
}

function stepsOf(runId: number, stageIndex: number, spec: StageSpec, base: string): StepRun[] {
  return spec.steps.map((step, k) => ({
    id: runId * 1000 + stageIndex * 10 + k,
    position: k,
    step_type: step.type,
    label: step.label,
    status: step.status,
    exit_code: step.exit ?? (step.status === "succeeded" ? 0 : null),
    started_at: step.status === "queued" ? null : base,
    finished_at:
      step.status === "succeeded" || step.status === "failed"
        ? new Date(Date.parse(base) + 40_000).toISOString()
        : null,
  }));
}

function specsFor(status: RunStatus): StageSpec[] {
  const ok = (type: string, label: string) => ({ type, label, status: "succeeded" as const });
  if (status === "running") {
    return [
      {
        name: "Build image",
        column: 0,
        status: "succeeded",
        steps: [ok("dockerBuild", "dockerBuild shop-api")],
      },
      {
        name: "Unit tests",
        column: 1,
        status: "running",
        steps: [{ type: "sh", label: "npm test", status: "running" }],
      },
      { name: "Lint", column: 1, status: "succeeded", steps: [ok("sh", "npm run lint")] },
      {
        name: "Deploy",
        column: 2,
        status: "queued",
        steps: [
          { type: "deploy", label: "deploy api", status: "queued" },
          { type: "healthCheck", label: "healthCheck /up", status: "queued" },
        ],
      },
    ];
  }
  if (status === "failed") {
    return [
      {
        name: "Build",
        column: 0,
        status: "succeeded",
        steps: [ok("sh", "npm ci"), ok("sh", "npm run build")],
      },
      {
        name: "Test",
        column: 1,
        status: "failed",
        steps: [{ type: "sh", label: "npm test", status: "failed", exit: 1 }],
      },
      {
        name: "Deploy",
        column: 2,
        status: "cancelled",
        steps: [{ type: "deploy", label: "deploy web", status: "cancelled" }],
      },
    ];
  }
  if (status === "queued") return [];
  const state = status === "succeeded" ? "succeeded" : status;
  return [
    {
      name: "Build",
      column: 0,
      status: "succeeded",
      steps: [ok("dockerBuild", "dockerBuild app")],
    },
    {
      name: "Deploy",
      column: 1,
      status: state,
      steps: [{ type: "deploy", label: "deploy app", status: state }],
    },
  ];
}

export function detailFor(id: number): RunDetail | null {
  const run = allRuns().find((item) => item.id === id);
  if (!run) return null;
  const base = run.started_at ?? run.created_at;
  const stages: StageRun[] = specsFor(run.status).map((spec, index) => ({
    id: id * 100 + index,
    position: index,
    column: spec.column,
    name: spec.name,
    status: spec.status,
    started_at: spec.status === "queued" ? null : base,
    finished_at:
      spec.status === "succeeded" || spec.status === "failed"
        ? new Date(Date.parse(base) + 45_000).toISOString()
        : null,
    steps: stepsOf(id, index, spec, base),
  }));
  return {
    ...run,
    stages,
  };
}

const COLOUR = (code: number, text: string): string => `\u001b[${code}m${text}\u001b[0m`;

export function logText(stepId: number, status: RunStatus): string {
  if (status === "queued") return "";
  const lines = [
    COLOUR(1, "$ npm test"),
    "> shop-api@1.0.0 test",
    "> vitest run",
    "",
    ...Array.from(
      { length: 24 },
      (_, i) => ` ${COLOUR(32, "PASS")} tests/unit/case-${i + 1}.test.ts (${(i % 7) + 2} tests)`,
    ),
  ];
  if (status === "failed") {
    lines.push(
      ` ${COLOUR(31, "FAIL")} tests/unit/totals.test.ts`,
      COLOUR(31, "AssertionError: expected 41.99 to equal 42"),
      "Command exited with code 1",
    );
  } else if (status === "succeeded") {
    lines.push("", COLOUR(32, "All tests passed"));
  }
  if (stepId % 11 === 0) return "";
  return lines.join("\n") + "\n";
}

export function logChunk(
  stepId: number,
  status: RunStatus,
  offset: number,
  limit = 2048,
): LogChunk {
  const bytes = new TextEncoder().encode(logText(stepId, status));
  const end = Math.min(bytes.length, offset + limit);
  const data = new TextDecoder().decode(bytes.slice(offset, end));
  const terminal = status !== "running" && status !== "queued";
  return {
    offset,
    next_offset: end,
    size: end - offset,
    text: data,
    complete: terminal && end >= bytes.length,
  };
}
