import { DatabaseIcon } from "@primer/octicons-react";
import { Label } from "@primer/react";
import { useBackups } from "../api/backupHooks";
import { Callout, EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import { RelativeTime } from "../components/Time";
import { formatBytes } from "../lib/format";
import box from "../styles/box.module.css";

export function BackupsPage() {
  const backups = useBackups();
  if (backups.isPending) return <Loading label="Loading database backups" />;
  return (
    <main className={box.stack}>
      <PageHead
        title="Database backups"
        description="Recent database backup attempts across the projects you can access."
      />
      {backups.isError ? (
        <ErrorCallout
          title="Could not load backups"
          error={backups.error}
          onRetry={() => void backups.refetch()}
        />
      ) : null}
      {backups.data?.length === 0 ? (
        <EmptyState icon={<DatabaseIcon size={32} />} title="No backup runs yet">
          A backup appears here after the first scheduled or manual run.
        </EmptyState>
      ) : null}
      {backups.data?.length ? (
        <ul className={`${box.box} ${box.list}`} aria-label="Database backup runs">
          {backups.data.map((backup) => (
            <li key={backup.id} className={box.row}>
              <div className={box.spread}>
                <strong>
                  {backup.project}/{backup.environment}/{backup.connection}
                </strong>
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
    </main>
  );
}
