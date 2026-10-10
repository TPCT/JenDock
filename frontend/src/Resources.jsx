import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  IconButton,
  Tooltip,
  Autocomplete,
  Box,
  Button,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Stack,
  Tab,
  Tabs,
  Typography,
} from "@mui/material";
import Field from "./Field";
import Hint from "./Hint";
import { Refresh } from "./icons";

const fields = [
  ["memory_mib", "RAM (MiB)", 16, 1048576],
  ["cpu", "CPU cores", 0.1, 1024],
];
const format = (value) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value);
const sum = (items, key) =>
  items.reduce((total, item) => total + (Number(item[key]) || 0), 0);
function fieldError(value, key, required) {
  const entry = value[key];
  if (entry == null || entry === "")
    return required ? "Enter a service limit." : "";
  const [, label, min, max] = fields.find((field) => field[0] === key);
  return !Number.isFinite(Number(entry)) ||
    Number(entry) < min ||
    Number(entry) > max
    ? `${label} must be between ${format(min)} and ${format(max)}.`
    : "";
}
function Budget({
  label,
  value,
  onChange,
  disabled,
  required = false,
  validate = false,
}) {
  return (
    <Box component="fieldset" sx={{ border: 0, p: 0, m: 0, minWidth: 0 }}>
      <Typography
        component="legend"
        variant="h6"
        sx={{ mb: 2, overflowWrap: "anywhere" }}
      >
        {label}
        {!required && (
          <Hint label={`${label} help`}>
            Set a budget large enough for the saved limits below it. A blank
            budget is allowed only when nothing is allocated for that resource.
          </Hint>
        )}
      </Typography>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", sm: "repeat(2,1fr)" },
          gap: 2,
        }}
      >
        {fields.map(([key, text, min, max]) => (
          <Field
            key={key}
            fullWidth
            type="number"
            label={text}
            value={value[key] ?? ""}
            disabled={disabled}
            required={required}
            error={validate && !!fieldError(value, key, required)}
            helperText={
              validate
                ? fieldError(value, key, required) || undefined
                : undefined
            }
            inputProps={{ min, max, step: "any", inputMode: "decimal" }}
            onChange={(e) => {
              const next = { ...value };
              if (e.target.value === "") delete next[key];
              else next[key] = Number(e.target.value);
              onChange(next);
            }}
          />
        ))}
      </Box>
    </Box>
  );
}
function validation(document) {
  for (const [label, budget, required] of [
    ["Project", document.budget, false],
    ...Object.entries(document.environments).flatMap(([name, env]) => [
      [name, env.budget, false],
      ...Object.entries(env.services).map(([service, limits]) => [
        service,
        limits,
        true,
      ]),
    ]),
  ]) {
    for (const [key] of fields) {
      const problem = fieldError(budget, key, required);
      if (problem) return `${label}: ${problem}`;
    }
  }
  for (const [key, label] of fields) {
    for (const [name, env] of Object.entries(document.environments))
      if (sum(Object.values(env.services), key) > (env.budget[key] || 0) + 1e-8)
        return `${name}: service ${label} allocations exceed the environment budget. Increase its budget or reduce the service limits.`;
    if (
      sum(
        Object.values(document.environments).map((env) => env.budget),
        key,
      ) >
      (document.budget[key] || 0) + 1e-8
    )
      return `Environment ${label} allocations exceed the project budget. Increase the project budget or reduce the environment allocations.`;
  }
  return "";
}
export default function Resources({
  id,
  services = [],
  api,
  setDirty,
  setOperationPending,
}) {
  const [saved, setSaved] = useState(null),
    [draft, setDraft] = useState(null),
    [environment, setEnvironment] = useState("development"),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(""),
    [selected, setSelected] = useState(null),
    [review, setReview] = useState(null),
    [reload, setReload] = useState(0),
    [confirmReload, setConfirmReload] = useState(false),
    [validate, setValidate] = useState(false);
  const lock = useRef(false),
    apiRef = useRef(api);
  apiRef.current = api;
  const dirty =
    !!saved && JSON.stringify(saved.document) !== JSON.stringify(draft);
  useEffect(() => {
    let active = true;
    setError("");
    setBusy("loading");
    apiRef
      .current("action", { action: "resource-policy", project: id })
      .then((r) => {
        if (active) {
          setSaved(r);
          setDraft(r.document);
          setValidate(false);
          setMessage("");
        }
      })
      .catch((e) => active && setError(e.message))
      .finally(() => active && setBusy(""));
    return () => {
      active = false;
    };
  }, [id, reload]);
  useEffect(() => {
    setDirty(dirty);
    return () => setDirty(false);
  }, [dirty, setDirty]);
  useEffect(() => {
    setOperationPending(!!busy);
    return () => setOperationPending(false);
  }, [busy, setOperationPending]);
  const change = (fn) => {
    setMessage("");
    setDraft((value) => {
      const next = structuredClone(value);
      fn(next);
      return next;
    });
  };
  const act = async (operation, fn) => {
    if (lock.current) return;
    lock.current = true;
    setBusy(operation);
    setError("");
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setError(
        e.message ||
          "The request could not be completed. Your changes are retained.",
      );
    } finally {
      lock.current = false;
      setBusy("");
    }
  };
  const refresh = () => {
    setConfirmReload(false);
    setReload((value) => value + 1);
  };
  if (!draft)
    return (
      <Stack spacing={2} sx={{ py: 3 }}>
        {error ? (
          <>
            <Alert severity="error">
              Could not load resource budgets. {error}
            </Alert>
            <Button onClick={refresh} disabled={!!busy}>
              Retry loading
            </Button>
          </>
        ) : (
          <Typography role="status">Loading resource budgets…</Typography>
        )}
      </Stack>
    );
  const env = draft.environments[environment],
    available = [
      ...new Set(services.map((c) => c.Names).filter(Boolean)),
    ].filter(
      (name) =>
        !Object.values(draft.environments).some((e) => e.services[name]),
    );
  return (
    <Stack spacing={3} sx={{ py: 3, maxWidth: 1000, minWidth: 0 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Box>
          <Typography component="h2" variant="h5" sx={{ display: "inline" }}>
            Resource limits
          </Typography>
          <Hint label="About resource limits">
            Budgets check saved totals; they do not reserve host capacity or
            enforce a shared project limit. Save first, then apply limits to
            each service.
          </Hint>
        </Box>
        <Tooltip title="Reload saved budgets">
          <span>
            <IconButton
              aria-label="Reload saved budgets"
              disabled={!!busy}
              onClick={() => (dirty ? setConfirmReload(true) : refresh())}
            >
              <Refresh />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      {error && (
        <Alert severity="error" sx={{ overflowWrap: "anywhere" }}>
          {error}
        </Alert>
      )}
      {message && (
        <Alert severity="success" role="status">
          {message}
        </Alert>
      )}
      <Budget
        label="Project budget"
        value={draft.budget}
        disabled={!!busy}
        validate={validate}
        onChange={(v) => change((d) => (d.budget = v))}
      />
      <Typography color="text.secondary">
        Environment total:{" "}
        {format(
          sum(
            Object.values(draft.environments).map((e) => e.budget),
            "memory_mib",
          ),
        )}{" "}
        MiB RAM ·{" "}
        {format(
          sum(
            Object.values(draft.environments).map((e) => e.budget),
            "cpu",
          ),
        )}{" "}
        CPU cores.
      </Typography>
      <Tabs
        aria-label="Resource environment"
        value={environment}
        onChange={(_, v) => {
          setEnvironment(v);
          setSelected(null);
        }}
      >
        <Tab
          id="resources-development"
          aria-controls="resources-panel"
          value="development"
          label="Development"
          disabled={!!busy}
        />
        <Tab
          id="resources-production"
          aria-controls="resources-panel"
          value="production"
          label="Production"
          disabled={!!busy}
        />
      </Tabs>
      <Stack
        spacing={3}
        role="tabpanel"
        id="resources-panel"
        aria-labelledby={`resources-${environment}`}
      >
        <Budget
          label={
            environment === "production"
              ? "Production budget"
              : "Development budget"
          }
          value={env.budget}
          disabled={!!busy}
          validate={validate}
          onChange={(v) =>
            change((d) => (d.environments[environment].budget = v))
          }
        />
        <Typography color="text.secondary">
          Service total:{" "}
          {format(sum(Object.values(env.services), "memory_mib"))} MiB RAM ·{" "}
          {format(sum(Object.values(env.services), "cpu"))} CPU cores.
        </Typography>
        <Box>
          <Typography variant="h6" sx={{ mb: 2 }}>
            Service limits{" "}
            <Hint label="About service limits">
              Choose an observed container and set both limits. Each container
              can belong to one environment. Removing saved limits does not
              change the container or its runtime limits.
            </Hint>
          </Typography>
          <Autocomplete
            options={available}
            value={selected}
            onChange={(_, v) => setSelected(v)}
            disabled={!!busy}
            noOptionsText="No other observed services"
            renderInput={(params) => <Field {...params} label="Service" />}
          />
          <Button
            sx={{ mt: 1 }}
            disabled={
              !selected || !!busy || Object.keys(env.services).length >= 100
            }
            onClick={() => {
              change(
                (d) => (d.environments[environment].services[selected] = {}),
              );
              setSelected(null);
            }}
          >
            Add service
          </Button>
        </Box>
        {Object.entries(env.services).map(([name, limits]) => (
          <Box
            key={name}
            sx={{ borderTop: 1, borderColor: "divider", pt: 2, minWidth: 0 }}
          >
            <Budget
              label={name}
              value={limits}
              required
              validate={validate}
              disabled={!!busy}
              onChange={(v) =>
                change((d) => (d.environments[environment].services[name] = v))
              }
            />
            <Stack
              direction={{ xs: "column", sm: "row" }}
              spacing={1}
              sx={{ mt: 2 }}
            >
              <Button
                disabled={!!busy || dirty}
                onClick={() =>
                  act("review", async () => {
                    const current = await api("action", {
                      action: "resource-details",
                      project: id,
                      container: name,
                    });
                    setReview({
                      current,
                      name,
                      limits,
                      environment,
                      policy_revision: saved.revision,
                      request_id: crypto.randomUUID(),
                    });
                  })
                }
              >
                Review & apply
              </Button>
              <Button
                color="inherit"
                disabled={!!busy}
                onClick={() =>
                  change(
                    (d) => delete d.environments[environment].services[name],
                  )
                }
              >
                Remove limits
              </Button>
            </Stack>
          </Box>
        ))}
        {!Object.keys(env.services).length && (
          <Typography color="text.secondary">
            No service limits yet. Choose a service above to get started.
          </Typography>
        )}
      </Stack>
      {dirty && (
        <Typography role="status" color="text.secondary">
          Unsaved changes · Save before applying service limits.
        </Typography>
      )}
      <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
        <Button
          variant="contained"
          disabled={!!busy || !dirty}
          onClick={() => {
            setValidate(true);
            const problem = validation(draft);
            if (problem) {
              setError(problem);
              return;
            }
            act("save", async () => {
              const result = await api("action", {
                action: "resource-policy-save",
                project: id,
                revision: saved.revision,
                document: draft,
              });
              setSaved(result);
              setDraft(result.document);
              setMessage(result.message);
            });
          }}
        >
          {busy === "save" ? "Saving…" : "Save budgets"}
        </Button>
        <Button
          color="inherit"
          disabled={!!busy || !dirty}
          onClick={() => {
            setDraft(saved.document);
            setValidate(false);
            setError("");
            setMessage("");
            setSelected(null);
          }}
        >
          Discard changes
        </Button>
      </Stack>
      <Typography color="text.secondary" fontSize={14}>
        Saved budgets do not change running services.
      </Typography>
      <Dialog
        open={confirmReload}
        onClose={() => setConfirmReload(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="resource-reload-title"
      >
        <DialogTitle id="resource-reload-title">
          Discard draft and reload budgets?
        </DialogTitle>
        <DialogContent dividers>
          Your unsaved RAM and CPU changes will be replaced by the latest saved
          settings. Running containers will not change.
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmReload(false)}>Keep editing</Button>
          <Button onClick={refresh}>Discard and reload</Button>
        </DialogActions>
      </Dialog>
      <Dialog
        open={!!review}
        onClose={() => !busy && setReview(null)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="resource-apply-title"
      >
        <DialogTitle id="resource-apply-title">
          Apply service limits
        </DialogTitle>
        <DialogContent dividers>
          {review && (
            <Stack spacing={2}>
              <Typography sx={{ overflowWrap: "anywhere" }}>
                {review.name} ·{" "}
                {review.environment === "production"
                  ? "Production"
                  : "Development"}
              </Typography>
              <Typography>
                RAM:{" "}
                {review.current.resources.memory_mib
                  ? `${format(review.current.resources.memory_mib)} MiB`
                  : "Unlimited"}{" "}
                → {format(review.limits.memory_mib)} MiB
              </Typography>
              <Typography>
                CPU:{" "}
                {review.current.resources.cpu
                  ? `${format(review.current.resources.cpu)} cores`
                  : "Unlimited"}{" "}
                → {format(review.limits.cpu)} cores
              </Typography>
              <Alert severity="warning">
                This recreates this service using its current image. A running
                service may be briefly unavailable. Swap will be disabled for
                this service. A stopped service stays stopped. Lower RAM may
                cause the application to run out of memory.
              </Alert>
              {error && (
                <Alert severity="error" sx={{ overflowWrap: "anywhere" }}>
                  {error} Retrying uses the same operation ID.
                </Alert>
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button disabled={!!busy} onClick={() => setReview(null)}>
            Close
          </Button>
          <Button
            variant="contained"
            disabled={!!busy || !review}
            onClick={() =>
              act("apply", async () => {
                const result = await api("action", {
                  action: "resource-apply",
                  project: id,
                  container: review.name,
                  container_id: review.current.container_id,
                  revision: review.current.revision,
                  environment: review.environment,
                  policy_revision: review.policy_revision,
                  request_id: review.request_id,
                });
                if (result.status !== "succeeded")
                  throw new Error(
                    result.message ||
                      "The operation has not completed successfully.",
                  );
                setMessage(result.message);
                setReview(null);
              })
            }
          >
            {busy === "apply"
              ? "Applying…"
              : error
                ? "Check / retry request"
                : "Apply limits"}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
