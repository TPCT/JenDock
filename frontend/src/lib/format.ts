import type { RunStatus } from "../api/types";

export const TERMINAL_STATUSES: readonly RunStatus[] = [
  "succeeded",
  "failed",
  "cancelled",
  "interrupted",
  "skipped",
];

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export function isCancellable(status: RunStatus): boolean {
  return status === "queued" || status === "running";
}

export const STATUS_LABEL: Record<RunStatus, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
  skipped: "Skipped",
};

/** 83_000 -> "1m 23s"; sub-second values read "<1s". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "-";
  const total = Math.floor(ms / 1000);
  if (total < 1) return "<1s";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

/** Duration of something that may still run; `now` is epoch milliseconds. */
export function durationBetween(
  startedAt: string | null,
  finishedAt: string | null,
  now: number,
): string | null {
  if (startedAt === null) return null;
  const start = Date.parse(startedAt);
  const end = finishedAt === null ? now : Date.parse(finishedAt);
  return formatDuration(end - start);
}

const DATE_TIME = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });
const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export function formatDateTime(iso: string): string {
  return DATE_TIME.format(new Date(iso));
}

export function formatRelative(iso: string, now: number): string {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  if (abs < 3600) return RELATIVE.format(Math.round(seconds / 60), "minute");
  if (abs < 86_400) return RELATIVE.format(Math.round(seconds / 3600), "hour");
  if (abs < 86_400 * 30) return RELATIVE.format(Math.round(seconds / 86_400), "day");
  if (abs < 86_400 * 365) return RELATIVE.format(Math.round(seconds / (86_400 * 30)), "month");
  return RELATIVE.format(Math.round(seconds / (86_400 * 365)), "year");
}

export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** 1536 -> "1.5 KiB". */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? String(Math.round(value)) : value.toFixed(1)} ${units[unit] ?? "B"}`;
}
