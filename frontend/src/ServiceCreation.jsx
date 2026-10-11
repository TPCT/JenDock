import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  FormControlLabel,
  Stack,
  Switch,
  Typography,
} from "@mui/material";
import Field from "./Field.jsx";
import Hint from "./Hint.jsx";
import {
  serviceProblems,
  serviceRequest,
  serviceTerminal,
  serviceResultMatches,
  saveServiceDraft,
  restoreServiceDraft,
} from "./serviceCreation.js";
import { creationSessionStorage } from "./projectCreationStorage.js";
import { containerRoute } from "./model.js";

const blank = () => ({
  name: "",
  artifact_id: "",
  memory_mib: "",
  cpu: "",
  desired_state: "stopped",
});
export default function ServiceCreation({
  project,
  environment,
  username,
  active,
  api,
  navigate,
  onChanged,
  onPending,
}) {
  const [cached] = useState(() =>
    restoreServiceDraft(
      creationSessionStorage(),
      username,
      project,
      environment,
    ),
  );
  const [draft, setDraft] = useState(cached?.draft || blank),
    [request, setRequest] = useState(cached?.request || null);
  const [open, setOpen] = useState(false),
    [catalog, setCatalog] = useState(null),
    [services, setServices] = useState([]),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [loadError, setLoadError] = useState(""),
    [operation, setOperation] = useState(null),
    [service, setService] = useState(null),
    [submitted, setSubmitted] = useState(false),
    [statusError, setStatusError] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const lock = useRef(false),
    apiRef = useRef(api),
    changedRef = useRef(onChanged),
    notified = useRef(null);
  apiRef.current = api;
  changedRef.current = onChanged;
  const label = environment === "production" ? "Production" : "Development";
  const problems = serviceProblems(draft),
    artifact = catalog?.templates.find(
      (item) => item.artifact_id === draft.artifact_id,
    );
  useEffect(() => {
    saveServiceDraft(
      creationSessionStorage(),
      username,
      project,
      environment,
      draft,
      request,
    );
  }, [username, project, environment, draft, request]);
  useEffect(() => {
    onPending?.(busy);
    return () => onPending?.(false);
  }, [busy, onPending]);
  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const [choices, records] = await Promise.all([
        apiRef.current("action", {
          action: "service-templates",
          project,
          environment,
        }),
        apiRef.current("action", {
          action: "service-list",
          project,
          environment,
        }),
      ]);
      if (!mounted.current) return;
      if (!Array.isArray(choices.templates) || !Array.isArray(records.services))
        throw Error("Container creation data is incomplete.");
      setCatalog(choices);
      setServices(records.services);
    } catch (failure) {
      if (mounted.current)
        setLoadError(
          failure.message ||
            "Available artifacts and creation records could not be loaded.",
        );
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [project, environment]);
  useEffect(() => {
    if (active) void load();
  }, [active, load]);
  useEffect(() => {
    if (
      !active ||
      !services.some((item) => ["queued", "provisioning"].includes(item.state))
    )
      return;
    const timer = setTimeout(() => void load(), 5000);
    return () => clearTimeout(timer);
  }, [active, services, load]);
  const readOperation = useCallback(
    async (id) => {
      const result = await apiRef.current("action", {
        action: "operation-status",
        operation_id: id,
      });
      if (!mounted.current) return;
      if (result.operation?.operation_id !== id)
        throw Error("The operation response could not be verified.");
      setOperation(result.operation);
      setStatusError("");
      if (serviceTerminal(result.operation.status)) {
        const records = await apiRef.current("action", {
          action: "service-list",
          project,
          environment,
        });
        if (!mounted.current) return;
        if (!Array.isArray(records.services))
          throw Error("The saved container record could not be loaded.");
        setServices(records.services);
        setService(
          records.services.find((item) => item.operation_id === id) || null,
        );
        if (
          ["running", "stopped"].includes(result.operation.status) &&
          notified.current !== id
        ) {
          notified.current = id;
          await changedRef.current?.();
        }
      }
    },
    [project, environment],
  );
  useEffect(() => {
    if (!open || !operation?.operation_id || serviceTerminal(operation.status))
      return;
    let activeRequest = true,
      timer;
    const poll = async () => {
      try {
        await readOperation(operation.operation_id);
      } catch (failure) {
        if (activeRequest)
          setStatusError(failure.message || "Operation status is unavailable.");
      } finally {
        if (activeRequest) timer = setTimeout(poll, 3000);
      }
    };
    timer = setTimeout(poll, 3000);
    return () => {
      activeRequest = false;
      clearTimeout(timer);
    };
  }, [open, operation?.operation_id, operation?.status, readOperation]);
  const submit = async (event) => {
    event.preventDefault();
    if (lock.current) return;
    setSubmitted(true);
    if (!request && Object.values(problems).some(Boolean)) return;
    let payload;
    try {
      payload = serviceRequest(
        request,
        project,
        environment,
        draft,
        catalog,
        crypto.randomUUID(),
      );
    } catch (failure) {
      setError(failure.message);
      return;
    }
    if (
      !saveServiceDraft(
        creationSessionStorage(),
        username,
        project,
        environment,
        draft,
        payload,
      )
    ) {
      setError(
        "The browser could not retain the creation request. No request was sent. Enable session storage and retry.",
      );
      return;
    }
    lock.current = true;
    setBusy(true);
    setError("");
    setRequest(payload);
    try {
      const result = await apiRef.current("action", payload);
      if (!serviceResultMatches(result, payload))
        throw Error(
          "The creation response could not be verified. Check the original request.",
        );
      setService(result.service);
      setOperation(result.operation);
      setStatusError("");
      await load();
      await readOperation(result.operation.operation_id).catch((failure) =>
        setStatusError(failure.message),
      );
    } catch (failure) {
      if (
        failure.status >= 400 &&
        failure.status < 500 &&
        failure.code !== "idempotency_conflict"
      )
        setRequest(null);
      setError(
        failure.message ||
          "The result is unknown. Check the original request before changing its details.",
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  const resume = async () => {
    if (lock.current || operation?.status !== "recovery-required") return;
    lock.current = true;
    setBusy(true);
    setError("");
    const id = operation.operation_id;
    try {
      const result = await apiRef.current("action", {
        action: "service-resume",
        operation_id: id,
      });
      if (
        result.operation?.operation_id !== id ||
        result.service?.operation_id !== id ||
        result.service?.project !== project ||
        result.service?.environment !== environment
      )
        throw Error(
          "The resumed operation could not be verified. Refresh its status.",
        );
      setOperation(result.operation);
      setService(result.service);
      setStatusError("");
      await readOperation(id);
      await load();
    } catch (failure) {
      setError(
        failure.message ||
          "Resume could not be confirmed. Refresh the original operation status.",
      );
      await readOperation(id).catch((statusFailure) =>
        setStatusError(statusFailure.message),
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  const inspectOperation = async (record) => {
    setOpen(true);
    setService(record);
    setOperation({ operation_id: record.operation_id, status: record.state });
    setError("");
    try {
      await readOperation(record.operation_id);
    } catch (failure) {
      setStatusError(failure.message);
    }
  };
  const reset = () => {
    setRequest(null);
    setOperation(null);
    setService(null);
    setDraft(blank());
    setError("");
    setStatusError("");
    setSubmitted(false);
    void load();
  };
  return (
    <>
      <Stack
        direction="row"
        justifyContent="space-between"
        alignItems="center"
        sx={{
          mb: "var(--space-group)",
          gap: "var(--space-group)",
          flexWrap: "wrap",
        }}
      >
        <Typography variant="h6">{label} containers</Typography>
        <Button
          variant="contained"
          disabled={loading || busy}
          onClick={() => {
            setOpen(true);
            if (!catalog) void load();
          }}
        >
          {request ? "Review creation" : "Add container"}
        </Button>
      </Stack>
      {loadError && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={
            <Button color="inherit" onClick={load}>
              Retry
            </Button>
          }
        >
          {loadError}
        </Alert>
      )}
      {!!services.length && (
        <Stack spacing={1} sx={{ mb: 2 }}>
          {services.map((record) => (
            <Box
              key={record.id}
              sx={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 2,
                borderBottom: 1,
                borderColor: "divider",
                py: 1,
              }}
            >
              <Typography sx={{ overflowWrap: "anywhere" }}>
                {record.name} ·{" "}
                {record.state?.replaceAll("-", " ") || "Status unavailable"}
              </Typography>
              <Button
                disabled={
                  busy ||
                  (!!request && record.operation_id !== operation?.operation_id)
                }
                onClick={() => inspectOperation(record)}
              >
                Creation details
              </Button>
            </Box>
          ))}
        </Stack>
      )}
      <Dialog
        open={open}
        onClose={() => !busy && setOpen(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby={`service-create-${environment}`}
        slotProps={{
          paper: { component: "form", onSubmit: submit, noValidate: true },
        }}
      >
        <DialogTitle
          id={`service-create-${environment}`}
          sx={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 1,
          }}
        >
          <Box component="span" sx={{ minWidth: 0, overflowWrap: "anywhere" }}>
            {operation ? "Container creation" : "Add container"}
          </Box>
          <Box component="span" sx={{ flexShrink: 0, display: "inline-flex" }}>
            <Hint label="About container creation">
              Uses a verified build artifact from this project. No database,
              public port, domain or secret configuration is added by this flow.
            </Hint>
          </Box>
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            <Typography fontWeight={600}>
              {project} · {label}
            </Typography>
            {error && <Alert severity="error">{error}</Alert>}
            {loadError && <Alert severity="error">{loadError}</Alert>}
            {operation ? (
              <>
                <Alert
                  severity={
                    ["running", "stopped"].includes(operation.status)
                      ? "success"
                      : ["failed", "recovery-required"].includes(
                            operation.status,
                          )
                        ? "warning"
                        : "info"
                  }
                  role="status"
                >
                  {operation.status?.replaceAll("-", " ")}
                  {operation.detail ? ` · ${operation.detail}` : ""}
                </Alert>
                <Typography sx={{ overflowWrap: "anywhere" }}>
                  {service?.name || request?.values.name}
                </Typography>
                <Typography color="text.secondary" fontSize={12}>
                  Operation {operation.operation_id}
                </Typography>
                {statusError && (
                  <Alert severity="error">
                    {statusError} The last recorded status is shown.
                  </Alert>
                )}
                {operation.status === "recovery-required" && (
                  <Typography>
                    Creation needs review. This screen will not repeat
                    provisioning.
                  </Typography>
                )}
              </>
            ) : (
              <>
                {request && (
                  <Typography color="text.secondary">
                    The original request is retained. Check its result before
                    starting another container.
                  </Typography>
                )}
                {loading && !catalog ? (
                  <Typography role="status">
                    Loading verified artifacts…
                  </Typography>
                ) : catalog && !catalog.templates.length ? (
                  <Alert severity="info">
                    No supported verified build artifacts are available for this
                    project.
                  </Alert>
                ) : null}
                <Field
                  label="Container name"
                  fullWidth
                  required
                  value={draft.name}
                  disabled={busy || !!request}
                  error={submitted && !!problems.name}
                  helperText={
                    submitted ? problems.name || undefined : undefined
                  }
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      name: event.target.value,
                    }))
                  }
                  inputProps={{
                    maxLength: 48,
                    autoCapitalize: "none",
                    spellCheck: false,
                  }}
                />
                <Autocomplete
                  options={catalog?.templates || []}
                  value={artifact || null}
                  disabled={
                    busy || !!request || loading || !catalog?.templates.length
                  }
                  getOptionLabel={(item) =>
                    `${item.label} · ${item.source_sha?.slice(0, 9) || item.artifact_id}`
                  }
                  isOptionEqualToValue={(a, b) =>
                    a.artifact_id === b.artifact_id
                  }
                  onChange={(_, value) =>
                    setDraft((current) => ({
                      ...current,
                      artifact_id: value?.artifact_id || "",
                      desired_state: value?.desired_states?.includes(
                        current.desired_state,
                      )
                        ? current.desired_state
                        : "stopped",
                    }))
                  }
                  renderInput={(params) => (
                    <Field
                      {...params}
                      label="Verified build artifact"
                      required
                      error={submitted && !!problems.artifact_id}
                      helperText={
                        submitted
                          ? problems.artifact_id || undefined
                          : undefined
                      }
                    />
                  )}
                />
                {artifact && (
                  <Box sx={{ overflowWrap: "anywhere" }}>
                    <Typography fontSize={13}>
                      Template: {artifact.id} · Revision {artifact.revision}
                    </Typography>
                    <Typography component="code" fontSize={12} display="block">
                      {artifact.image}
                    </Typography>
                    <Typography fontSize={12}>
                      Build #{artifact.source_job} ·{" "}
                      {artifact.source_environment}
                    </Typography>
                  </Box>
                )}
                <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
                  {[
                    ["memory_mib", "RAM (MiB)"],
                    ["cpu", "CPU cores"],
                  ].map(([key, title]) => (
                    <Field
                      key={key}
                      label={title}
                      required
                      fullWidth
                      type="number"
                      value={draft[key]}
                      disabled={busy || !!request}
                      error={submitted && !!problems[key]}
                      helperText={
                        submitted ? problems[key] || undefined : undefined
                      }
                      inputProps={{
                        min: key === "cpu" ? 0.1 : 16,
                        step: "any",
                        inputMode: "decimal",
                      }}
                      onChange={(event) =>
                        setDraft((value) => ({
                          ...value,
                          [key]: event.target.value,
                        }))
                      }
                    />
                  ))}
                </Stack>
                <Box>
                  <FormControlLabel
                    label="Start after creation"
                    control={
                      <Switch
                        checked={draft.desired_state === "running"}
                        disabled={
                          busy ||
                          !!request ||
                          !artifact?.desired_states?.includes("running")
                        }
                        onChange={(event) =>
                          setDraft((value) => ({
                            ...value,
                            desired_state: event.target.checked
                              ? "running"
                              : "stopped",
                          }))
                        }
                      />
                    }
                  />
                  <Typography color="text.secondary" fontSize={13}>
                    {draft.desired_state === "running"
                      ? "Create and start this container; runtime health must pass."
                      : "Create stopped. Nothing starts automatically."}
                  </Typography>
                </Box>
              </>
            )}
          </Stack>
        </DialogContent>
        <DialogActions
          sx={{
            flexWrap: "wrap",
            gap: 1,
            "& > .MuiButton-root": {
              marginLeft: "0 !important",
              flex: { xs: "1 1 auto", sm: "0 0 auto" },
            },
            "& > .MuiButton-contained": {
              order: { xs: -1, sm: 0 },
              flexBasis: { xs: "100%", sm: "auto" },
            },
          }}
        >
          <Button disabled={busy} onClick={() => setOpen(false)}>
            Close
          </Button>
          {!operation && !request && (
            <Button disabled={busy || loading} onClick={load}>
              Reload artifacts
            </Button>
          )}
          {operation ? (
            <>
              {operation.status === "recovery-required" && (
                <Button variant="contained" disabled={busy} onClick={resume}>
                  {busy ? "Resuming…" : "Resume creation"}
                </Button>
              )}
              <Button
                disabled={busy}
                onClick={() =>
                  readOperation(operation.operation_id).catch((failure) =>
                    setStatusError(failure.message),
                  )
                }
              >
                Refresh status
              </Button>
              {service?.container?.id &&
                service?.container?.name &&
                ["running", "stopped"].includes(operation.status) && (
                  <Button
                    variant="contained"
                    onClick={() => {
                      setOpen(false);
                      navigate(
                        containerRoute(
                          project,
                          service.container.name,
                          environment,
                        ),
                      );
                    }}
                  >
                    View container
                  </Button>
                )}
              {["running", "stopped"].includes(operation.status) && (
                <Button onClick={reset}>New container</Button>
              )}
            </>
          ) : (
            <Button
              type="submit"
              variant="contained"
              disabled={
                busy ||
                loading ||
                (!request && (!catalog?.templates.length || !!loadError))
              }
            >
              {busy
                ? "Submitting…"
                : request
                  ? "Check original request"
                  : "Create container"}
            </Button>
          )}
        </DialogActions>
      </Dialog>
    </>
  );
}
