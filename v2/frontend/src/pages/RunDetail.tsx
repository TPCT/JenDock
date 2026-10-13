import {
  AlertIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  HistoryIcon,
  StopIcon,
  SyncIcon,
} from "@primer/octicons-react";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { useCancelRun, useProject, useRun } from "../api/hooks";
import type { RunDetail, StageRun, StepRun } from "../api/types";
import { useRunStream } from "../api/useRunStream";
import { useCrumbs } from "../components/Breadcrumbs";
import { CopySha } from "../components/CopySha";
import { Dialog } from "../components/Dialog";
import { EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { LogViewer } from "../components/LogViewer";
import { runsPath } from "../components/EnvironmentCard";
import { StageGraph, groupColumns, stageAnchor } from "../components/StageGraph";
import { StatusBadge, StatusIcon } from "../components/Status";
import { Duration, RelativeTime } from "../components/Time";
import { capitalize, isCancellable, isTerminal } from "../lib/format";
import "./RunDetail.css";

const TRIGGER_TEXT = {
  push: "A push to the branch",
  manual: "A manual run",
  redeploy: "A redeploy",
} as const;

function CancelControl({ run }: { run: RunDetail }) {
  const [confirming, setConfirming] = useState(false);
  const cancel = useCancelRun(run.id);
  if (!isCancellable(run.status)) return null;
  if (run.cancel_requested_at !== null) {
    return (
      <button type="button" className="btn" disabled>
        <StopIcon size={16} aria-hidden="true" />
        Cancel requested
      </button>
    );
  }
  return (
    <>
      <button
        type="button"
        className="btn btn-danger"
        onClick={() => {
          setConfirming(true);
        }}
      >
        <StopIcon size={16} aria-hidden="true" />
        Cancel run
      </button>
      {confirming ? (
        <Dialog
          title={`Cancel run #${run.number}?`}
          onClose={() => {
            setConfirming(false);
          }}
        >
          <p>
            {run.status === "queued"
              ? "The run has not started. It will be removed from the queue."
              : "The step that is running now is stopped and the remaining stages are skipped. Anything already deployed stays deployed."}{" "}
            This cannot be undone.
          </p>
          {cancel.isError ? (
            <div className="callout callout-danger" role="alert">
              <AlertIcon size={16} aria-hidden="true" />
              <div className="callout-body">{errorMessage(cancel.error)}</div>
            </div>
          ) : null}
          <div className="dialog-actions">
            <button
              type="button"
              className="btn"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Keep running
            </button>
            <button
              type="button"
              className="btn btn-danger-solid"
              disabled={cancel.isPending}
              onClick={() => {
                cancel.mutate(undefined, {
                  onSuccess: () => {
                    setConfirming(false);
                  },
                });
              }}
            >
              <StopIcon size={16} aria-hidden="true" />
              {cancel.isPending ? "Cancelling…" : "Cancel run"}
            </button>
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

function StepRow({
  step,
  open,
  onToggle,
  run,
  store,
  stale,
}: {
  step: StepRun;
  open: boolean;
  onToggle: () => void;
  run: RunDetail;
  store: ReturnType<typeof useRunStream>["store"];
  stale: boolean;
}) {
  const panelId = `step-log-${step.id}`;
  const Chevron = open ? ChevronDownIcon : ChevronRightIcon;
  return (
    <li className="step">
      <button
        type="button"
        className="step-head"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <Chevron size={16} aria-hidden="true" />
        <StatusIcon status={step.status} />
        <span className="step-label">
          <span className="sr-only">{step.status}: </span>
          {step.label}
        </span>
        <code className="step-type">{step.step_type}</code>
        <span className="step-time muted">
          <Duration startedAt={step.started_at} finishedAt={step.finished_at} />
        </span>
      </button>
      {open ? (
        <div id={panelId} className="step-body">
          {step.exit_code !== null && step.exit_code !== 0 ? (
            <p className="muted">Exited with code {step.exit_code}.</p>
          ) : null}
          <LogViewer
            store={store}
            runId={run.id}
            stepId={step.id}
            running={step.status === "running"}
            stale={stale}
          />
        </div>
      ) : null}
    </li>
  );
}

function StageSection({
  stage,
  openSteps,
  toggle,
  run,
  store,
  stale,
}: {
  stage: StageRun;
  openSteps: ReadonlySet<number>;
  toggle: (id: number) => void;
  run: RunDetail;
  store: ReturnType<typeof useRunStream>["store"];
  stale: boolean;
}) {
  const headingId = `${stageAnchor(stage.id)}-title`;
  return (
    <section
      id={stageAnchor(stage.id)}
      className="stage box"
      aria-labelledby={headingId}
      tabIndex={-1}
    >
      <header className="box-header">
        <h3 id={headingId} className="stage-title">
          <StatusIcon status={stage.status} />
          {stage.name}
        </h3>
        <span className="muted">
          <Duration startedAt={stage.started_at} finishedAt={stage.finished_at} />
        </span>
      </header>
      {stage.steps.length === 0 ? (
        <p className="box-row muted">No steps have started in this stage.</p>
      ) : (
        <ul className="steps">
          {stage.steps.map((step) => (
            <StepRow
              key={step.id}
              step={step}
              open={openSteps.has(step.id)}
              onToggle={() => {
                toggle(step.id);
              }}
              run={run}
              store={store}
              stale={stale}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function activeStepId(run: RunDetail): number | null {
  const steps = run.stages.flatMap((stage) => stage.steps);
  return (
    (steps.find((s) => s.status === "running") ?? steps.find((s) => s.status === "failed"))?.id ??
    null
  );
}

export function RunDetailPage() {
  const { id } = useParams();
  const runId = Number(id);
  const valid = Number.isInteger(runId) && runId > 0;
  const query = useRun(valid ? runId : 0);
  const run = query.data;
  const project = useProject(run?.project ?? "");
  const projectName = project.data?.name ?? run?.project ?? "Project";
  const { store, connection, reconnect } = useRunStream(runId, run?.status);
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  const touched = useRef(false);

  useCrumbs([
    { label: "Projects", to: "/projects" },
    {
      label: projectName,
      to: run ? `/projects/${encodeURIComponent(run.project)}` : "/projects",
    },
    {
      label: run ? `${capitalize(run.environment)} runs` : "Runs",
      to: run ? runsPath(run.project, run.environment) : "/runs",
    },
    { label: run ? `Run #${run.number}` : "Run" },
  ]);

  const active = run ? activeStepId(run) : null;
  useEffect(() => {
    if (active === null || touched.current) return;
    setOpen(new Set([active]));
  }, [active]);

  if (!valid) {
    return (
      <EmptyState
        icon={<HistoryIcon size={32} aria-hidden="true" />}
        title="Run not found"
        action={
          <Link className="btn" to="/runs">
            Go to runs
          </Link>
        }
      >
        The run number in the address is not valid.
      </EmptyState>
    );
  }
  if (query.isPending) return <Loading label="Loading run" />;
  if (query.isError) {
    if (isApiError(query.error) && query.error.status === 404) {
      return (
        <EmptyState
          icon={<HistoryIcon size={32} aria-hidden="true" />}
          title="Run not found"
          action={
            <Link className="btn" to="/runs">
              Go to runs
            </Link>
          }
        >
          This run does not exist or was removed.
        </EmptyState>
      );
    }
    return (
      <ErrorCallout
        title="Could not load this run"
        error={query.error}
        onRetry={() => void query.refetch()}
      />
    );
  }
  const data = query.data;
  const stale = connection === "stale";

  const toggle = (stepId: number) => {
    touched.current = true;
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(stepId)) next.delete(stepId);
      else next.add(stepId);
      return next;
    });
  };

  const orderedStages = groupColumns(data.stages).flat();

  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="run-heading">
            Run #{data.number} <StatusBadge status={data.status} />
          </h1>
          <p className="subtitle">
            {projectName} · {capitalize(data.environment)}
          </p>
        </div>
        <div className="row-actions">
          <CancelControl run={data} />
        </div>
      </div>

      {stale ? (
        <div className="callout callout-attention stale-banner" role="status">
          <SyncIcon size={16} aria-hidden="true" />
          <div className="callout-body">
            <strong>Live updates are disconnected</strong>
            <span>
              What you see is the last known state and may be out of date. Reconnecting
              automatically.
            </span>
            <div className="callout-actions">
              <button type="button" className="btn" onClick={reconnect}>
                Reconnect now
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {data.cancel_requested_at !== null && !isTerminal(data.status) ? (
        <div className="callout stale-banner" role="status">
          <SyncIcon size={16} aria-hidden="true" className="spin" />
          <div className="callout-body">
            Cancel requested. The run stops as soon as the worker has cleaned up.
          </div>
        </div>
      ) : null}
      {data.message ? <p className="subtitle run-message">{data.message}</p> : null}

      <div className="box run-summary">
        <dl className="dl">
          <dt>Branch</dt>
          <dd>
            <code>{data.branch}</code>
          </dd>
          <dt>Commit</dt>
          <dd>
            <CopySha sha={data.commit_sha} />
          </dd>
          <dt>Started by</dt>
          <dd>{TRIGGER_TEXT[data.trigger]}</dd>
          <dt>Pipeline</dt>
          <dd>
            {data.pipeline_source === "repository"
              ? "Jenkinsfile from the repository"
              : "Pipeline saved in this platform"}
          </dd>
          <dt>Created</dt>
          <dd>
            <RelativeTime iso={data.created_at} />
          </dd>
          <dt>Duration</dt>
          <dd>
            <Duration startedAt={data.started_at} finishedAt={data.finished_at} />
          </dd>
        </dl>
      </div>

      <h2 className="section-title">Stages</h2>
      <div className="box graph-box">
        <StageGraph stages={data.stages} />
      </div>

      <h2 className="section-title">Steps and logs</h2>
      {data.status === "queued" && data.stages.length === 0 ? (
        <InfoQueued />
      ) : (
        <div className="stack">
          {orderedStages.map((stage) => (
            <StageSection
              key={stage.id}
              stage={stage}
              openSteps={open}
              toggle={toggle}
              run={data}
              store={store}
              stale={stale}
            />
          ))}
        </div>
      )}
    </>
  );
}

function InfoQueued() {
  return (
    <p className="muted">
      This run is waiting in the queue. Only one build runs at a time; it will start as soon as the
      one before it finishes.
    </p>
  );
}
