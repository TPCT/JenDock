import { ClockIcon, GitBranchIcon, StopwatchIcon } from "@primer/octicons-react";
import { BranchName, VisuallyHidden } from "@primer/react";
import { Link } from "react-router-dom";
import type { RunSummary } from "../api/types";
import { STATUS_LABEL, capitalize } from "../lib/format";
import { CopySha } from "./CopySha";
import styles from "./RunList.module.css";
import { StatusIcon } from "./Status";
import { Duration, RelativeTime } from "./Time";

const TRIGGER_TEXT = { push: "Push", manual: "Manual", redeploy: "Redeploy" } as const;

/** Modelled on the GitHub Actions run list: state icon, title and meta on the left, times right. */
export function RunList({
  runs,
  showProject = false,
}: {
  runs: RunSummary[];
  showProject?: boolean;
}) {
  return (
    <ul className={styles.runs} aria-label="Runs">
      {runs.map((run) => (
        <li key={run.id} className={styles.row}>
          <span className={styles.state}>
            <StatusIcon status={run.status} />
            <VisuallyHidden>{STATUS_LABEL[run.status]}</VisuallyHidden>
          </span>
          <div className={styles.main}>
            <Link to={`/runs/${run.id}`} className={styles.title}>
              {showProject ? `${run.project} · ${capitalize(run.environment)} · ` : ""}Run #
              {run.number}
            </Link>
            <div className={styles.meta}>
              <span>{TRIGGER_TEXT[run.trigger]}</span>
              <CopySha sha={run.commit_sha} />
              <span className={styles.branch}>
                <GitBranchIcon size={14} aria-hidden="true" />
                <VisuallyHidden>Branch</VisuallyHidden>
                <BranchName as="span">{run.branch}</BranchName>
              </span>
            </div>
          </div>
          <div className={styles.times}>
            <span className={styles.time}>
              <ClockIcon size={14} aria-hidden="true" />
              <RelativeTime iso={run.created_at} />
            </span>
            <span className={styles.time}>
              <StopwatchIcon size={14} aria-hidden="true" />
              <VisuallyHidden>Duration </VisuallyHidden>
              <Duration startedAt={run.started_at} finishedAt={run.finished_at} />
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
