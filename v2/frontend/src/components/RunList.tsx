import { GitBranchIcon } from "@primer/octicons-react";
import { Link } from "react-router-dom";
import type { RunSummary } from "../api/types";
import { capitalize } from "../lib/format";
import { CopySha } from "./CopySha";
import { StatusBadge } from "./Status";
import { Duration, RelativeTime } from "./Time";
import "./RunList.css";

const TRIGGER_TEXT = { push: "Push", manual: "Manual", redeploy: "Redeploy" } as const;

export function RunList({
  runs,
  showProject = false,
}: {
  runs: RunSummary[];
  showProject?: boolean;
}) {
  return (
    <ul className="runs" aria-label="Runs">
      {runs.map((run) => (
        <li key={run.id} className="run-row">
          <div className="run-status">
            <StatusBadge status={run.status} />
          </div>
          <div className="run-main">
            <Link to={`/runs/${run.id}`} className="run-title">
              {showProject ? `${run.project} · ${capitalize(run.environment)} · ` : ""}Run #
              {run.number}
            </Link>
            <div className="run-meta">
              <span className="run-branch">
                <GitBranchIcon size={14} aria-hidden="true" />
                <span className="sr-only">Branch</span>
                <code>{run.branch}</code>
              </span>
              <CopySha sha={run.commit_sha} />
              <span className="label">{TRIGGER_TEXT[run.trigger]}</span>
            </div>
          </div>
          <div className="run-time muted">
            <RelativeTime iso={run.created_at} />
            <span>
              <span className="sr-only">Duration </span>
              <Duration startedAt={run.started_at} finishedAt={run.finished_at} />
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
