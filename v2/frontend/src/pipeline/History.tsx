import { HistoryIcon } from "@primer/octicons-react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchPipelineVersion, usePipelineVersions } from "../api/hooks";
import type { EnvironmentName } from "../api/types";
import { Dialog } from "../components/Dialog";
import { ErrorCallout, Loading } from "../components/Feedback";
import { RelativeTime } from "../components/Time";

function VersionDialog({
  slug,
  env,
  version,
  canRestore,
  onRestore,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  version: number;
  canRestore: boolean;
  onRestore: (text: string) => void;
  onClose: () => void;
}) {
  const query = useQuery({
    queryKey: ["pipeline", slug, env, "version", version],
    queryFn: () => fetchPipelineVersion(slug, env, version),
  });
  return (
    <Dialog title={`Version ${version}`} onClose={onClose}>
      {query.isPending ? <Loading label="Loading version" /> : null}
      {query.isError ? (
        <ErrorCallout title="Could not load this version" error={query.error} />
      ) : null}
      {query.data ? (
        <pre className="version-text" tabIndex={0} aria-label={`Jenkinsfile of version ${version}`}>
          {query.data.jenkinsfile}
        </pre>
      ) : null}
      <div className="dialog-actions">
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
        {canRestore && query.data ? (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              onRestore(query.data.jenkinsfile);
              onClose();
            }}
          >
            Restore into the editor
          </button>
        ) : null}
      </div>
    </Dialog>
  );
}

/** Saved versions of the stored pipeline; stays available in read-only mode. */
export function History({
  slug,
  env,
  canRestore,
  onRestore,
}: {
  slug: string;
  env: EnvironmentName;
  canRestore: boolean;
  onRestore: (text: string) => void;
}) {
  const versions = usePipelineVersions(slug, env);
  const [open, setOpen] = useState<number | null>(null);
  return (
    <section className="panel" aria-labelledby="pipeline-history">
      <h3 id="pipeline-history">
        <HistoryIcon size={16} aria-hidden="true" /> History
      </h3>
      {versions.isPending ? <Loading label="Loading history" /> : null}
      {versions.isError ? (
        <ErrorCallout title="Could not load the history" error={versions.error} />
      ) : null}
      {versions.data?.length === 0 ? <p className="muted">Nothing has been saved yet.</p> : null}
      {versions.data && versions.data.length > 0 ? (
        <ul className="history">
          {versions.data.map((item) => (
            <li key={item.version}>
              <span>
                <strong>Version {item.version}</strong>{" "}
                <span className="muted">
                  <RelativeTime iso={item.created_at} />
                  {item.created_by ? ` by ${item.created_by}` : ""}
                </span>
              </span>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setOpen(item.version);
                }}
                aria-label={`View version ${item.version}`}
              >
                View
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {open !== null ? (
        <VersionDialog
          slug={slug}
          env={env}
          version={open}
          canRestore={canRestore}
          onRestore={onRestore}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
    </section>
  );
}
