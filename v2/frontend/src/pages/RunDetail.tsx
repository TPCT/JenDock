import { HistoryIcon, HomeIcon, StopIcon, SyncIcon } from "@primer/octicons-react";
import { Button, ConfirmationDialog, NavList, PageLayout, VisuallyHidden } from "@primer/react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { useRetryRun } from "../api/adminHooks";
import { useCancelRun, useProject, useRun } from "../api/hooks";
import type { RunDetail } from "../api/types";
import { useRunStream } from "../api/useRunStream";
import { CopySha } from "../components/CopySha";
import { runsPath } from "../components/EnvironmentCard";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import { StageGraph, groupColumns } from "../components/StageGraph";
import { StageView } from "../components/StageView";
import { StatusBadge, StatusIcon } from "../components/Status";
import { Duration, RelativeTime } from "../components/Time";
import { STATUS_LABEL, capitalize, isCancellable, isTerminal } from "../lib/format";
import box from "../styles/box.module.css";
import styles from "./RunDetail.module.css";

const TRIGGER_TEXT = {
  push: "A push to the branch",
  manual: "A manual run",
  redeploy: "A redeploy",
} as const;

/** "Cancel run" opens a confirmation; once requested the button says so and stays disabled. */
function CancelControl({ run }: { run: RunDetail }) {
  const [confirming, setConfirming] = useState(false);
  const cancel = useCancelRun(run.id);
  if (!isCancellable(run.status)) return null;
  if (run.cancel_requested_at !== null) {
    return (
      <Button leadingVisual={StopIcon} disabled>
        Cancel requested
      </Button>
    );
  }
  return (
    <>
      <Button
        variant="danger"
        leadingVisual={StopIcon}
        onClick={() => {
          setConfirming(true);
        }}
      >
        Cancel run
      </Button>
      {confirming ? (
        <ConfirmationDialog
          title={`Cancel run #${run.number}?`}
          confirmButtonContent="Cancel run"
          cancelButtonContent="Keep running"
          confirmButtonType="danger"
          confirmButtonLoading={cancel.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              setConfirming(false);
              return;
            }
            cancel.mutate(undefined, {
              onSuccess: () => {
                setConfirming(false);
              },
            });
          }}
        >
          <p>
            {run.status === "queued"
              ? "The run has not started. It will be removed from the queue."
              : "The step that is running now is stopped and the remaining stages are skipped. Anything already deployed stays deployed."}{" "}
            This cannot be undone.
          </p>
          {cancel.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(cancel.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </>
  );
}

function activeStep(run: RunDetail): { stageId: number; stepId: number } | null {
  const steps = run.stages.flatMap((stage) => stage.steps.map((step) => ({ stage, step })));
  const hit =
    steps.find(({ step }) => step.status === "running") ??
    steps.find(({ step }) => step.status === "failed");
  return hit ? { stageId: hit.stage.id, stepId: hit.step.id } : null;
}

function InfoQueued() {
  return (
    <p className="muted">
      This run is waiting in the queue. Only one build runs at a time; it will start as soon as the
      one before it finishes.
    </p>
  );
}

export function RunDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const runId = Number(id);
  const valid = Number.isInteger(runId) && runId > 0;
  const query = useRun(valid ? runId : 0);
  const run = query.data;
  const project = useProject(run?.project ?? "");
  const rerun = useRetryRun(runId);
  const projectName = project.data?.name ?? run?.project ?? "Project";
  const { store, connection, reconnect } = useRunStream(runId, run?.status);
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  const touched = useRef(false);

  const active = run ? activeStep(run) : null;
  const activeStepId = active?.stepId ?? null;
  useEffect(() => {
    if (activeStepId === null || touched.current) return;
    setOpen(new Set([activeStepId]));
  }, [activeStepId]);

  if (!valid) {
    return (
      <EmptyState
        icon={<HistoryIcon size={32} aria-hidden="true" />}
        title="Run not found"
        action={
          <Button as={Link} to="/runs">
            Go to runs
          </Button>
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
            <Button as={Link} to="/runs">
              Go to runs
            </Button>
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
  const stages = groupColumns(data.stages).flat();

  // Like Actions the page opens on the summary; a running or failed step is more useful, so
  // the run opens on its stage instead. `?stage=summary` or `?stage=<id>` always wins.
  const wanted = params.get("stage");
  const requested = stages.find((stage) => String(stage.id) === wanted);
  const selected =
    wanted === "summary"
      ? null
      : (requested ??
        (wanted === null ? stages.find((stage) => stage.id === active?.stageId) : undefined) ??
        null);

  const toggle = (stepId: number) => {
    touched.current = true;
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(stepId)) next.delete(stepId);
      else next.add(stepId);
      return next;
    });
  };

  const select = (stageId: number) => {
    void navigate({ search: `?stage=${stageId}` });
  };

  const finished = isTerminal(data.status);

  return (
    <>
      <PageHead
        crumbs={[
          { label: "Projects", to: "/projects" },
          { label: projectName, to: `/projects/${encodeURIComponent(data.project)}` },
          {
            label: `${capitalize(data.environment)} runs`,
            to: runsPath(data.project, data.environment),
          },
          { label: `Run #${data.number}` },
        ]}
        title={`Run #${data.number}`}
        trailing={<StatusBadge status={data.status} />}
        description={
          <span className={styles.subtitle}>
            {projectName} · {capitalize(data.environment)} · <code>{data.branch}</code> ·{" "}
            <CopySha sha={data.commit_sha} />
          </span>
        }
        actions={
          <div className={box.cluster}>
            {finished ? (
              <Button
                leadingVisual={SyncIcon}
                disabled={rerun.isPending}
                onClick={() => {
                  rerun.mutate(undefined, {
                    onSuccess: (created) => {
                      void navigate(`/runs/${created.id}`);
                    },
                  });
                }}
              >
                {rerun.isPending ? "Starting…" : "Re-run"}
              </Button>
            ) : null}
            <CancelControl run={data} />
          </div>
        }
      />

      <div className={`${box.stack} ${box.section}`}>
        {rerun.isError ? (
          <Callout variant="danger" role="alert" title="Could not re-run">
            {errorMessage(rerun.error)}
          </Callout>
        ) : null}

        {stale ? (
          <Callout
            variant="warning"
            role="status"
            title="Live updates are disconnected"
            actions={
              <Button size="small" onClick={reconnect}>
                Reconnect now
              </Button>
            }
          >
            What you see is the last known state and may be out of date. Reconnecting automatically.
          </Callout>
        ) : null}

        {data.cancel_requested_at !== null && !finished ? (
          <Callout role="status">
            Cancel requested. The run stops as soon as the worker has cleaned up.
          </Callout>
        ) : null}
        {data.message ? (
          <Callout variant={data.status === "failed" ? "danger" : "default"}>
            {data.message}
          </Callout>
        ) : null}

        <PageLayout containerWidth="full" padding="none" columnGap="normal" rowGap="normal">
          <PageLayout.Pane position="start" width="small" padding="none" divider="none">
            <NavList aria-label="Run">
              <NavList.Item
                as={Link}
                to={{ search: "?stage=summary" }}
                aria-current={selected === null ? "page" : undefined}
              >
                <NavList.LeadingVisual>
                  <HomeIcon />
                </NavList.LeadingVisual>
                Summary
              </NavList.Item>
              <NavList.Divider />
              <NavList.Group title="Stages">
                {stages.map((stage) => (
                  <NavList.Item
                    key={stage.id}
                    as={Link}
                    to={{ search: `?stage=${stage.id}` }}
                    aria-current={selected?.id === stage.id ? "page" : undefined}
                  >
                    <NavList.LeadingVisual>
                      <StatusIcon status={stage.status} />
                    </NavList.LeadingVisual>
                    {stage.name}
                    <VisuallyHidden>, {STATUS_LABEL[stage.status]}</VisuallyHidden>
                    <NavList.TrailingVisual>
                      <span className="muted">
                        <Duration startedAt={stage.started_at} finishedAt={stage.finished_at} />
                      </span>
                    </NavList.TrailingVisual>
                  </NavList.Item>
                ))}
              </NavList.Group>
            </NavList>
          </PageLayout.Pane>
          <PageLayout.Content as="div" padding="none">
            {selected ? (
              <StageView
                key={selected.id}
                stage={selected}
                openSteps={open}
                toggle={toggle}
                run={data}
                store={store}
                stale={stale}
              />
            ) : (
              <div className={box.stack}>
                <div className={box.box}>
                  <div className={box.header}>
                    <span>Summary</span>
                  </div>
                  <div className={box.row}>
                    <dl className={box.defs}>
                      <dt>Status</dt>
                      <dd>
                        <StatusBadge status={data.status} />
                      </dd>
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
                </div>
                {data.status === "queued" && data.stages.length === 0 ? (
                  <InfoQueued />
                ) : (
                  <div className={box.box}>
                    <div className={box.header}>
                      <span>Stages</span>
                    </div>
                    <StageGraph stages={data.stages} onSelect={select} />
                  </div>
                )}
              </div>
            )}
          </PageLayout.Content>
        </PageLayout>
      </div>
    </>
  );
}
