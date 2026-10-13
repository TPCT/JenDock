import { Label } from "@primer/react";
import type { ContainerHealth, ContainerPort, ContainerState } from "../api/containerTypes";
import { formatDuration } from "../lib/format";
import { useNow } from "../lib/useNow";

type Variant = "default" | "secondary" | "success" | "attention" | "severe" | "danger";

const STATE: Record<ContainerState, Variant> = {
  running: "success",
  created: "secondary",
  exited: "secondary",
  paused: "attention",
  restarting: "attention",
  removing: "attention",
  dead: "danger",
};

const HEALTH: Record<ContainerHealth, Variant> = {
  healthy: "success",
  starting: "attention",
  unhealthy: "danger",
};

export function StateLabel({ state }: { state: ContainerState }) {
  return <Label variant={STATE[state]}>{state}</Label>;
}

export function HealthLabel({ health }: { health: ContainerHealth | null }) {
  return health === null ? (
    <span className="muted">no health check</span>
  ) : (
    <Label variant={HEALTH[health]}>{health}</Label>
  );
}

/** "3h 12m" since the container started; blank unless it is running. */
export function Uptime({ startedAt, state }: { startedAt: string | null; state: ContainerState }) {
  const running = state === "running" && startedAt !== null;
  const now = useNow(running, 30_000);
  if (!running) return <span className="muted">-</span>;
  return <span>{formatDuration(now - Date.parse(startedAt))}</span>;
}

export function shortImageId(imageId: string): string {
  return imageId.replace(/^sha256:/, "").slice(0, 12);
}

export function portText(port: ContainerPort): string {
  return port.host_port === null
    ? `${String(port.container_port)} (not published)`
    : `${port.host_ip ?? "0.0.0.0"}:${String(port.host_port)} → ${String(port.container_port)}`;
}
