import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import Field from "./Field";
import { Refresh } from "./icons";
import {
  QueueObservation,
  FailureRecords,
  ScheduleRecords,
} from "./WorkloadPanel";
import {
  observedAge,
  retryRequest,
  saveRetryRequest,
  workloadStateMessage,
} from "./workloadPanel.js";
import {
  workloadViewKey,
  workloadResponse,
  retryStorageKey,
  restoreWorkloadRetry,
  retryOperation,
  workloadFailure,
} from "./workloadConsole.js";

export default function WorkloadConsole({
  metadata,
  username,
  api,
  active,
  disabled,
  onPending,
}) {
  const target = metadata?.target;
  if (!target)
    return (
      <Alert severity="info">
        This workload has no verified container target. Provision or inspect its
        container before loading observations.
      </Alert>
    );
  return (
    <WorkloadWorkspace
      key={workloadViewKey(username,target)}
      {...{ metadata, username, api, active, disabled, onPending }}
    />
  );
}
function WorkloadWorkspace({
  metadata,
  username,
  api,
  active,
  disabled,
  onPending,
}) {
  const target = metadata.target,
    role = metadata.role;
  const apiRef = useRef(api);
  apiRef.current = api;
  const generation = useRef(0),
    lock = useRef(false),
    alive = useRef(true);
  const [observation, setObservation] = useState(null),
    [state, setState] = useState("loading"),
    [busy, setBusy] = useState(false);
  const [queue, setQueue] = useState(metadata.configuration?.queues?.[0] || "");
  const [page, setPage] = useState(null),
    [pageObserved, setPageObserved] = useState(null),
    [pageError, setPageError] = useState(""),
    [cursor, setCursor] = useState(null),
    [trail, setTrail] = useState([]);
  const [selection, setSelection] = useState(null),
    [request, setRequest] = useState(null),
    [operation, setOperation] = useState(null),
    [error, setError] = useState(""),
    [restoreError, setRestoreError] = useState("");
  const [clock, setClock] = useState(Date.now() / 1000);
  const key = retryStorageKey(username, target);
  const unresolved =
    !!request && !["requeued", "rejected"].includes(operation?.status);
  const stale =
    observation &&
    ((observedAge(observation.observation.observed_at, clock) ?? Infinity) >
      90 ||
      (pageObserved && (observedAge(pageObserved, clock) ?? Infinity) > 90));
  const targetIdentity = JSON.stringify(target);
  useEffect(() => {
    alive.current = true;
    try {
      const old = restoreWorkloadRetry(sessionStorage, key, target);
      if (old) setRequest(old);
    } catch {
      setRestoreError(
        "The saved retry could not be read. Recovery must be resolved before another retry.",
      );
    }
    return () => {
      alive.current = false;
      generation.current++;
    };
  }, [key]);
  useEffect(() => {
    onPending?.(unresolved || !!selection || !!restoreError || lock.current);
    return () => onPending?.(false);
  }, [unresolved, selection, restoreError, busy, onPending]);
  useEffect(() => {
    const protect = (e) => {
      if (unresolved || selection || restoreError) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [unresolved, selection, restoreError]);
  useEffect(() => {
    const id = setInterval(() => setClock(Date.now() / 1000), 5000);
    return () => clearInterval(id);
  }, []);
  async function load() {
    if (lock.current || !active) return;
    lock.current = true;
    setBusy(true);
    const version = ++generation.current;
    try {
      const main = workloadResponse(
        await apiRef.current("action", {
          action: "workload-role-details",
          ...target,
        }),
        target,
        role,
      );
      if (version !== generation.current || !alive.current) return;
      setObservation(main);
      setState("ready");
      setPageError("");
      const result = main.observation.result;
      const extras =
        role === "queue"
          ? { connection_id: result.connection_id, queue }
          : { registry_revision: result.registry_revision };
      if (role === "queue" && !queue) {
        setPage(null);
        return;
      }
      const next = workloadResponse(
        await apiRef.current("action", {
          action: role === "queue" ? "queue-failures" : "scheduler-history",
          ...target,
          ...extras,
          limit: 25,
          ...(cursor ? { cursor } : {}),
        }),
        target,
        role,
      );
      if (version === generation.current && alive.current) {
        setPage(next.observation.result);
        setPageObserved(next.observation.observed_at);
      }
    } catch (e) {
      if (version === generation.current && alive.current) {
        setObservation(null);
        setPage(null);
        setState(workloadFailure(e));
        setPageError("");
      }
    } finally {
      lock.current = false;
      if (alive.current) setBusy(false);
    }
  }
  useEffect(() => {
    generation.current++;
    setObservation(null);
    setPage(null);
    setState("loading");
    void load();
  }, [targetIdentity, queue, cursor, active]);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => {
      if (!selection && !unresolved) void load();
    }, 15000);
    return () => clearInterval(timer);
  }, [active, targetIdentity, queue, cursor, selection, unresolved]);
  async function retry() {
    if (
      lock.current ||
      request ||
      !selection ||
      disabled ||
      stale ||
      !observation?.capabilities?.retry
    )
      return;
    let captured;
    try {
      captured = retryRequest({
        ...target,
        request_id: crypto.randomUUID(),
        connection_id: observation.observation.result.connection_id,
        queue: selection.queue,
        failed_job_id: selection.failed_job_id,
        failure_revision: selection.failure_revision,
      });
      saveRetryRequest(sessionStorage, key, captured);
    } catch {
      setError(
        "The retry identity could not be retained. No request was sent.",
      );
      return;
    }
    setRequest(captured);
    setSelection(null);
    setError("");
    lock.current = true;
    setBusy(true);
    onPending?.(true);
    try {
      const result = retryOperation(
        await apiRef.current("action", { action: "queue-retry", ...captured }),
        captured,
      );
      if (alive.current) setOperation(result);
    } catch {
      if (alive.current)
        setError(
          "The outcome could not be confirmed. Check the original retry; do not submit another.",
        );
    } finally {
      lock.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function recover() {
    if (lock.current || !request) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const result = retryOperation(
        await apiRef.current("action", {
          action: "workload-operation",
          project: request.project,
          environment: request.environment,
          service_id: request.service_id,
          request_id: request.request_id,
        }),
        request,
      );
      if (alive.current) setOperation(result);
    } catch {
      if (alive.current)
        setError(
          "The original outcome remains unknown. No retry was repeated.",
        );
    } finally {
      lock.current = false;
      if (alive.current) setBusy(false);
    }
  }
  function dismiss() {
    try {
      sessionStorage.removeItem(key);
    } catch {
      setError(
        "The resolved retry could not be removed from browser storage. Retry closing.",
      );
      return;
    }
    setRequest(null);
    setOperation(null);
    setError("");
    void load();
  }
  const result = observation?.observation.result;
  return (
    <Stack spacing={2}>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        gap={1}
      >
        <Typography variant="h6">
          {role === "queue" ? "Queue activity" : "Schedules"}
        </Typography>
        <Tooltip title="Refresh workload observations">
          <span>
            <IconButton
              aria-label="Refresh workload observations"
              disabled={busy || disabled}
              onClick={load}
            >
              <Refresh />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      {restoreError && <Alert severity="error">{restoreError}</Alert>}
      {disabled && (
        <Alert severity="warning">
          Current container operations are unavailable. Observations do not
          authorize a retry.
        </Alert>
      )}
      {state !== "ready" && (
        <Alert severity={state === "loading" ? "info" : "warning"}>
          {workloadStateMessage(state)}
        </Alert>
      )}
      {stale && (
        <Alert severity="warning">{workloadStateMessage("stale")}</Alert>
      )}
      {observation && (
        <Typography color="text.secondary" variant="body2">
          Observed{" "}
          {new Date(observation.observation.observed_at).toLocaleString()} ·{" "}
          {stale ? "Stale" : "Current"}
        </Typography>
      )}
      {result && role === "queue" && (
        <>
          <QueueObservation snapshot={result} />
          <Typography variant="h6">Failed jobs</Typography>
          <Autocomplete
            options={result.queues.map((q) => q.name)}
            value={queue || null}
            disabled={busy || unresolved}
            onChange={(_, value) => {
              setQueue(value || "");
              setCursor(null);
              setTrail([]);
            }}
            renderInput={(params) => <Field {...params} label="Queue" />}
          />
          {page && (
            <FailureRecords
              items={page.items}
              retryAllowed={
                observation.capabilities.retry && !restoreError && !unresolved
              }
              disabled={busy || disabled || stale}
              onRetry={(item) => {
                setSelection({ ...item });
                setError("");
              }}
            />
          )}
        </>
      )}
      {result && page && role === "scheduler" && (
        <ScheduleRecords
          items={result.items}
          registryRevision={result.registry_revision}
          history={page?.items || []}
        />
      )}
      {pageError && <Alert severity="warning">{pageError}</Alert>}
      {page && (
        <Stack direction="row" spacing={1}>
          <Button
            disabled={busy || !trail.length}
            onClick={() => {
              setCursor(trail.at(-1));
              setTrail(trail.slice(0, -1));
            }}
          >
            Previous
          </Button>
          <Button
            disabled={busy || !page.next_cursor}
            onClick={() => {
              setTrail([...trail, cursor]);
              setCursor(page.next_cursor);
            }}
          >
            Next
          </Button>
        </Stack>
      )}
      <Dialog
        open={!!selection || !!request}
        onClose={() => {
          if (!request && !busy) setSelection(null);
        }}
        fullWidth
        maxWidth="sm"
        aria-labelledby="workload-retry-title"
      >
        <DialogTitle id="workload-retry-title">
          {request ? "Retry operation" : "Retry failed job"}
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            <Typography sx={{ overflowWrap: "anywhere" }}>
              {request?.failed_job_id || selection?.failed_job_id}
            </Typography>
            <Typography variant="body2">
              {request?.environment || target.environment} · Queue{" "}
              {request?.queue || selection?.queue}
            </Typography>
            {!request && (
              <Typography>
                This retries one failed job and may repeat its business effects.
              </Typography>
            )}
            {request && (
              <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>
                Request {request.request_id}
              </Typography>
            )}
            {operation && (
              <Alert
                severity={
                  operation.status === "requeued"
                    ? "success"
                    : operation.status === "rejected"
                      ? "warning"
                      : "info"
                }
              >
                {operation.status === "requeued"
                  ? "Requeued. Business completion has not been observed."
                  : operation.status === "rejected"
                    ? "Retry rejected before enqueue. Reload the failure before reviewing another retry."
                    : "The retry outcome is unresolved."}
              </Alert>
            )}
            {error && <Alert severity="error">{error}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          {!request ? (
            <>
              <Button
                color="inherit"
                disabled={busy}
                onClick={() => setSelection(null)}
              >
                Cancel
              </Button>
              <Button
                variant="contained"
                disabled={
                  busy || disabled || stale || !observation?.capabilities?.retry
                }
                onClick={retry}
              >
                Retry job
              </Button>
            </>
          ) : unresolved ? (
            <Button disabled={busy} onClick={recover}>
              {busy ? "Checking…" : "Check original retry"}
            </Button>
          ) : (
            <Button onClick={dismiss}>Close</Button>
          )}
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
