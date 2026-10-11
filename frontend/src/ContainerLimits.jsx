import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from "@mui/material";
import Field from "./Field";
import Hint from "./Hint";
import {
  limitFields as definitions,
  limitProblems,
  limitRequest,
} from "./containerLimits.js";

const display = (value, unit) =>
  !Number.isFinite(value) || value < 0
    ? "Unavailable"
    : value > 0
      ? `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 }).format(value)} ${unit}`
      : "Unlimited";
const initial = (detail) =>
  Object.fromEntries(
    definitions.map(([key]) => [
      key,
      detail.resources?.[key] > 0 ? String(detail.resources[key]) : "",
    ]),
  );

export default function ContainerLimits({
  project,
  name,
  detail,
  api,
  disabled,
  disabledReason,
  onDirty,
  onPending,
  onApplied,
  onNeedsInspection,
  onInspect,
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(null);
  const [reviewed, setReviewed] = useState(null);
  const [request, setRequest] = useState(null);
  const [attempted, setAttempted] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [resolved, setResolved] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const lock = useRef(false);
  const inputs = useRef({});
  const dirty =
    !!draft &&
    (!!request ||
      JSON.stringify(draft) !== JSON.stringify(initial(reviewed || detail)));
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);
  useEffect(() => {
    onPending(busy);
    return () => onPending(false);
  }, [busy, onPending]);
  const inspect = async (target = detail) => {
    const current = await api("action", {
      action: "resource-details",
      project,
      container: name,
    });
    if (
      current.container !== name ||
      current.container_id !== target.container_id ||
      current.revision !== target.revision
    ) {
      onNeedsInspection();
      throw Error(
        "This container or its configuration changed. Close and refresh its details before editing limits.",
      );
    }
    if (
      !current.resources ||
      definitions.some(
        ([key]) =>
          !Number.isFinite(current.resources[key]) ||
          current.resources[key] < 0,
      )
    )
      throw Error(
        "Current RAM and CPU limits are unavailable. Retry inspection.",
      );
    if (
      current.resource_policy?.configured &&
      (!Number.isInteger(current.resource_policy.revision) ||
        current.resource_policy.revision < 0)
    )
      throw Error(
        "The resource budget revision is unavailable. Retry inspection before applying limits.",
      );
    return current;
  };
  const edit = async () => {
    setOpen(true);
    if (draft || lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const current = await inspect();
      setReviewed(current);
      setDraft(initial(current));
      setSubmitted(false);
    } catch (error) {
      setError(
        error.message ||
          "Current limits could not be inspected. Retry loading.",
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  const policyBlocked = reviewed?.resource_policy?.editable === false;
  const problems = limitProblems(draft);
  const clear = () => {
    setDraft(null);
    setReviewed(null);
    setRequest(null);
    setAttempted(false);
    setResolved(false);
    setSubmitted(false);
    setError("");
    setOpen(false);
  };
  const apply = async (event) => {
    event.preventDefault();
    if (
      lock.current ||
      resolved ||
      !draft ||
      policyBlocked ||
      (disabled && !request)
    )
      return;
    setSubmitted(true);
    const invalid = definitions.find(([key]) => problems[key]);
    if (invalid) {
      inputs.current[invalid[0]]?.focus();
      return;
    }
    let payload;
    try {
      payload = limitRequest(
        request,
        { project, name, reviewed, draft },
        request?.request_id || crypto.randomUUID(),
      );
    } catch (failure) {
      setResolved(true);
      setError(failure.message || "The reviewed limits are unavailable. Inspect the container again.");
      onNeedsInspection();
      return;
    }
    setRequest(payload);
    setAttempted(true);
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await api("action", payload);
      if (result.status !== "succeeded") {
        setResolved(
          ["failed", "rolled-back", "recovery-failed"].includes(result.status),
        );
        setError(
          result.message ||
            "The operation has not completed. Check the current request before starting another.",
        );
        onNeedsInspection();
        return;
      }
      clear();
      setMessage(result.message || "Container limits applied and verified.");
      await onApplied(result.message);
    } catch (error) {
      const rejected = [400, 404, 409].includes(error.status);
      setResolved(rejected);
      setError(
        rejected
          ? `${error.message} Inspect the current limits before trying again.`
          : `${error.message || "The result could not be confirmed."} Check this request to retrieve its recorded result or run it if it never started.`,
      );
      onNeedsInspection();
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return (
    <Box
      sx={{
        my: "var(--space-section)",
        py: "var(--space-group)",
        borderTop: 1,
        borderBottom: 1,
        borderColor: "divider",
      }}
    >
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ sm: "center" }}
        spacing={2}
      >
        <Box>
          <Typography variant="h6">
            Container limits{" "}
            <Hint label="About container limits">
              These are the inspected RAM and CPU limits on this container.
              Editing applies limits to its existing Compose service without
              assigning an environment.
            </Hint>
          </Typography>
          <Typography color="text.secondary" sx={{ mt: 1 }}>
            RAM:{" "}
            {detail.resources
              ? display(detail.resources.memory_mib, "MiB")
              : "Unavailable"}{" "}
            · CPU:{" "}
            {detail.resources
              ? display(detail.resources.cpu, "cores")
              : "Unavailable"}
          </Typography>
        </Box>
        <Button
          variant="outlined"
          disabled={busy || (!request && disabled) || !detail.resources}
          onClick={edit}
        >
          {request
            ? "Review limit request"
            : draft && dirty
              ? "Continue editing limits"
              : "Edit limits"}
        </Button>
      </Stack>
      {disabled && !request && disabledReason && (
        <Typography color="text.secondary" fontSize={13} sx={{ mt: 1 }}>
          {disabledReason}
        </Typography>
      )}
      {message && (
        <Alert severity="success" role="status" sx={{ mt: 2 }}>
          {message}
        </Alert>
      )}
      <Dialog
        open={open}
        onClose={() => !busy && setOpen(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="container-limits-title"
        slotProps={{
          paper: { component: "form", noValidate: true, onSubmit: apply },
        }}
      >
        <DialogTitle id="container-limits-title">
          Edit container limits
        </DialogTitle>
        <DialogContent dividers>
          {!draft &&
            (busy ? (
              <Typography role="status">
                Inspecting current container limits…
              </Typography>
            ) : (
              <Stack spacing={2}>
                <Alert severity="error">
                  {error || "Current limits are unavailable."}
                </Alert>
              </Stack>
            ))}
          {draft && (
            <Stack spacing={2}>
              <Box>
                <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
                  {name}
                </Typography>
                <Typography color="text.secondary" fontSize={13}>
                  {project} · {reviewed.state}
                </Typography>
              </Box>
              <Typography color="text.secondary">
                Current RAM: {display(reviewed.resources.memory_mib, "MiB")} ·
                CPU: {display(reviewed.resources.cpu, "cores")}
              </Typography>
              <Box
                sx={{
                  display: "grid",
                  gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" },
                  gap: 2,
                }}
              >
                {definitions.map(([key, label, min, max]) => (
                  <Field
                    key={key}
                    fullWidth
                    required
                    type="number"
                    label={label}
                    value={draft[key]}
                    inputRef={(node) => {
                      inputs.current[key] = node;
                    }}
                    disabled={busy || attempted}
                    error={submitted && !!problems[key]}
                    helperText={
                      submitted ? problems[key] || undefined : undefined
                    }
                    inputProps={{ min, max, step: "any", inputMode: "decimal" }}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        [key]: event.target.value,
                      }))
                    }
                  />
                ))}
              </Box>
              <Alert severity="warning">
                Applying recreates this container. A running container may be
                briefly unavailable; a stopped container stays stopped. Swap is
                disabled. A RAM limit that is too low can stop the application.
              </Alert>
              {reviewed.resource_policy?.configured &&
                reviewed.resource_policy.limits_ceiling && (
                  <Typography color="text.secondary" fontSize={13}>
                    Budget available for this container: RAM{" "}
                    {`${reviewed.resource_policy.limits_ceiling.memory_mib} MiB`}{" "}
                    · CPU{" "}
                    {`${reviewed.resource_policy.limits_ceiling.cpu} cores`}
                    <Hint label="About available container budget">
                      This includes the container’s current reservation. Saving
                      limits updates its reservation using verified environment
                      ownership.
                    </Hint>
                  </Typography>
                )}
              {reviewed.resource_policy?.configured && (
                <Typography color="text.secondary" fontSize={13}>
                  {reviewed.resource_policy.reason ||
                    "Existing project and environment budgets still apply."}
                </Typography>
              )}
              {error && (
                <Alert severity="error" sx={{ overflowWrap: "anywhere" }}>
                  {error}
                </Alert>
              )}
              {busy && (
                <Typography role="status">
                  Applying and verifying runtime limits…
                </Typography>
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={() => setOpen(false)}>
            Close
          </Button>
          {draft && !attempted && (
            <Button color="inherit" disabled={busy} onClick={clear}>
              Discard
            </Button>
          )}
          {!draft ? (
            <Button disabled={busy} onClick={edit}>
              Retry loading
            </Button>
          ) : resolved ? (
            <Button
              disabled={busy}
              onClick={async () => {
                if (lock.current) return;
                lock.current = true;
                setBusy(true);
                try {
                  const detail = await onInspect();
                  if (!detail)
                    throw Error(
                      "Current details could not be inspected. Retry before applying.",
                    );
                  const updated = await inspect(detail);
                  setReviewed(updated);
                  setRequest(null);
                  setAttempted(false);
                  setResolved(false);
                  setError("");
                } catch (error) {
                  setError(
                    error.message || "Current limits could not be inspected.",
                  );
                } finally {
                  lock.current = false;
                  setBusy(false);
                }
              }}
            >
              Inspect current limits
            </Button>
          ) : (
            <Button
              variant="contained"
              type="submit"
              disabled={
                busy ||
                !draft ||
                policyBlocked ||
                (!request && disabled) ||
                (!request && !dirty)
              }
            >
              {busy
                ? "Applying…"
                : attempted
                  ? "Check request"
                  : "Apply limits"}
            </Button>
          )}
        </DialogActions>
      </Dialog>
    </Box>
  );
}
