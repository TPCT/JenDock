import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Link,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import {
  Check,
  ErrorIcon,
  PendingIcon,
  SkippedIcon,
  Refresh,
  Chevron,
  Branch,
  History,
  Terminal,
  Arrow,
  Close,
  Workflow,
  CommitIcon,
} from "./icons.jsx";
import { activeStates, retryStates, statusLabel, date } from "./model.js";
import { englishMessage } from "./legacyMessages.js";
import { environmentLabel, managedDeliverySummary } from "./managedDelivery.js";
import { cleanupStatus } from "./cleanupStatus.js";
import MigrationStatus from "./MigrationStatus.jsx";
import "./RunDetail.css";

const cancellable = [
  "queued",
  "validation-queued",
  "awaiting-validation",
  "auto-deploy-disabled",
];
const failures = [
  "failed",
  "interrupted",
  "rolled-back",
  "needs-review",
  "recovery-failed",
];
const successes = ["succeeded", "validated"];
const stageNames = {
  queued: "Waiting for worker",
  "validation-queued": "Waiting for validation",
  building: "Build images",
  testing: "Run tests",
  deploying: "Deploy services",
  recovering: "Recover deployment",
  succeeded: "Deployment verified",
  validated: "Build verified",
  failed: "Execution failed",
  interrupted: "Execution interrupted",
  "rolled-back": "Deployment rolled back",
  "needs-review": "Review required",
  cancelled: "Run cancelled",
  superseded: "Run superseded",
  "awaiting-validation": "Not started under previous policy",
  "auto-deploy-disabled": "Automatic deployment disabled",
};
const recordedDate = (value) =>
  Number.isFinite(value) && value > 0 ? date(value) : "Not recorded";

// Events are observations, not a prescribed pipeline. An observed "building"
// event never becomes a fabricated successful step when a later event arrives.
export function recordedRunStages(job, events = []) {
  const rows = events
    .filter((event) => String(event.job_id) === String(job.id))
    .slice()
    .sort(
      (a, b) =>
        (a.created || 0) - (b.created || 0) || (a.id || 0) - (b.id || 0),
    );
  const stages = [];
  for (const event of rows) {
    const previous = stages.at(-1);
    if (previous?.status === event.status) previous.events.push(event);
    else
      stages.push({
        key: `event-${event.id ?? stages.length}`,
        status: event.status,
        events: [event],
        source: "events",
      });
  }
  // Some historical transitions updated the job without inserting an event.
  // Show their actual current record separately and identify its source.
  if (!stages.length || stages.at(-1).status !== job.status)
    stages.push({
      key: "job-state",
      status: job.status,
      events: [
        { created: job.updated, detail: job.detail, status: job.status },
      ],
      source: "job",
    });
  return stages;
}

function StatusIcon({ status, current = true, ...props }) {
  const Icon = successes.includes(status)
    ? Check
    : failures.includes(status)
      ? ErrorIcon
      : ["cancelled", "superseded"].includes(status)
        ? SkippedIcon
        : current && activeStates.includes(status)
          ? PendingIcon
          : History;
  const tone = successes.includes(status)
    ? "success"
    : failures.includes(status)
      ? "failure"
      : current && activeStates.includes(status)
        ? "pending"
        : "neutral";
  return (
    <span className={`run-status-icon run-tone-${tone}`}>
      <Icon {...props} />
    </span>
  );
}

export default function RunDetail({
  job,
  project,
  events = [],
  onNavigate,
  onBuild,
  action,
}) {
  const [expanded, setExpanded] = useState({});
  const [cancelOpen, setCancelOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  const submitLock = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    setExpanded({});
    setCancelOpen(false);
    setError("");
  }, [job?.id]);
  if (!job)
    return (
      <Box className="run-detail run-empty">
        <Workflow />
        <Typography variant="h6">Run not found</Typography>
        <Typography color="text.secondary">
          This run is not in the available history.
        </Typography>
        <Button onClick={() => onNavigate("jobs")}>View runs</Button>
      </Box>
    );
  const delivery = managedDeliverySummary(job);
  const cleanup = cleanupStatus(job.cleanup);
  const runLabel = delivery?.label || statusLabel(job.status);
  const runIcon = delivery?.icon || job.status;
  const stages = recordedRunStages(job, events);
  const latestStage = stages.at(-1);
  const failureEvent = [...stages]
    .reverse()
    .flatMap((stage) => [...stage.events].reverse())
    .find((event) => failures.includes(event.status) && event.detail);
  const diagnostic = failures.includes(job.status)
    ? job.detail || failureEvent?.detail
    : job.detail;
  const canCancel = cancellable.includes(job.status);
  const canRetry = !!project?.build_ready && retryStates.includes(job.status);
  const retryBlocked = [
    "recovery-required",
    "reconciling",
    "running",
    "queued",
    "pending",
  ].includes(job.managed_delivery?.status);
  const retryHint = retryBlocked
    ? "Deployment is unavailable until this delivery finishes or runtime reconciliation completes. Open the project for build-only validation."
    : "";
  const mode =
    job.mode === "validate"
      ? "Build and test only"
      : job.mode === "deploy"
        ? "Build, test and deploy"
        : "Not recorded";
  const projectName = project?.label || job.project;
  const repository =
    typeof project?.repository === "string" &&
    /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(project.repository)
      ? project.repository
      : null;
  const commitUrl =
    repository && /^[a-f0-9]{7,40}$/i.test(job.sha || "")
      ? `https://github.com/${repository}/commit/${job.sha}`
      : null;
  const cancel = async () => {
    if (!canCancel || submitLock.current) return;
    submitLock.current = true;
    setBusy(true);
    setError("");
    try {
      await action({ action: "cancel", project: job.project, job_id: job.id });
      if (mounted.current) setCancelOpen(false);
    } catch (failure) {
      if (mounted.current) setError(englishMessage(failure.message));
    } finally {
      submitLock.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const retryLabel =
    job.status === "validated"
      ? "Deploy from branch"
      : job.mode === "validate"
        ? "Re-run validation"
        : ["awaiting-validation", "auto-deploy-disabled"].includes(job.status)
          ? "Build & deploy"
          : "Re-run";
  return (
    <Box className="run-detail">
      <header className="run-header">
        <Box className="run-heading">
          <StatusIcon status={runIcon} />
          <Box>
            <Typography component="h1" variant="h4">
              {projectName} <span className="run-number">#{job.id}</span>
            </Typography>
            <Stack
              direction="row"
              spacing={1}
              alignItems="center"
              className="run-subheading"
            >
              <Typography component="span">{runLabel}</Typography>
              <span aria-hidden="true">·</span>
              <Typography component="span">{mode}</Typography>
            </Stack>
          </Box>
        </Box>
        <Stack
          direction="row"
          spacing={1}
          useFlexGap
          flexWrap="wrap"
          className="run-actions"
        >
          {canRetry && (
            <Tooltip title={retryHint}>
              <span
                tabIndex={retryBlocked ? 0 : undefined}
                aria-label={retryBlocked ? retryHint : undefined}
              >
                <Button
                  variant="outlined"
                  color="inherit"
                  startIcon={<Refresh />}
                  disabled={busy || retryBlocked}
                  onClick={() => onBuild(job.project, job)}
                >
                  {retryLabel}
                </Button>
              </span>
            </Tooltip>
          )}
          {canCancel && (
            <Button
              variant="outlined"
              color="error"
              disabled={busy}
              onClick={() => {
                setError("");
                setCancelOpen(true);
              }}
            >
              Cancel run
            </Button>
          )}
          <Tooltip title="Open project">
            <IconButton
              aria-label="Open project"
              disabled={busy}
              onClick={() =>
                onNavigate(`project/${encodeURIComponent(job.project)}`)
              }
            >
              <Arrow />
            </IconButton>
          </Tooltip>
        </Stack>
      </header>
      <Box component="dl" className="run-facts">
        <Box>
          <Typography component="dt">Environment</Typography>
          <Typography component="dd">
            {environmentLabel(delivery?.environment || job.target?.environment)}
          </Typography>
        </Box>
        <Box>
          <Typography component="dt">Triggered by</Typography>
          <Typography component="dd">
            <History />
            {job.actor || "Not recorded"}
          </Typography>
        </Box>
        <Box>
          <Typography component="dt">Branch</Typography>
          <Typography component="dd">
            <Branch />
            <span>{job.branch || "Not recorded"}</span>
          </Typography>
        </Box>
        <Box>
          <Typography component="dt">Commit</Typography>
          <Typography component="dd">
            <CommitIcon />
            {commitUrl ? (
              <Link
                href={commitUrl}
                target="_blank"
                rel="noopener noreferrer"
                title={job.sha}
              >
                {job.sha.slice(0, 12)}
              </Link>
            ) : (
              <span title={job.sha || undefined}>
                {job.sha ? job.sha.slice(0, 12) : "Not recorded"}
              </span>
            )}
          </Typography>
        </Box>
        <Box>
          <Typography component="dt">Created</Typography>
          <Typography component="dd">{recordedDate(job.created)}</Typography>
        </Box>
        <Box>
          <Typography component="dt">Last updated</Typography>
          <Typography component="dd">{recordedDate(job.updated)}</Typography>
        </Box>
      </Box>
      {job.retry_of && (
        <Button
          size="small"
          className="run-parent"
          startIcon={<History />}
          onClick={() => onNavigate(`job/${job.retry_of}`)}
        >
          Based on run #{job.retry_of}
        </Button>
      )}
      <MigrationStatus key={job.id} record={job.migration} runStatus={job.status} />
      {cleanup && (
        <Box sx={{ mt: "var(--space-panel)" }} role="status">
          {cleanup.pending ? <Alert severity="info"><strong>{cleanup.label}</strong> · {cleanup.detail}</Alert>
            : <Typography color="text.secondary" fontSize={12}>{cleanup.label}</Typography>}
        </Box>
      )}
      <div className="run-workspace">
        <aside className="run-sidebar" aria-label="Run summary">
          <Typography variant="subtitle2" className="run-sidebar-title">
            Execution
          </Typography>
          <div className="run-sidebar-current">
            <Workflow />
            <span>
              {job.mode === "validate"
                ? "Build validation"
                : job.mode === "deploy"
                  ? "Build and deploy"
                  : "Recorded run"}
            </span>
            <StatusIcon status={runIcon} />
          </div>
          <Typography className="run-sidebar-caption">
            {stages.reduce(
              (total, stage) =>
                total + (stage.source === "events" ? stage.events.length : 0),
              0,
            )}{" "}
            recorded events
          </Typography>
        </aside>
        <section
          className="run-execution"
          aria-labelledby="run-execution-title"
        >
          <div className="run-execution-heading">
            <div>
              <Typography variant="h6" id="run-execution-title">
                Execution record
              </Typography>
              <Typography color="text.secondary" variant="body2">
                {runLabel} · updated {recordedDate(job.updated)}
              </Typography>
            </div>
            <Chip
              variant="outlined"
              size="small"
              label={
                job.mode === "validate"
                  ? "Validation"
                  : job.mode === "deploy"
                    ? "Deployment"
                    : "Mode not recorded"
              }
            />
          </div>
          {delivery && (
            <Box
              component="section"
              aria-labelledby="run-containers-title"
              sx={{
                mb: 3,
                border: "1px solid var(--run-border)",
                borderRadius: "6px",
                overflow: "hidden",
              }}
            >
              <Box sx={{ p: 2, background: "var(--run-subtle)" }}>
                <Typography
                  component="h2"
                  variant="subtitle2"
                  id="run-containers-title"
                >
                  Container outcomes
                </Typography>
                <Typography
                  variant="body2"
                  color="text.secondary"
                  sx={{ mt: 0.5, overflowWrap: "anywhere" }}
                >
                  {environmentLabel(delivery.environment)} · {delivery.deployed}{" "}
                  deployed · {delivery.skipped} skipped
                </Typography>
                {delivery.detail && (
                  <Typography
                    variant="body2"
                    sx={{ mt: 1, overflowWrap: "anywhere" }}
                  >
                    {englishMessage(delivery.detail)}
                  </Typography>
                )}
              </Box>
              {delivery.services.length ? (
                <Box component="ul" sx={{ m: 0, p: 0, listStyle: "none" }}>
                  {delivery.services.map((service) => (
                    <Box
                      component="li"
                      key={service.key}
                      sx={{
                        display: "flex",
                        alignItems: "center",
                        flexWrap: "wrap",
                        gap: 1,
                        px: 2,
                        py: 1.5,
                        borderTop: "1px solid var(--run-border)",
                      }}
                    >
                      <StatusIcon status={service.icon} />
                      <Typography
                        variant="body2"
                        sx={{
                          flex: "1 1 140px",
                          minWidth: 0,
                          overflowWrap: "anywhere",
                        }}
                      >
                        {service.name}
                      </Typography>
                      <Typography
                        variant="body2"
                        className={`run-tone-${service.tone}`}
                        sx={{ fontSize: 12 }}
                      >
                        {service.label}
                      </Typography>
                    </Box>
                  ))}
                </Box>
              ) : (
                <Typography variant="body2" sx={{ p: 2 }}>
                  No container outcomes were recorded for this delivery.
                </Typography>
              )}
            </Box>
          )}
          {diagnostic &&
            !stages.some((stage) =>
              stage.events.some((event) => event.detail === diagnostic),
            ) && (
              <div
                className={`run-diagnostic ${failures.includes(job.status) ? "run-diagnostic-failure" : ""}`}
                role={failures.includes(job.status) ? "alert" : undefined}
              >
                <StatusIcon status={job.status} />
                <div>
                  <Typography variant="subtitle2">
                    {failures.includes(job.status)
                      ? "Last recorded diagnostic"
                      : "Recorded result"}
                  </Typography>
                  <Typography className="run-diagnostic-text">
                    {englishMessage(diagnostic)}
                  </Typography>
                </div>
              </div>
            )}
          {!events.some((event) => String(event.job_id) === String(job.id)) && (
            <Alert
              severity="info"
              icon={<History />}
              className="run-record-notice"
            >
              Legacy run: stage-level history is unavailable.
            </Alert>
          )}
          <div className="run-stage-list">
            {stages.map((stage, index) => {
              const current =
                index === stages.length - 1 && stage.status === job.status;
              const open =
                expanded[stage.key] ??
                (stage.key === latestStage.key ||
                  failures.includes(stage.status));
              const title =
                stageNames[stage.status] || statusLabel(stage.status);
              const panelId = `run-${job.id}-stage-${index}`;
              const evidenceLabel = successes.includes(stage.status)
                ? statusLabel(stage.status)
                : failures.includes(stage.status) ||
                    ["cancelled", "superseded"].includes(stage.status)
                  ? statusLabel(stage.status)
                  : current
                    ? "Current state"
                    : "Recorded";
              return (
                <div
                  className={`run-stage ${open ? "run-stage-open" : ""}`}
                  key={stage.key}
                >
                  <button
                    className="run-stage-toggle"
                    type="button"
                    aria-expanded={open}
                    aria-controls={panelId}
                    onClick={() =>
                      setExpanded((previous) => ({
                        ...previous,
                        [stage.key]: !open,
                      }))
                    }
                  >
                    <Chevron className="run-stage-chevron" />
                    <StatusIcon status={stage.status} current={current} />
                    <strong>{title}</strong>
                    <span className="run-stage-outcome">{evidenceLabel}</span>
                    <time>{recordedDate(stage.events[0]?.created)}</time>
                  </button>
                  <Collapse in={open}>
                    <div id={panelId} className="run-stage-body">
                      <Typography className="run-stage-source">
                        {stage.source === "job"
                          ? "Current job record · no corresponding event was recorded"
                          : `${stage.events.length} recorded ${stage.events.length === 1 ? "event" : "events"}`}
                      </Typography>
                      {stage.events.map((event, eventIndex) => (
                        <div className="run-event" key={event.id ?? eventIndex}>
                          <span className="run-event-index" aria-hidden="true">
                            {eventIndex + 1}
                          </span>
                          <div>
                            <time>{recordedDate(event.created)}</time>
                            <Typography>
                              {englishMessage(event.detail) ||
                                "No diagnostic detail was recorded for this state."}
                            </Typography>
                          </div>
                        </div>
                      ))}
                    </div>
                  </Collapse>
                </div>
              );
            })}
          </div>
          <div className="run-record-footer">
            <Terminal />
            <Typography variant="body2">
              These are recorded execution diagnostics. Raw command output is
              not exposed by this monitor.
            </Typography>
          </div>
        </section>
      </div>
      <Dialog
        open={cancelOpen}
        onClose={() => {
          if (!busy) setCancelOpen(false);
        }}
        fullWidth
        maxWidth="sm"
        aria-labelledby="cancel-run-title"
      >
        <DialogTitle
          id="cancel-run-title"
          sx={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          Cancel run #{job.id}?
          <IconButton
            aria-label="Close cancellation confirmation"
            disabled={busy}
            onClick={() => setCancelOpen(false)}
          >
            <Close />
          </IconButton>
        </DialogTitle>
        <DialogContent dividers>
          <Typography>
            This removes the queued run for {projectName} before execution. A
            run that has already started cannot be cancelled.
          </Typography>
          <Box component="dl" className="run-cancel-facts">
            <dt>Branch</dt>
            <dd>{job.branch || "Not recorded"}</dd>
            <dt>Mode</dt>
            <dd>{mode}</dd>
          </Box>
          {!canCancel && (
            <Alert severity="warning">
              This run is no longer waiting. Its updated state is{" "}
              {statusLabel(job.status)}.
            </Alert>
          )}
          {error && (
            <Alert severity="error" sx={{ mt: 2 }}>
              {error}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button
            color="inherit"
            disabled={busy}
            onClick={() => setCancelOpen(false)}
          >
            Keep run
          </Button>
          <Button
            variant="contained"
            color="error"
            disabled={busy || !canCancel}
            onClick={cancel}
            startIcon={
              busy ? <CircularProgress size={16} color="inherit" /> : undefined
            }
          >
            {busy ? "Cancelling…" : "Cancel run"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
