import { ProgressBar } from "@primer/react";
import { Button } from "@primer/react";
import { useState } from "react";
import { useApplyResources, useResourceUsage, useResources } from "../api/resourceHooks";
import type {
  ContainerUsage,
  HostCapacity,
  ResourceChange,
  ServiceResources,
} from "../api/resourceTypes";
import type { EnvironmentName } from "../api/types";
import { formatBytes } from "../lib/format";
import { useCan } from "../lib/permissions";
import { describeLimits } from "../lib/resources";
import box from "../styles/box.module.css";
import { Callout, ErrorCallout, Loading, errorMessage } from "./Feedback";
import { ResourceLimitsDialog } from "./ResourceLimitsDialog";
import styles from "./Resources.module.css";

const tone = (percent: number) =>
  percent > 90 ? "danger.emphasis" : percent > 75 ? "attention.emphasis" : "accent.emphasis";

function Meter({ label, text, percent }: { label: string; text: string; percent: number }) {
  const clamped = Math.max(0, Math.min(100, Math.round(percent)));
  return (
    <div className={styles.meter}>
      <div className={styles.meterLabel}>
        <span>{label}</span>
        <span className="muted">{text}</span>
      </div>
      <ProgressBar
        aria-label={`${label} used`}
        progress={clamped}
        barSize="small"
        bg={tone(clamped)}
      />
    </div>
  );
}

/** CPU and memory of one running container against its limits (the host's capacity if none). */
function UsageMeters({ usage, host }: { usage: ContainerUsage; host: HostCapacity }) {
  const cores = usage.cpu_limit ?? host.cpus;
  const used = usage.cpu_percent / 100;
  const memoryTotal = usage.memory_limit ?? host.memory_bytes;
  return (
    <div className={styles.meters} role="group" aria-label={`Usage of ${usage.name}`}>
      <Meter
        label="CPU"
        percent={(used / cores) * 100}
        text={`${used.toFixed(2)} of ${String(cores)} CPU${cores === 1 ? "" : "s"}${usage.cpu_limit === null ? " (host)" : ""}`}
      />
      <Meter
        label="Memory"
        percent={(usage.memory_used / memoryTotal) * 100}
        text={`${formatBytes(usage.memory_used)} of ${formatBytes(memoryTotal)}${usage.memory_limit === null ? " (host)" : ""}`}
      />
    </div>
  );
}

/** Whether a running container still has other limits than the saved ones. */
function isBehind(entry: ServiceResources, usage: ContainerUsage[]): boolean {
  const saved = entry.limits;
  if (saved === null) return false;
  return usage.some(
    (item) =>
      (saved.cpus !== null && item.cpu_limit !== saved.cpus) ||
      (saved.memory_bytes !== null && item.memory_limit !== saved.memory_bytes),
  );
}

function summary(change: ResourceChange, removed: boolean): { text: string; ok: boolean } {
  const failed = change.applied.filter((item) => !item.ok);
  const done = change.applied.length - failed.length;
  const parts = [
    removed ? "Limit removed. Future deploys run without it." : "Limits saved for future deploys.",
  ];
  if (done > 0) parts.push(`Applied to ${String(done)} running container${done === 1 ? "" : "s"}.`);
  for (const item of failed) {
    parts.push(`Could not update ${item.name}: ${item.error ?? "unknown error"}.`);
  }
  parts.push(...change.notes);
  return { text: parts.join(" "), ok: failed.length === 0 };
}

function ServiceRow({
  slug,
  env,
  entry,
  host,
  usage,
  canEdit,
  onDone,
}: {
  slug: string;
  env: EnvironmentName;
  entry: ServiceResources;
  host: HostCapacity;
  usage: ContainerUsage[];
  canEdit: boolean;
  onDone: (change: ResourceChange, removed: boolean) => void;
}) {
  const [editing, setEditing] = useState(false);
  const apply = useApplyResources(slug, env, entry.service);
  const behind = isBehind(entry, usage);
  return (
    <div
      className={`${box.row} ${styles.service}`}
      role="group"
      aria-label={`${entry.service} resources`}
    >
      <div className={box.spread}>
        <div className={styles.limits}>
          <strong>{entry.service}</strong>{" "}
          <span className="muted">
            {entry.limits ? describeLimits(entry.limits) : "No limit set"}
          </span>
        </div>
        {canEdit ? (
          <div className={box.cluster}>
            {behind ? (
              <Button
                size="small"
                disabled={apply.isPending}
                onClick={() => {
                  apply.mutate(undefined, {
                    onSuccess: (change) => {
                      onDone(change, false);
                    },
                  });
                }}
              >
                {apply.isPending ? "Applying…" : "Apply now"}
              </Button>
            ) : null}
            <Button
              size="small"
              onClick={() => {
                setEditing(true);
              }}
            >
              Edit limits
            </Button>
          </div>
        ) : null}
      </div>
      {usage.length === 0 ? (
        <p className="muted">No running container, so there is no usage to show.</p>
      ) : (
        usage.map((item) => <UsageMeters key={item.id} usage={item} host={host} />)
      )}
      {behind ? (
        <p className={`muted ${styles.note}`}>
          The saved limits are not applied to the running containers yet. They apply at the next
          deploy
          {canEdit ? ", or now with Apply now" : ""}.
        </p>
      ) : null}
      {apply.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(apply.error)}
        </Callout>
      ) : null}
      {editing ? (
        <ResourceLimitsDialog
          slug={slug}
          env={env}
          service={entry.service}
          host={host}
          current={entry.limits}
          hasRunning={usage.length > 0}
          onClose={() => {
            setEditing(false);
          }}
          onDone={(change, removed) => {
            setEditing(false);
            onDone(change, removed);
          }}
        />
      ) : null}
    </div>
  );
}

/**
 * CPU and memory use against the limits, with the controls to change them. Pass `service` to show
 * one service (the container page), nothing to show every service of the environment.
 */
export function ResourcesSection({
  slug,
  env,
  service,
}: {
  slug: string;
  env: EnvironmentName;
  service?: string | undefined;
}) {
  const resources = useResources(slug, env);
  const usage = useResourceUsage(slug, env);
  const can = useCan();
  const [notice, setNotice] = useState<{ text: string; ok: boolean } | null>(null);

  const entries = (resources.data?.services ?? []).filter(
    (item) => service === undefined || item.service === service,
  );
  return (
    <section className={box.box} aria-label="Resources">
      <div className={box.header}>
        <span>Resources</span>
        <span className={`muted ${styles.note}`}>
          Limits are saved for every future deploy; Apply now updates running containers.
        </span>
      </div>
      {resources.isPending ? (
        <div className={box.row}>
          <Loading label="Loading resources" />
        </div>
      ) : null}
      {resources.isError ? (
        <div className={box.row}>
          <ErrorCallout
            title="Could not load the resource limits"
            error={resources.error}
            onRetry={() => void resources.refetch()}
          />
        </div>
      ) : null}
      {notice ? (
        <div className={box.row}>
          <Callout variant={notice.ok ? "success" : "warning"} role="status">
            {notice.text}
          </Callout>
        </div>
      ) : null}
      {resources.data && entries.length === 0 ? (
        <p className={`${box.row} muted`}>No services to show.</p>
      ) : null}
      {resources.data
        ? entries.map((entry) => (
            <ServiceRow
              key={entry.service}
              slug={slug}
              env={env}
              entry={entry}
              host={resources.data.host}
              usage={(usage.data?.containers ?? []).filter(
                (item) => item.service === entry.service,
              )}
              canEdit={can("container.limits", { project: slug, env })}
              onDone={(change, removed) => {
                setNotice(summary(change, removed));
              }}
            />
          ))
        : null}
      {usage.isError && resources.data ? (
        <p className={`${box.row} muted`}>Live usage is not available right now.</p>
      ) : null}
    </section>
  );
}
