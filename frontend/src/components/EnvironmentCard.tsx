import {
  GitBranchIcon,
  GlobeIcon,
  HistoryIcon,
  PlayIcon,
  WorkflowIcon,
} from "@primer/octicons-react";
import {
  BranchName,
  Button,
  Dialog,
  Heading,
  Label,
  LinkButton,
  TextInput,
  VisuallyHidden,
} from "@primer/react";
import { Link } from "react-router-dom";
import type { EnvironmentSummary, Project } from "../api/types";
import { capitalize } from "../lib/format";
import { useCan } from "../lib/permissions";
import box from "../styles/box.module.css";
import { CopySha } from "./CopySha";
import styles from "./EnvironmentCard.module.css";
import { StatusBadge } from "./Status";
import { Duration, RelativeTime } from "./Time";
import { useEnvironmentPower, useEnvironmentPowerAction } from "../api/environmentPowerHooks";
import { useState } from "react";
import { Callout, errorMessage } from "./Feedback";

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
  compact = false,
}: {
  project: Project;
  environment: EnvironmentSummary;
  detailed?: boolean;
  /** List rows: status only, without the shortcut buttons (the project page has them). */
  compact?: boolean;
}) {
  const { last_run: run } = environment;
  const can = useCan();
  const scope = { project: project.slug, env: environment.name };
  const headingId = `env-${project.slug}-${environment.name}`;
  const power = useEnvironmentPower(project.slug, environment.name, detailed);
  const [dialog, setDialog] = useState<"start" | "stop" | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const action = useEnvironmentPowerAction(project.slug, environment.name, dialog ?? "start");
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
      {detailed && power.data ? (
        <div className={box.cluster}>
          <Label
            variant={
              power.data.state === "RUNNING"
                ? "success"
                : power.data.state === "STOPPED"
                  ? "attention"
                  : "secondary"
            }
          >
            {power.data.state === "RUNNING"
              ? "Running"
              : power.data.state === "STOPPED"
                ? "Stopped"
                : "Not deployed"}
          </Label>
          {power.data.state === "RUNNING" && can("environment.stop", scope) ? (
            <Button
              size="small"
              onClick={() => {
                setDialog("stop");
              }}
            >
              Stop
            </Button>
          ) : power.data.state === "STOPPED" && can("environment.start", scope) ? (
            <Button
              size="small"
              variant="primary"
              onClick={() => {
                setDialog("start");
              }}
            >
              Start
            </Button>
          ) : null}
          {power.data.auto_stop_hours ? (
            <span className="muted">
              Auto-stop after {power.data.auto_stop_hours} h without a deploy
              {power.data.auto_stop_due_at
                ? ` · due ${new Date(power.data.auto_stop_due_at).toLocaleString()}`
                : ""}
            </span>
          ) : null}
        </div>
      ) : null}
      {detailed && power.isError ? (
        <p className="muted">Environment power state is unavailable.</p>
      ) : null}
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
      {compact ? null : (
        <div className={box.cluster}>
          {can("run.read", scope) ? (
            <LinkButton
              as={Link}
              to={runsPath(project.slug, environment.name)}
              size="small"
              leadingVisual={HistoryIcon}
              aria-label={`View ${environment.name} runs of ${project.name}`}
            >
              View runs
            </LinkButton>
          ) : null}
          {can("pipeline.read", scope) ? (
            <LinkButton
              as={Link}
              to={pipelinePath(project.slug, environment.name)}
              size="small"
              leadingVisual={WorkflowIcon}
              aria-label={`Edit the ${environment.name} pipeline of ${project.name}`}
            >
              Pipeline
            </LinkButton>
          ) : null}
          {detailed && can("run.create", scope) ? (
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
      )}
      {dialog ? (
        <Dialog
          title={`${dialog === "stop" ? "Stop" : "Start"} ${environment.name}?`}
          onClose={() => {
            setDialog(null);
            setConfirmation("");
          }}
        >
          <div className={box.stack}>
            <p>
              {dialog === "stop"
                ? "Services will stop while containers and volumes stay in place."
                : "Services will start with the last deployed images."}
            </p>
            {dialog === "stop" && environment.name === "production" ? (
              <TextInput
                aria-label="Confirm project slug"
                placeholder={project.slug}
                value={confirmation}
                onChange={(event) => {
                  setConfirmation(event.target.value);
                }}
              />
            ) : null}
            {action.isError ? (
              <Callout variant="danger" role="alert">
                {errorMessage(action.error)}
              </Callout>
            ) : null}
            <div className={box.cluster}>
              <Button
                onClick={() => {
                  setDialog(null);
                }}
              >
                Cancel
              </Button>
              <Button
                variant={dialog === "stop" ? "danger" : "primary"}
                disabled={
                  action.isPending ||
                  (dialog === "stop" &&
                    environment.name === "production" &&
                    confirmation !== project.slug)
                }
                onClick={() => {
                  action.mutate(confirmation, {
                    onSuccess: () => {
                      setDialog(null);
                      setConfirmation("");
                    },
                  });
                }}
              >
                {action.isPending
                  ? "Working…"
                  : dialog === "stop"
                    ? "Stop environment"
                    : "Start environment"}
              </Button>
            </div>
          </div>
        </Dialog>
      ) : null}
    </section>
  );
}
