/**
 * Wire types of the pipeline endpoints (see docs/api-contract.md, "Pipelines").
 * The browser never parses Jenkinsfile text: `Model` is produced and consumed by the server.
 */
import type { Diagnostic } from "./types";

export type PipelineSource = "platform" | "repository" | "none";

export interface PipelineDocument {
  source: PipelineSource;
  /** 0 when nothing is stored yet. */
  version: number;
  jenkinsfile: string;
  updated_at: string | null;
  updated_by: string | null;
}

export interface PipelineVersionInfo {
  version: number;
  created_at: string;
  created_by: string | null;
}

export interface PipelineVersion extends PipelineVersionInfo {
  jenkinsfile: string;
}

// ---- the editing model ---------------------------------------------------------------------

/** Literal text (rendered single-quoted) or a template whose `${NAME}` parts are variables. */
export interface TemplateString {
  template: string;
}
export interface MapValue {
  map: { key: string; value: Value }[];
}
export type Value = string | number | boolean | TemplateString | MapValue | Value[];
export type StringValue = string | TemplateString;

export interface Credentials {
  credentials: string;
}

export interface EnvVar {
  name: string;
  value: StringValue | Credentials;
}

export interface Call {
  name: string;
  args: Record<string, Value>;
  /** Present only for steps that take a block (timeout, retry, dir, withEnv). */
  body?: Call[];
}

export type Agent =
  | { type: "any" }
  | { type: "none" }
  | { type: "docker"; image: StringValue; args: StringValue | null };

export type Condition =
  | { type: "branch"; pattern: StringValue }
  | { type: "environment"; name: string; value: StringValue }
  | { type: "not"; condition: Condition }
  | { type: "allOf"; conditions: Condition[] }
  | { type: "anyOf"; conditions: Condition[] };

export type PostCondition =
  "always" | "changed" | "aborted" | "failure" | "success" | "unstable" | "cleanup";

export interface PostBlock {
  condition: PostCondition;
  steps: Call[];
}
export interface Post {
  blocks: PostBlock[];
}

export type StageBody =
  | { type: "steps"; steps: Call[] }
  | { type: "parallel"; stages: Stage[] }
  | { type: "stages"; stages: Stage[] };

export interface Stage {
  name: string;
  agent: Agent | null;
  environment: EnvVar[];
  options: Call[];
  /** Top-level conditions, all of which must hold; `null` = no `when`. */
  when: Condition[] | null;
  body: StageBody;
  post: Post | null;
}

export interface Model {
  agent: Agent | null;
  environment: EnvVar[];
  options: Call[];
  parameters: Call[];
  stages: Stage[];
  post: Post | null;
}

// ---- catalogue -----------------------------------------------------------------------------

export type ArgType = "string" | "int" | "bool" | "list" | "map";

export interface ArgDef {
  name: string;
  type: ArgType;
  required: boolean;
  default: string | number | boolean | null;
  /** Allowed literal values (rendered as a select). */
  enum: string[] | null;
  min: number | null;
  max: number | null;
  description: string;
  multiline: boolean;
  /** `produces_image`: names an image this step builds. `uses_image`: refers to a built image. */
  role: "produces_image" | "uses_image" | null;
}

export interface StepDef {
  name: string;
  title: string;
  description: string;
  takes_block: boolean;
  needs_container: boolean;
  args: ArgDef[];
}

export interface Catalog {
  steps: StepDef[];
  options: StepDef[];
  parameters: StepDef[];
  post_conditions: { name: PostCondition; description: string }[];
}

export interface Diagnostics {
  diagnostics: Diagnostic[];
}
export interface ParseResult extends Diagnostics {
  /** `null` when the text has syntax errors and cannot be turned into a model. */
  model: Model | null;
}
export interface RenderResult extends Diagnostics {
  jenkinsfile: string | null;
}

export interface SecretName {
  name: string;
  created_at: string;
  updated_at: string;
}
