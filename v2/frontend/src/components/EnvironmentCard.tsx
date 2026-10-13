import { GitBranchIcon, HistoryIcon, PlayIcon, WorkflowIcon } from "@primer/octicons-react";
import { Link } from "react-router-dom";
import type { EnvironmentSummary, Project } from "../api/types";
import { capitalize } from "../lib/format";
import { CopySha } from "./CopySha";
import { StatusBadge } from "./Status";
import { Duration, RelativeTime } from "./Time";
import "./EnvironmentCard.css";

export function runsPath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/runs`;
}

const TRIGGER_TEXT = { push: "Push", manual: "Manual run", redeploy: "Redeploy" } as const;

export function EnvironmentCard({
  project,
  environment,
  detailed = false,
}: {
  project: Project;
  environment: EnvironmentSummary;
  detailed?: boolean;
}) {
  const { last_run: run } = environment;
  const headingId = `env-${project.slug}-${environment.name}`;
  return (
    <section className="env" aria-labelledby={headingId}>
      <div className="env-head">
        <h3 id={headingId}>{capitalize(environment.name)}</h3>
        <span className="label">
          {environment.auto_deploy ? "Auto-deploy on" : "Manual deploys"}
        </span>
      </div>
      <p className="env-branch">
        <GitBranchIcon size={14} aria-hidden="true" />
        <span className="sr-only">Branch</span>
        <code>{environment.branch}</code>
      </p>
      {run ? (
        <div className="env-run">
          <div className="env-run-line">
            <StatusBadge status={run.status} />
            <Link to={`/runs/${run.id}`}>Run #{run.number}</Link>
            <span className="muted">
              <RelativeTime iso={run.finished_at ?? run.started_at ?? run.created_at} />
            </span>
          </div>
          {detailed ? (
            <dl className="dl env-run-detail">
              <dt>Commit</dt>
              <dd>
                <CopySha sha={run.commit_sha} />
              </dd>
              <dt>Started by</dt>
              <dd>{TRIGGER_TEXT[run.trigger]}</dd>
              <dt>Duration</dt>
              <dd>
                <Duration startedAt={run.started_at} finishedAt={run.finished_at} />
              </dd>
            </dl>
          ) : null}
        </div>
      ) : (
        <p className="muted">
          No runs yet.{" "}
          {environment.auto_deploy
            ? `Push to ${environment.branch} to start one.`
            : "Open runs to start one manually."}
        </p>
      )}
      <div className="row-actions">
        <Link
          className="btn"
          to={runsPath(project.slug, environment.name)}
          aria-label={`View ${environment.name} runs of ${project.name}`}
        >
          <HistoryIcon size={16} aria-hidden="true" />
          View runs
        </Link>
        <Link
          className="btn"
          to={`/projects/${encodeURIComponent(project.slug)}/environments/${environment.name}/pipeline`}
          aria-label={`Edit the ${environment.name} pipeline of ${project.name}`}
        >
          <WorkflowIcon size={16} aria-hidden="true" />
          Pipeline
        </Link>
        {detailed ? (
          <Link
            className="btn btn-primary"
            to={`${runsPath(project.slug, environment.name)}?run=new`}
            aria-label={`Run pipeline for ${environment.name} of ${project.name}`}
          >
            <PlayIcon size={16} aria-hidden="true" />
            Run pipeline
          </Link>
        ) : null}
      </div>
    </section>
  );
}
