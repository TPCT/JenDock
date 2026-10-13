import { useContainerStats } from "../../api/containerHooks";
import type { ContainerView } from "../../api/containerTypes";
import type { EnvironmentName } from "../../api/types";
import {
  HealthLabel,
  StateLabel,
  Uptime,
  portText,
  shortImageId,
} from "../../components/ContainerBits";
import { CopyField } from "../../components/CopyField";
import { ResourcesSection } from "../../components/Resources";
import { Sparkline } from "../../components/Sparkline";
import { RelativeTime } from "../../components/Time";
import { formatBytes } from "../../lib/format";
import box from "../../styles/box.module.css";

export function OverviewPanel({
  slug,
  env,
  container,
}: {
  slug: string;
  env: EnvironmentName;
  container: ContainerView;
}) {
  const running = container.state === "running";
  const stats = useContainerStats(slug, env, container.id, running);
  const history = stats.data?.history ?? [];
  const latest = stats.data?.latest;
  return (
    <div className={box.stack}>
      <ResourcesSection slug={slug} env={env} service={container.service} />
      <section className={box.box} aria-label="Resource use">
        <div className={box.header}>Resource use</div>
        <div className={box.row}>
          {!running ? (
            <p className="muted">Statistics are only recorded while the container is running.</p>
          ) : stats.isPending ? (
            <p className="muted">Loading statistics…</p>
          ) : latest ? (
            <dl className={box.defs}>
              <dt>CPU</dt>
              <dd>
                <Sparkline
                  label="CPU"
                  unit="%"
                  max={100}
                  values={history.map((s) => s.cpu_percent)}
                />{" "}
                {latest.cpu_percent.toFixed(1)}%
              </dd>
              <dt>Memory</dt>
              <dd>
                <Sparkline
                  label="Memory"
                  unit="%"
                  max={100}
                  values={history.map((s) => s.memory_percent)}
                />{" "}
                {formatBytes(latest.memory_used)} of {formatBytes(latest.memory_limit)} (
                {latest.memory_percent.toFixed(0)}%)
              </dd>
              <dt>Network</dt>
              <dd>
                {formatBytes(latest.net_rx)} received, {formatBytes(latest.net_tx)} sent
              </dd>
              <dt>Processes</dt>
              <dd>{latest.pids}</dd>
            </dl>
          ) : (
            <p className="muted">Statistics are not available yet.</p>
          )}
          <p className="muted">
            Sampled every 15 seconds; the chart shows the most recent samples.
          </p>
        </div>
      </section>

      <section className={box.box} aria-label="Details">
        <div className={box.header}>Details</div>
        <div className={box.row}>
          <dl className={box.defs}>
            <dt>State</dt>
            <dd>
              <StateLabel state={container.state} /> <HealthLabel health={container.health} />
            </dd>
            <dt>Service</dt>
            <dd>{container.service}</dd>
            <dt>Image</dt>
            <dd>
              <code>{container.image}</code> (<code>{shortImageId(container.image_id)}</code>)
            </dd>
            <dt>Commit</dt>
            <dd>{container.commit ? <code>{container.commit}</code> : "unknown"}</dd>
            <dt>Restarts</dt>
            <dd>{container.restart_count}</dd>
            <dt>Started</dt>
            <dd>
              {container.started_at ? (
                <>
                  <RelativeTime iso={container.started_at} /> (up{" "}
                  <Uptime startedAt={container.started_at} state={container.state} />)
                </>
              ) : (
                "not running"
              )}
            </dd>
            {container.exit_code !== null ? (
              <>
                <dt>Exit code</dt>
                <dd>{container.exit_code}</dd>
              </>
            ) : null}
            <dt>Limits</dt>
            <dd>
              {container.cpus ?? "no"} CPUs,{" "}
              {container.memory_bytes ? formatBytes(container.memory_bytes) : "no"} memory
            </dd>
            <dt>Ports</dt>
            <dd>
              {container.ports.length === 0
                ? "none"
                : container.ports.map((port) => (
                    <div key={portText(port)}>
                      <code>{portText(port)}</code>
                    </div>
                  ))}
            </dd>
            <dt>Mounts</dt>
            <dd>
              {container.mounts.length === 0
                ? "none"
                : container.mounts.map((mount) => (
                    <div key={mount.destination}>
                      <code>
                        {mount.source} → {mount.destination}
                      </code>{" "}
                      <span className="muted">
                        ({mount.type}, {mount.read_only ? "read only" : "read and write"})
                      </span>
                    </div>
                  ))}
            </dd>
          </dl>
        </div>
        <div className={box.row}>
          <CopyField label="Container ID" value={container.id} />
        </div>
      </section>
    </div>
  );
}
