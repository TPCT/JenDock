import type { Diagnostic } from "../api/types";
import type {
  ArgDef,
  Call,
  Catalog,
  Condition,
  Model,
  Post,
  Stage,
  StepDef,
  Value,
} from "../api/pipelineTypes";

// ---- catalogue (mirrors shiftcodes/pipeline/steps.py; the real one comes from the server) ----

function arg(name: string, type: ArgDef["type"], extra: Partial<ArgDef> = {}): ArgDef {
  return {
    name,
    type,
    required: false,
    default: null,
    enum: null,
    min: null,
    max: null,
    description: "",
    multiline: false,
    role: null,
    ...extra,
  };
}

function def(
  name: string,
  title: string,
  description: string,
  args: ArgDef[] = [],
  extra: Partial<StepDef> = {},
): StepDef {
  return { name, title, description, takes_block: false, needs_container: false, args, ...extra };
}

const TIMEOUT_ARGS = [
  arg("time", "int", { required: true, min: 1, description: "How long to wait before giving up." }),
  arg("unit", "string", {
    default: "MINUTES",
    enum: ["SECONDS", "MINUTES", "HOURS"],
    description: "Unit of the time value.",
  }),
];
const RETRY_ARGS = [
  arg("count", "int", {
    required: true,
    min: 1,
    max: 10,
    description: "Total number of attempts.",
  }),
];

export const CATALOG: Catalog = {
  steps: [
    def(
      "sh",
      "Run a shell command",
      "Runs a command inside a container. Needs a Docker agent.",
      [
        arg("script", "string", {
          required: true,
          multiline: true,
          description: "The command to run.",
        }),
        arg("label", "string", { description: "Short name shown in the log." }),
      ],
      { needs_container: true },
    ),
    def("echo", "Print a message", "Writes a line to the log.", [
      arg("message", "string", { required: true, description: "Text to print." }),
    ]),
    def("error", "Fail the run", "Stops the run with an error message.", [
      arg("message", "string", { required: true, description: "Why the run is stopped." }),
    ]),
    def(
      "timeout",
      "Limit the time",
      "Stops the steps inside if they take too long.",
      TIMEOUT_ARGS,
      {
        takes_block: true,
      },
    ),
    def("retry", "Retry on failure", "Repeats the steps inside until they succeed.", RETRY_ARGS, {
      takes_block: true,
    }),
    def(
      "dir",
      "Work in a folder",
      "Runs the steps inside in a sub-folder of the checkout.",
      [arg("path", "string", { required: true, description: "Relative folder, for example web." })],
      { takes_block: true },
    ),
    def(
      "withEnv",
      "Set variables",
      "Adds environment variables for the steps inside.",
      [arg("overrides", "list", { required: true, description: "Entries like NAME=value." })],
      { takes_block: true },
    ),
    def("dockerBuild", "Build an image", "Builds a Docker image from your repository.", [
      arg("image", "string", {
        required: true,
        role: "produces_image",
        description: "A name for the image, for example web.",
      }),
      arg("dockerfile", "string", {
        default: "Dockerfile",
        description: "Path of the Dockerfile.",
      }),
      arg("context", "string", {
        default: ".",
        description: "Folder sent to Docker as build context.",
      }),
      arg("target", "string", { description: "Build stage to stop at (optional)." }),
      arg("args", "map", { description: "Build arguments." }),
    ]),
    def("deploy", "Deploy a service", "Starts a service from an image built earlier.", [
      arg("service", "string", { description: "Service name." }),
      arg("image", "string", {
        role: "uses_image",
        description: "Image built by an earlier step.",
      }),
      arg("services", "map", { description: "Several services at once: service name to image." }),
    ]),
    def("healthCheck", "Check a URL", "Waits until a URL answers with the expected status.", [
      arg("url", "string", { required: true, description: "Address starting with https://." }),
      arg("expectStatus", "int", {
        default: 200,
        min: 100,
        max: 599,
        description: "HTTP status that means healthy.",
      }),
      arg("timeout", "int", {
        default: 60,
        min: 1,
        max: 3600,
        description: "Seconds to keep trying.",
      }),
    ]),
  ],
  options: [
    def("timeout", "Time limit", "Stops the run if it takes too long.", TIMEOUT_ARGS),
    def("retry", "Retries", "Repeats the whole stage on failure.", RETRY_ARGS),
    def(
      "disableConcurrentBuilds",
      "One run at a time",
      "Does not start this pipeline twice in parallel.",
    ),
    def("skipDefaultCheckout", "Skip checkout", "Does not fetch the repository automatically."),
  ],
  parameters: [
    def("string", "Text parameter", "Asks for a text value.", [
      arg("name", "string", { required: true, description: "Variable name." }),
      arg("defaultValue", "string"),
      arg("description", "string"),
    ]),
    def("booleanParam", "Yes/no parameter", "Asks for a yes/no value.", [
      arg("name", "string", { required: true, description: "Variable name." }),
      arg("defaultValue", "bool"),
      arg("description", "string"),
    ]),
    def("choice", "Choice parameter", "Asks to pick one value.", [
      arg("name", "string", { required: true, description: "Variable name." }),
      arg("choices", "list", { required: true, description: "The allowed values." }),
      arg("description", "string"),
    ]),
  ],
  post_conditions: [
    { name: "always", description: "Every time" },
    { name: "changed", description: "When the result differs from the last run" },
    { name: "aborted", description: "When the run is cancelled" },
    { name: "failure", description: "When the run fails" },
    { name: "success", description: "When the run succeeds" },
    { name: "unstable", description: "When the run is unstable" },
    { name: "cleanup", description: "At the very end, whatever happened" },
  ],
};

// ---- a small renderer so the editor shows believable text -------------------------------------

const quote = (text: string): string => `'${text.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;

function value(v: Value): string {
  if (typeof v === "string") return quote(v);
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return `[${v.map(value).join(", ")}]`;
  if ("template" in v) return `"${v.template.replace(/"/g, '\\"')}"`;
  return v.map.length === 0
    ? "[:]"
    : `[${v.map.map((e) => `${e.key}: ${value(e.value)}`).join(", ")}]`;
}

function call(c: Call, pad: string): string[] {
  const entries = Object.entries(c.args);
  let head = c.name;
  if (c.name === "sh" || c.name === "echo" || c.name === "error") {
    const first = entries[0];
    head =
      entries.length === 1 && first
        ? `${c.name} ${value(first[1])}`
        : `${c.name}(${entries.map(([k, v]) => `${k}: ${value(v)}`).join(", ")})`;
  } else if (entries.length > 0 || c.body) {
    head = `${c.name}(${entries.map(([k, v]) => `${k}: ${value(v)}`).join(", ")})`;
  }
  if (!c.body) return [pad + head];
  return [`${pad}${head} {`, ...c.body.flatMap((inner) => call(inner, pad + "    ")), `${pad}}`];
}

function condition(c: Condition): string {
  switch (c.type) {
    case "branch":
      return `branch ${value(c.pattern)}`;
    case "environment":
      return `environment name: ${quote(c.name)}, value: ${value(c.value)}`;
    case "not":
      return `not { ${condition(c.condition)} }`;
    default:
      return `${c.type} { ${c.conditions.map(condition).join("; ")} }`;
  }
}

function post(p: Post | null, pad: string): string[] {
  if (!p) return [];
  return [
    `${pad}post {`,
    ...p.blocks.flatMap((b) => [
      `${pad}    ${b.condition} {`,
      ...b.steps.flatMap((s) => call(s, pad + "        ")),
      `${pad}    }`,
    ]),
    `${pad}}`,
  ];
}

function stage(s: Stage, pad: string): string[] {
  const inner = pad + "    ";
  const body =
    s.body.type === "steps"
      ? [`${inner}steps {`, ...s.body.steps.flatMap((c) => call(c, inner + "    ")), `${inner}}`]
      : [
          `${inner}${s.body.type === "parallel" ? "parallel" : "stages"} {`,
          ...s.body.stages.flatMap((child) => stage(child, inner + "    ")),
          `${inner}}`,
        ];
  return [
    `${pad}stage(${quote(s.name)}) {`,
    ...(s.agent
      ? [
          `${inner}agent ${s.agent.type === "docker" ? `{ docker ${value(s.agent.image)} }` : s.agent.type}`,
        ]
      : []),
    ...(s.when ? [`${inner}when { ${s.when.map(condition).join("; ")} }`] : []),
    ...body,
    ...post(s.post, inner),
    `${pad}}`,
  ];
}

const rendered = new Map<string, Model>();

export function renderModel(model: Model): string {
  const lines = [
    "pipeline {",
    `    agent ${model.agent ? (model.agent.type === "docker" ? `{ docker ${value(model.agent.image)} }` : model.agent.type) : "any"}`,
    ...(model.environment.length
      ? [
          "    environment {",
          ...model.environment.map(
            (e) =>
              `        ${e.name} = ${typeof e.value === "object" && "credentials" in e.value ? `credentials(${quote(e.value.credentials)})` : value(e.value)}`,
          ),
          "    }",
        ]
      : []),
    ...(model.options.length
      ? ["    options {", ...model.options.flatMap((c) => call(c, "        ")), "    }"]
      : []),
    "    stages {",
    ...model.stages.flatMap((s) => stage(s, "        ")),
    "    }",
    ...post(model.post, "    "),
    "}",
  ];
  const text = lines.join("\n") + "\n";
  rendered.set(text, structuredClone(model));
  return text;
}

export function parseText(text: string): { model: Model | null; diagnostics: Diagnostic[] } {
  const model = rendered.get(text);
  const diagnostics = validateText(text);
  if (model) return { model: structuredClone(model), diagnostics };
  if (text.includes("SYNTAX ERROR")) {
    const line = text.split("\n").findIndex((l) => l.includes("SYNTAX ERROR")) + 1;
    return {
      model: null,
      diagnostics: [{ severity: "error", message: "unexpected token", line, column: 1 }],
    };
  }
  return {
    model: null,
    diagnostics: [
      {
        severity: "error",
        message: "mock server can only parse text it rendered",
        line: 1,
        column: 1,
      },
    ],
  };
}

export function validateText(text: string): Diagnostic[] {
  const found: Diagnostic[] = [];
  text.split("\n").forEach((line, index) => {
    const fixme = line.indexOf("FIXME");
    if (fixme >= 0) {
      found.push({
        severity: "error",
        message: "FIXME is not allowed here",
        line: index + 1,
        column: fixme + 1,
      });
    }
    const todo = line.indexOf("TODO");
    if (todo >= 0) {
      found.push({
        severity: "warning",
        message: "TODO left in the pipeline",
        line: index + 1,
        column: todo + 1,
      });
    }
  });
  return found;
}

// ---- stored pipelines ---------------------------------------------------------------------

export interface StoredVersion {
  version: number;
  created_at: string;
  created_by: string | null;
  jenkinsfile: string;
}
export interface StoredPipeline {
  source: "platform" | "repository" | "none";
  versions: StoredVersion[];
}

const echo = (message: string): Call => ({ name: "echo", args: { message } });
const sh = (script: string): Call => ({ name: "sh", args: { script } });

export const STARTER_MODEL: Model = {
  agent: { type: "any" },
  environment: [],
  options: [],
  parameters: [],
  stages: [
    {
      name: "Build",
      agent: null,
      environment: [],
      options: [],
      when: null,
      post: null,
      body: { type: "steps", steps: [echo("Hello")] },
    },
  ],
  post: null,
};

const dockerAgent = { type: "docker", image: "web", args: null } as const;

export const PARALLEL_MODEL: Model = {
  agent: { type: "none" },
  environment: [{ name: "DB_PASSWORD", value: { credentials: "DB_PASSWORD" } }],
  options: [{ name: "timeout", args: { time: 30, unit: "MINUTES" } }],
  parameters: [],
  stages: [
    {
      name: "Build image",
      agent: null,
      environment: [],
      options: [],
      when: null,
      post: null,
      body: {
        type: "steps",
        steps: [{ name: "dockerBuild", args: { image: "web", dockerfile: "Dockerfile" } }],
      },
    },
    {
      name: "Checks",
      agent: dockerAgent,
      environment: [],
      options: [],
      when: null,
      post: null,
      body: {
        type: "parallel",
        stages: [
          {
            name: "Unit tests",
            agent: null,
            environment: [],
            options: [],
            when: null,
            post: null,
            body: { type: "steps", steps: [sh("npm test")] },
          },
          {
            name: "Lint",
            agent: null,
            environment: [],
            options: [],
            when: null,
            post: null,
            body: { type: "steps", steps: [sh("npm run lint")] },
          },
        ],
      },
    },
    {
      name: "Deploy",
      agent: null,
      environment: [],
      options: [],
      when: [{ type: "branch", pattern: "production" }],
      post: { blocks: [{ condition: "failure", steps: [echo("Deploy failed")] }] },
      body: {
        type: "steps",
        steps: [
          { name: "deploy", args: { service: "web", image: "web" } },
          { name: "healthCheck", args: { url: "https://example.test/up", timeout: 90 } },
        ],
      },
    },
  ],
  post: null,
};

const DAY = 86_400_000;
const ago = (ms: number): string => new Date(Date.now() - ms).toISOString();

function seed(): Map<string, StoredPipeline> {
  const version = (n: number, model: Model, days: number, by: string): StoredVersion => ({
    version: n,
    created_at: ago(days * DAY),
    created_by: by,
    jenkinsfile: renderModel(model),
  });
  const withName = (model: Model, name: string): Model => ({
    ...model,
    stages: model.stages.map((s, i) => (i === 0 ? { ...s, name } : s)),
  });
  return new Map<string, StoredPipeline>([
    [
      "shop-api/development",
      {
        source: "platform",
        versions: [
          version(1, STARTER_MODEL, 9, "admin"),
          version(2, withName(PARALLEL_MODEL, "Build"), 4, "admin"),
          version(3, PARALLEL_MODEL, 1, "admin"),
        ],
      },
    ],
    [
      "shop-api/production",
      { source: "platform", versions: [version(1, STARTER_MODEL, 2, "admin")] },
    ],
    [
      "marketing-site/development",
      {
        source: "repository",
        versions: [
          version(1, STARTER_MODEL, 20, "admin"),
          version(2, withName(PARALLEL_MODEL, "Compile"), 12, "admin"),
        ],
      },
    ],
  ]);
}

let store = seed();

export function resetPipelines(): void {
  rendered.clear();
  store = seed();
}

export function getStored(slug: string, env: string): StoredPipeline {
  return store.get(`${slug}/${env}`) ?? { source: "none", versions: [] };
}

export function saveStored(
  slug: string,
  env: string,
  jenkinsfile: string,
  by: string,
): StoredVersion {
  const current = getStored(slug, env);
  const next: StoredVersion = {
    version: (current.versions[current.versions.length - 1]?.version ?? 0) + 1,
    created_at: new Date().toISOString(),
    created_by: by,
    jenkinsfile,
  };
  store.set(`${slug}/${env}`, { source: "platform", versions: [...current.versions, next] });
  return next;
}

/** Another user saving, for conflict tests. */
export function saveAsSomeoneElse(slug: string, env: string, jenkinsfile: string): StoredVersion {
  return saveStored(slug, env, jenkinsfile, "sam");
}
