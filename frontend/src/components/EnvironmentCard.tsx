import {
  GitBranchIcon,
  GlobeIcon,
  HistoryIcon,
  PlayIcon,
  WorkflowIcon,
} from "@primer/octicons-react";
import { BranchName, Heading, Label, LinkButton, VisuallyHidden } from "@primer/react";
import { Link } from "react-router-dom";
import type { EnvironmentSummary, Project } from "../api/types";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";
import { CopySha } from "./CopySha";
import styles from "./EnvironmentCard.module.css";
import { StatusBadge } from "./Status";
import { Duration, RelativeTime } from "./Time";

export function runsPath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/runs`;
}

export function domainsPath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/domains`;
}

export function pipelinePath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/pipeline`;
}

export const TRIGGER_TEXT = { push: "Push", manual: "Manual run", redeploy: "Redeploy" } as const;

/** One environment of a project: branch, deployment mode, last run and the shortcuts. */
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
    <section className={styles.env} aria-labelledby={headingId}>
      <div className={box.spread}>
        <Heading as="h3" id={headingId} className={styles.title}>
          {capitalize(environment.name)}
        </Heading>
        <Label variant={environment.auto_deploy ? "success" : "secondary"}>
          {environment.auto_deploy ? "Auto-deploy on" : "Auto-deploy off"}
        </Label>
      </div>
      <p className={styles.branch}>
        <GitBranchIcon size={14} aria-hidden="true" />
        <VisuallyHidden>Branch</VisuallyHidden>
        <BranchName as="span">{environment.branch}</BranchName>
      </p>
      {run ? (
        <div className={styles.run}>
          <div className={box.cluster}>
            <StatusBadge status={run.status} />
            <Link to={`/runs/${run.id}`}>Run #{run.number}</Link>
            <span className="muted">
              <RelativeTime iso={run.finished_at ?? run.started_at ?? run.created_at} />
            </span>
          </div>
          {detailed ? (
            <dl className={box.defs}>
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
            ? `Every push to ${environment.branch} builds and deploys.`
            : "Open runs to start one manually."}
        </p>
      )}
      <div className={box.cluster}>
        <LinkButton
          as={Link}
          to={runsPath(project.slug, environment.name)}
          size="small"
          leadingVisual={HistoryIcon}
          aria-label={`View ${environment.name} runs of ${project.name}`}
        >
          View runs
        </LinkButton>
        <LinkButton
          as={Link}
          to={pipelinePath(project.slug, environment.name)}
          size="small"
          leadingVisual={WorkflowIcon}
          aria-label={`Edit the ${environment.name} pipeline of ${project.name}`}
        >
          Pipeline
        </LinkButton>
        {detailed ? (
          <LinkButton
            as={Link}
            to={`${domainsPath(project.slug, environment.name)}?add=1`}
            size="small"
            leadingVisual={GlobeIcon}
            aria-label={`Add a domain to ${environment.name} of ${project.name}`}
          >
            Add domain
          </LinkButton>
        ) : null}
        {detailed ? (
          <LinkButton
            as={Link}
            to={`${runsPath(project.slug, environment.name)}?run=new`}
            size="small"
            variant="primary"
            leadingVisual={PlayIcon}
            aria-label={`Run pipeline for ${environment.name} of ${project.name}`}
          >
            Run pipeline
          </LinkButton>
        ) : null}
      </div>
    </section>
  );
}
