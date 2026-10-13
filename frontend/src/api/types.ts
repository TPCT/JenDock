/** Wire types. Keep in sync with docs/api-contract.md. Timestamps are ISO-8601 UTC strings. */

export type EnvironmentName = "development" | "production";
export const ENVIRONMENTS: readonly EnvironmentName[] = ["development", "production"];

export type RunStatus =
  "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted" | "skipped";
export type RunTrigger = "push" | "manual" | "redeploy";
export type PipelineSource = "repository" | "platform";

export interface User {
  id: number;
  username: string;
}

export interface Session {
  user: User;
  csrf_token: string;
  expires_at: string;
}

export interface RunSummary {
  id: number;
  number: number;
  /** Project slug. */
  project: string;
  environment: EnvironmentName;
  trigger: RunTrigger;
  branch: string;
  commit_sha: string;
  pipeline_source: PipelineSource;
  status: RunStatus;
  /** Why the run ended the way it did (cancelled while queued, failure reason...). */
  message: string | null;
  cancel_requested_at: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface EnvironmentSummary {
  name: EnvironmentName;
  branch: string;
  auto_deploy: boolean;
  /** Docker Compose settings the deployer needs; empty until configured. */
  compose_project: string | null;
  compose_files: string[];
  compose_env_files: string[];
  services: string[];
  /** Whether a pipeline is stored (a repository Jenkinsfile alone does not count). */
  has_pipeline: boolean;
  last_run: RunSummary | null;
}

export interface Project {
  slug: string;
  name: string;
  repository: string;
  created_at: string;
  environments: EnvironmentSummary[];
  /** How the worker clones: `ssh` = deploy key, `https` = public repositories. */
  git_transport: "ssh" | "https";
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface StepRun {
  id: number;
  position: number;
  step_type: string;
  label: string;
  status: RunStatus;
  exit_code: number | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface StageRun {
  id: number;
  position: number;
  /** Proposed backend field. Stages sharing a column run in parallel; absent = sequential. */
  column?: number | null;
  name: string;
  status: RunStatus;
  started_at: string | null;
  finished_at: string | null;
  steps: StepRun[];
}

export interface RunDetail extends RunSummary {
  stages: StageRun[];
}

export interface LogChunk {
  offset: number;
  next_offset: number;
  size: number;
  text: string;
  complete: boolean;
}

export interface BranchHead {
  branch: string;
  commit_sha: string;
  message: string;
  author: string;
  committed_at: string;
}

/** Events sent on `GET /api/runs/{id}/events` (payloads are JSON). */
export interface RunEventMap {
  /** Full run state; sent first and again on every change. */
  status: RunDetail;
  /** New log text of one step; `offset` is the byte offset where `text` starts. */
  log: { step_id: number; offset: number; text: string };
  /** Last event; the server then closes the stream. */
  end: { status: RunStatus };
}

export interface CancelResult {
  outcome: "cancelled" | "requested";
  run: RunDetail;
}

export interface Diagnostic {
  severity: "error" | "warning";
  message: string;
  line: number;
  column: number;
}

export interface FieldIssue {
  loc: string[];
  msg: string;
}
