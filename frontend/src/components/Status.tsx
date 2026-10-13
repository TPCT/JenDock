import {
  AlertIcon,
  CheckCircleFillIcon,
  DotFillIcon,
  SkipIcon,
  StopIcon,
  SyncIcon,
  XCircleFillIcon,
} from "@primer/octicons-react";
import { Label } from "@primer/react";
import type { ComponentProps } from "react";
import type { RunStatus } from "../api/types";
import { STATUS_LABEL } from "../lib/format";
import styles from "./Status.module.css";

/** The same icons GitHub Actions uses for check state. */
const ICONS = {
  succeeded: CheckCircleFillIcon,
  failed: XCircleFillIcon,
  running: SyncIcon,
  queued: DotFillIcon,
  cancelled: StopIcon,
  interrupted: AlertIcon,
  skipped: SkipIcon,
} as const;

type LabelVariant = NonNullable<ComponentProps<typeof Label>["variant"]>;

const LABEL_VARIANT: Record<RunStatus, LabelVariant> = {
  succeeded: "success",
  failed: "danger",
  running: "attention",
  queued: "attention",
  cancelled: "secondary",
  interrupted: "severe",
  skipped: "secondary",
};

/** Icon only; pair it with text or give it a label. Colour is never the only signal. */
export function StatusIcon({ status, size = 16 }: { status: RunStatus; size?: number }) {
  const Icon = ICONS[status];
  const className = [styles[status], status === "running" ? "spin" : ""].filter(Boolean).join(" ");
  return <Icon size={size} className={className} aria-hidden="true" />;
}

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <Label variant={LABEL_VARIANT[status]} className={styles.badge}>
      <StatusIcon status={status} size={12} />
      {STATUS_LABEL[status]}
    </Label>
  );
}
