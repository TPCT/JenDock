import { Button, Label } from "@primer/react";
import { useEnvironmentBackups, useRunBackup } from "../api/backupHooks";
import { Callout, errorMessage } from "../components/Feedback";
import { RelativeTime } from "../components/Time";
import { formatBytes } from "../lib/format";
import { useCan } from "../lib/permissions";
import type { EnvironmentName } from "../api/types";
import box from "../styles/box.module.css";

export function DatabaseBackupActions({
  slug,
  env,
  connection,
}: {
  slug: string;
  env: EnvironmentName;
  connection: string;
}) {
  const backups = useEnvironmentBackups(slug, env);
  const run = useRunBackup(slug, env, connection);
  const allowed = useCan()("backup.run", { project: slug, env });
  const latest = backups.data?.find((backup) => backup.connection === connection);
  return (
    <div>
      <div className={box.cluster}>
        <strong>Latest backup</strong>
        {latest ? (
          <Label
            variant={
              latest.status === "succeeded"
                ? "success"
                : latest.status === "failed"
                  ? "danger"
                  : "attention"
            }
          >
            {latest.status}
          </Label>
        ) : (
          <Label variant="secondary">none yet</Label>
        )}
        {latest ? (
          <span className="muted">
            {latest.started_at ? <RelativeTime iso={latest.started_at} /> : ""}
            {latest.size_bytes !== null ? ` · ${formatBytes(latest.size_bytes)}` : ""}
          </span>
        ) : null}
        {allowed ? (
          <Button
            size="small"
            disabled={run.isPending || latest?.status === "running"}
            onClick={() => {
              run.mutate();
            }}
          >
            {run.isPending ? "Starting…" : "Back up now"}
          </Button>
        ) : null}
      </div>
      {latest?.error ? (
        <Callout variant="danger" role="alert">
          {latest.error}
        </Callout>
      ) : null}
      {run.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(run.error)}
        </Callout>
      ) : null}
      {backups.isError ? (
        <Callout variant="danger" role="alert">
          Backup history could not be loaded.
        </Callout>
      ) : null}
    </div>
  );
}

export function EnvironmentBackupHistory({ slug, env }: { slug: string; env: EnvironmentName }) {
  const backups = useEnvironmentBackups(slug, env);
  return (
    <section aria-label="Backups" className={box.stack}>
      <h2>Backups</h2>
      {backups.isPending ? <p role="status">Loading backup history…</p> : null}
      {backups.isError ? (
        <Callout variant="danger" role="alert">
          Backup history could not be loaded.
        </Callout>
      ) : null}
      {backups.data?.length === 0 ? <p className="muted">No backup runs yet.</p> : null}
      {backups.data && backups.data.length > 0 ? (
        <ul className={`${box.box} ${box.list}`} aria-label="Environment backup runs">
          {backups.data.map((backup) => (
            <li className={box.row} key={backup.id}>
              <div className={box.spread}>
                <strong>{backup.connection}</strong>
                <Label
                  variant={
                    backup.status === "succeeded"
                      ? "success"
                      : backup.status === "failed"
                        ? "danger"
                        : "attention"
                  }
                >
                  {backup.status}
                </Label>
              </div>
              <p className="muted">
                {backup.trigger === "schedule" ? "Scheduled" : "Manual"} · started{" "}
                <RelativeTime iso={backup.started_at} />
                {backup.size_bytes !== null ? ` · ${formatBytes(backup.size_bytes)}` : ""}
              </p>
              {backup.object_key ? <code>{backup.object_key}</code> : null}
              {backup.error ? (
                <Callout variant="danger" role="alert">
                  {backup.error}
                </Callout>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
