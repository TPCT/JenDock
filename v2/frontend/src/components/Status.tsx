import {
  AlertIcon,
  CheckCircleFillIcon,
  ClockIcon,
  SyncIcon,
  XCircleFillIcon,
  StopIcon,
  SkipIcon,
} from "@primer/octicons-react";
import type { RunStatus } from "../api/types";
import { STATUS_LABEL } from "../lib/format";

const ICONS = {
  succeeded: CheckCircleFillIcon,
  failed: XCircleFillIcon,
  running: SyncIcon,
  queued: ClockIcon,
  cancelled: StopIcon,
  interrupted: AlertIcon,
  skipped: SkipIcon,
} as const;

/** Icon only; pair it with text or give it a label. Colour is never the only signal. */
export function StatusIcon({ status, size = 16 }: { status: RunStatus; size?: number }) {
  const Icon = ICONS[status];
  return (
    <span className={`status status-${status}`}>
      <Icon size={size} className={status === "running" ? "spin" : undefined} aria-hidden="true" />
    </span>
  );
}

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`status status-${status}`}>
      <StatusIcon status={status} />
      <span className="status-label">{STATUS_LABEL[status]}</span>
    </span>
  );
}
