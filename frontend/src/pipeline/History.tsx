import { GitCommitIcon, HistoryIcon } from "@primer/octicons-react";
import { Button, Dialog, Heading, Timeline } from "@primer/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchPipelineVersion, usePipelineVersions } from "../api/hooks";
import type { EnvironmentName } from "../api/types";
import { ErrorCallout, Loading } from "../components/Feedback";
import { RelativeTime } from "../components/Time";
import styles from "./pipeline.module.css";

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
  const data = query.data;
  return (
    <Dialog
      title={`Version ${version}`}
      width="xlarge"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Close", onClick: onClose },
        ...(canRestore && data
          ? [
              {
                buttonType: "primary" as const,
                content: "Restore into the editor",
                onClick: () => {
                  onRestore(data.jenkinsfile);
                  onClose();
                },
              },
            ]
          : []),
      ]}
    >
      {query.isPending ? <Loading label="Loading version" /> : null}
      {query.isError ? (
        <ErrorCallout title="Could not load this version" error={query.error} />
      ) : null}
      {data ? (
        <pre
          className={styles.versionText}
          tabIndex={0}
          aria-label={`Jenkinsfile of version ${version}`}
        >
          {data.jenkinsfile}
        </pre>
      ) : null}
    </Dialog>
  );
}

/** Saved versions of the stored pipeline as a GitHub-style timeline; available in read-only mode. */
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
    <section aria-labelledby="pipeline-history">
      <Heading as="h3" id="pipeline-history" className={styles.panelTitle}>
        <HistoryIcon size={16} aria-hidden="true" /> History
      </Heading>
      {versions.isPending ? <Loading label="Loading history" /> : null}
      {versions.isError ? (
        <ErrorCallout title="Could not load the history" error={versions.error} />
      ) : null}
      {versions.data?.length === 0 ? <p className="muted">Nothing has been saved yet.</p> : null}
      {versions.data && versions.data.length > 0 ? (
        <Timeline className={styles.history}>
          {versions.data.map((item) => (
            <Timeline.Item key={item.version}>
              <Timeline.Badge>
                <GitCommitIcon />
              </Timeline.Badge>
              <Timeline.Body>
                <strong>Version {item.version}</strong>{" "}
                <span className="muted">
                  <RelativeTime iso={item.created_at} />
                  {item.created_by ? ` by ${item.created_by}` : ""}
                </span>{" "}
                <Button
                  size="small"
                  onClick={() => {
                    setOpen(item.version);
                  }}
                  aria-label={`View version ${item.version}`}
                >
                  View
                </Button>
              </Timeline.Body>
            </Timeline.Item>
          ))}
        </Timeline>
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
