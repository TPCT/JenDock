import { ProgressBar } from "@primer/react";
import { useHostTelemetry } from "../api/containerHooks";
import { formatBytes } from "../lib/format";
import box from "../styles/box.module.css";
import styles from "./HostCard.module.css";
import { Sparkline } from "./Sparkline";

function Meter({ label, used, total }: { label: string; used: number; total: number }) {
  const percent = total > 0 ? Math.round((used / total) * 100) : 0;
  return (
    <div className={styles.meter}>
      <div className={box.spread}>
        <span>{label}</span>
        <span className="muted">
          {formatBytes(used)} of {formatBytes(total)} ({percent}%)
        </span>
      </div>
      <ProgressBar
        aria-label={`${label} used`}
        progress={percent}
        barSize="small"
        bg={
          percent > 90 ? "danger.emphasis" : percent > 75 ? "attention.emphasis" : "accent.emphasis"
        }
      />
    </div>
  );
}

/** Host CPU, memory and disk from the worker's telemetry ring buffer. */
export function HostCard() {
  const host = useHostTelemetry();
  if (host.isPending) return null;
  if (host.isError) {
    return (
      <div className={box.box}>
        <div className={box.header}>Host</div>
        <p className={`${box.row} muted`}>Host telemetry is not available right now.</p>
      </div>
    );
  }
  const { latest, history } = host.data;
  return (
    <section className={box.box} aria-label="Host telemetry">
      <div className={box.header}>
        <span>Host</span>
        <span className={`muted ${styles.note}`}>
          As the build worker sees it; inside a container these are the host kernel&apos;s values.
        </span>
      </div>
      <div className={`${box.row} ${styles.grid}`}>
        <div className={styles.meter}>
          <div className={box.spread}>
            <span>CPU ({latest.cpu_count} cores)</span>
            <span className="muted">
              {latest.cpu_percent === null ? "measuring…" : `${latest.cpu_percent.toFixed(0)}%`}
            </span>
          </div>
          <Sparkline
            label="Host CPU"
            unit="%"
            max={100}
            values={history.flatMap((sample) =>
              sample.cpu_percent === null ? [] : [sample.cpu_percent],
            )}
          />
          <span className="muted">
            Load {latest.load_1.toFixed(2)} / {latest.load_5.toFixed(2)} /{" "}
            {latest.load_15.toFixed(2)}
          </span>
        </div>
        <Meter label="Memory" used={latest.memory_used} total={latest.memory_total} />
        <Meter label="Disk" used={latest.disk_used} total={latest.disk_total} />
      </div>
    </section>
  );
}
