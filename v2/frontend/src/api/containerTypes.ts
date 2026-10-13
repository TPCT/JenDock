/** Wire types of the containers, telemetry and terminal endpoints (docs/api-contract.md). */

export type ContainerState =
  "created" | "running" | "paused" | "restarting" | "exited" | "removing" | "dead";
export type ContainerHealth = "starting" | "healthy" | "unhealthy";

export interface ContainerPort {
  container_port: number;
  host_ip: string | null;
  host_port: number | null;
}

export interface ContainerMount {
  type: string;
  source: string;
  destination: string;
  read_only: boolean;
}

export interface ContainerView {
  /** Always the full 64-hex id. */
  id: string;
  name: string;
  service: string;
  image: string;
  image_id: string;
  revision: string | null;
  commit: string | null;
  state: ContainerState;
  health: ContainerHealth | null;
  restart_count: number;
  started_at: string | null;
  exit_code: number | null;
  cpus: number | null;
  memory_bytes: number | null;
  ports: ContainerPort[];
  mounts: ContainerMount[];
}

export type LifecycleAction = "start" | "stop" | "restart";

/** Step one of a lifecycle action: nothing has changed yet. */
export interface ActionPlan {
  action: LifecycleAction;
  container: ContainerView;
  effects: string[];
  confirmation: string;
  expires_at: string;
}

export interface ActionResult {
  action: LifecycleAction;
  previous_state: ContainerState;
  container: ContainerView;
}

export interface LogLineView {
  timestamp: string;
  text: string;
}

export interface LogPage {
  lines: LogLineView[];
  cursor: { since: string; skip: number } | null;
  dropped: number;
}

export interface EnvVarView {
  name: string;
  sensitive: boolean;
  overridden: boolean;
  masked_value: string;
}

export interface EnvUpdateResult {
  service: string;
  restarted: boolean;
  containers: ContainerView[];
}

export interface DirectoryEntry {
  name: string;
  kind: "directory" | "file";
}

export interface DirectoryView {
  path: string;
  entries: DirectoryEntry[];
  truncated: boolean;
}

export interface FileView {
  path: string;
  size: number;
  truncated: boolean;
  binary: boolean;
  text: string | null;
}

export interface StatsSample {
  timestamp: string;
  cpu_percent: number;
  memory_used: number;
  memory_limit: number;
  memory_percent: number;
  net_rx: number;
  net_tx: number;
  block_read: number;
  block_write: number;
  pids: number;
}

export interface ContainerStats {
  latest: StatsSample;
  history: StatsSample[];
}

export interface HostSample {
  timestamp: string;
  cpu_percent: number | null;
  cpu_count: number;
  load_1: number;
  load_5: number;
  load_15: number;
  memory_total: number;
  memory_used: number;
  memory_available: number;
  swap_total: number;
  swap_free: number;
  disk_total: number;
  disk_used: number;
  disk_free: number;
}

export interface HostTelemetry {
  latest: HostSample;
  history: HostSample[];
}

export interface TerminalTicket {
  ticket: string;
  expires_at: string;
  websocket_path: string;
  container_id: string;
}

/** Text frames the terminal server sends. Binary frames are terminal output. */
export type TerminalServerMessage =
  | { type: "ready"; terminal_id: string }
  | { type: "exit"; code: number | null; reason: TerminalExitReason }
  | { type: "error"; message: string }
  | { type: "pong" };

export type TerminalExitReason =
  "exited" | "closed" | "idle_timeout" | "absolute_timeout" | "shutdown";
