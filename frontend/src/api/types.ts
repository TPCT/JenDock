/** Wire types. Keep in sync with docs/api-contract.md. Timestamps are ISO-8601 UTC strings. */

export type EnvironmentName = "development" | "production";
export const ENVIRONMENTS: readonly EnvironmentName[] = ["development", "production"];

export type RunStatus =
  "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted" | "skipped";
export type RunTrigger = "push" | "manual" | "redeploy";
export type PipelineSource = "repository" | "platform";

/** Platform-wide role: owners and admins can do everything (only owners manage owners). */
export type GlobalRole = "owner" | "admin" | "member";
/** What a grant on a project or environment allows (cumulative, like GitHub repository roles). */
export type AccessRole = "viewer" | "developer" | "maintainer";
/** What a grant on one container service allows. */
export type ContainerPermission = "logs" | "terminal" | "actions";

export interface User {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  role: GlobalRole;
}

/** What the current user may do in one environment (and in its granted services). */
export interface EnvironmentPermissions {
  role: AccessRole | null;
  /** Action names such as `run.create`; see docs/api-contract.md. */
  actions: string[];
  services: Record<string, string[]>;
}

/**
 * Effective permissions. Owners and admins have `all: true`; for members `projects` lists what
 * their grants give. The API stays the authority: this only decides what the UI shows.
 */
export interface Permissions {
  role: GlobalRole;
  all: boolean;
  global: string[];
  projects: Record<
    string,
    { environments: Partial<Record<EnvironmentName, EnvironmentPermissions>> }
  >;
}

export interface Session {
  user: User;
  csrf_token: string;
  expires_at: string;
  /** After an administrator reset the password: the user must change it before anything else. */
  must_change_password: boolean;
  permissions: Permissions;
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
  auto_stop_hours?: number | null;
  include_in_backups: boolean;
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

/** The envelope of every list endpoint (see "Lists" in docs/api-contract.md). */
export interface Page<T> {
  items: T[];
  total: number;
  /** 1-based. */
  page: number;
  per_page: number;
  /** At least 1, even for an empty list. */
  pages: number;
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
