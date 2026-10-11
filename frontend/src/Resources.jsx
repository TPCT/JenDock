import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Alert,
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
import { containerRoute, projectReturnRoute } from "./model";

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
        return `${name}: service ${label} allocations exceed the environment budget. Increase its budget or update the container limits.`;
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
  verifiedServices = [],
  verifiedEnvironment,
  navigate,
  footerContainer,
  api,
  setDirty,
  setOperationPending,
}) {
  const [saved, setSaved] = useState(null),
    [draft, setDraft] = useState(null),
    [environment, setEnvironment] = useState(
      verifiedEnvironment || "development",
    ),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(""),
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
      <Stack spacing={2} sx={{ py: "var(--space-panel)" }}>
        {error ? (
          <>
            <Alert severity="error">
              Could not load resource budgets. {error}
            </Alert>
            {footerContainer &&
              createPortal(
                <Button onClick={refresh} disabled={!!busy}>
                  Retry loading
                </Button>,
                footerContainer,
              )}
          </>
        ) : (
          <Typography role="status">Loading resource budgets…</Typography>
        )}
      </Stack>
    );
  const env = draft.environments[environment];
  const containers = verifiedServices.filter((row) => row.Names && row.ID);
  return (
    <Stack spacing={2} sx={{ maxWidth: 1000, minWidth: 0 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Box>
          <Typography component="h2" variant="h5" sx={{ display: "inline" }}>
            Resource budgets
          </Typography>
          <Hint label="About resource budgets">
            Budgets check saved totals; they do not reserve host capacity or
            enforce a shared project limit. Edit enforced RAM and CPU limits
            from an actual container’s details.
          </Hint>
        </Box>
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
        spacing={2}
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
          Saved allocation total:{" "}
          {format(sum(Object.values(env.services), "memory_mib"))} MiB RAM ·{" "}
          {format(sum(Object.values(env.services), "cpu"))} CPU cores.
        </Typography>
        <Box>
          <Typography variant="h6" component="h3" sx={{ mb: 1 }}>
            Saved allocations
            <Hint label="About saved allocations">
              These records count toward the budget. They do not prove a
              container exists or that its runtime limits are enforced. Removing
              a record changes the budget only after saving; it does not remove
              a container’s RAM or CPU limits.
            </Hint>
          </Typography>
          {Object.entries(env.services).map(([name, limits]) => (
            <Stack
              key={name}
              direction={{ xs: "column", sm: "row" }}
              spacing={1}
              sx={{ borderTop: 1, borderColor: "divider", py: 2, minWidth: 0 }}
              alignItems={{ xs: "stretch", sm: "center" }}
              justifyContent="space-between"
            >
              <Box sx={{ minWidth: 0 }}>
                <Typography sx={{ overflowWrap: "anywhere" }}>
                  {name}
                </Typography>
                <Typography color="text.secondary">
                  {format(limits.memory_mib)} MiB RAM · {format(limits.cpu)} CPU
                  cores allocated
                </Typography>
              </Box>
              <Button
                color="inherit"
                disabled={!!busy}
                aria-label={`Remove allocation record for ${name}`}
                onClick={() =>
                  change(
                    (d) => delete d.environments[environment].services[name],
                  )
                }
              >
                Remove record
              </Button>
            </Stack>
          ))}
          {!Object.keys(env.services).length && (
            <Typography color="text.secondary">
              No saved allocations in this environment.
            </Typography>
          )}
        </Box>
      </Stack>
      <Box>
        <Typography component="h3" variant="h6" sx={{ mb: 1 }}>
          Container RAM and CPU limits
        </Typography>
        <Typography color="text.secondary" sx={{ mb: 2 }}>
          Open a verified container’s details to review its current limits and
          edit RAM or CPU.
        </Typography>
        {environment !== verifiedEnvironment ? (
          <Stack spacing={1}>
            <Typography color="text.secondary">
              Open this project’s{" "}
              {environment === "production" ? "Production" : "Development"}{" "}
              containers to load verified ownership and edit their limits.
            </Typography>
            <Button
              disabled={!!busy || dirty || !navigate}
              onClick={() => navigate(projectReturnRoute(id, environment))}
            >
              View {environment === "production" ? "Production" : "Development"}{" "}
              containers
            </Button>
          </Stack>
        ) : containers.length > 0 &&
          ["development", "production"].includes(verifiedEnvironment) ? (
          <Stack spacing={1}>
            <Typography color="text.secondary">
              {verifiedEnvironment === "production"
                ? "Production"
                : "Development"}{" "}
              containers
            </Typography>
            {containers.map((container) => (
              <Stack
                key={container.ID}
                direction={{ xs: "column", sm: "row" }}
                alignItems={{ xs: "stretch", sm: "center" }}
                justifyContent="space-between"
                spacing={1}
                sx={{
                  borderTop: 1,
                  borderColor: "divider",
                  py: 1.5,
                  minWidth: 0,
                }}
              >
                <Typography sx={{ overflowWrap: "anywhere" }}>
                  {container.Names}
                </Typography>
                <Button
                  disabled={!!busy || dirty || !navigate}
                  aria-label={`View RAM and CPU limits for ${container.Names}`}
                  onClick={() =>
                    navigate(
                      containerRoute(
                        id,
                        container.Names,
                        verifiedEnvironment,
                        "details",
                      ),
                    )
                  }
                >
                  View container limits
                </Button>
              </Stack>
            ))}
          </Stack>
        ) : (
          <Typography color="text.secondary">
            No verified containers are available in the selected project
            environment. Select an environment with verified containers from the
            project page to edit its limits.
          </Typography>
        )}
      </Box>
      {dirty && (
        <Typography role="status" color="text.secondary">
          Unsaved changes · Save or discard budget changes before opening
          container limits.
        </Typography>
      )}
      {footerContainer &&
        createPortal(
          <>
            <Button
              color="inherit"
              disabled={!!busy}
              onClick={() => (dirty ? setConfirmReload(true) : refresh())}
            >
              Reload budgets
            </Button>
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
              }}
            >
              Discard changes
            </Button>
          </>,
          footerContainer,
        )}
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
    </Stack>
  );
}
