import React, { useCallback, useEffect, useRef, useState } from "react";
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
import Field from "./Field.jsx";
import Hint from "./Hint.jsx";
import {
  argvFields,
  profileDraft,
  profileProblems,
  profilePayload,
} from "./buildProfile.js";
const names = { development: "Development", production: "Production" };
const commandNames = {
  test_command: "Build test command",
  command: "Container command",
  healthcheck: "Health check command",
};

function EnvironmentProfile({
  id,
  environment,
  active,
  api,
  onDirty,
  onPending,
  onSaved,
}) {
  const [profile, setProfile] = useState(null),
    [draft, setDraft] = useState(null),
    [open, setOpen] = useState(false),
    [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [submitted, setSubmitted] = useState(false),
    [confirmReload, setConfirmReload] = useState(false);
  const apiRef = useRef(api),
    savedRef = useRef(onSaved),
    lock = useRef(false),
    mounted = useRef(true);
  apiRef.current = api;
  savedRef.current = onSaved;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const dirty =
    !!draft &&
    JSON.stringify(draft) !== JSON.stringify(profileDraft(profile?.values));
  const problems = draft ? profileProblems(draft) : {};
  useEffect(() => {
    onDirty(environment, dirty);
    return () => onDirty(environment, false);
  }, [environment, dirty, onDirty]);
  useEffect(() => {
    onPending(environment, busy === "saving");
    return () => onPending(environment, false);
  }, [environment, busy, onPending]);
  const accept = (result) => {
    const value = result.profile;
    if (
      value?.project !== id ||
      value?.environment !== environment ||
      !Number.isInteger(value.profile_revision)
    )
      throw Error(
        "The returned source profile does not match this environment.",
      );
    return value;
  };
  const load = useCallback(async () => {
    if (lock.current) return;
    lock.current = true;
    setBusy("loading");
    setError("");
    try {
      const result = await apiRef.current("action", {
        action: "build-profile-get",
        project: id,
        environment,
      });
      if (!mounted.current) return;
      const value = accept(result);
      setProfile(value);
      setDraft(profileDraft(value.values));
      setSubmitted(false);
      setConfirmReload(false);
    } catch (failure) {
      if (mounted.current)
        setError(failure.message || "Source profile could not be loaded.");
    } finally {
      lock.current = false;
      if (mounted.current) setBusy("");
    }
  }, [id, environment]);
  useEffect(() => {
    if (active && !profile && !error) void load();
  }, [active, profile, error, load]);
  const save = async (event) => {
    event.preventDefault();
    if (lock.current) return;
    setSubmitted(true);
    setError("");
    setMessage("");
    let body;
    try {
      body = profilePayload(id, environment, profile, draft);
    } catch (failure) {
      setError(failure.message);
      return;
    }
    lock.current = true;
    setBusy("saving");
    try {
      const result = await apiRef.current("action", body);
      const value = accept(result);
      if (value.profile_revision <= profile.profile_revision)
        throw Error(
          "The saved revision could not be verified. Reload to check the result.",
        );
      setProfile(value);
      setDraft(profileDraft(value.values));
      setSubmitted(false);
      setMessage("Source and build settings saved.");
      await savedRef.current?.();
    } catch (failure) {
      setError(
        failure.message ||
          "Save could not be confirmed. Reload to check the saved revision before retrying.",
      );
    } finally {
      lock.current = false;
      setBusy("");
    }
  };
  const update = (key, value) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setMessage("");
    setConfirmReload(false);
  };
  return (
    <>
      <Box
        sx={{
          borderTop: 1,
          borderColor: "divider",
          py: 2,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 2,
        }}
      >
        <Box>
          <Typography fontWeight={600}>
            Source &amp; build{" "}
            <Hint label="About source and build">
              Builds a verified artifact from this environment’s repository
              branch. Saving does not deploy or change existing containers.
            </Hint>
          </Typography>
          <Typography color="text.secondary" fontSize={13}>
            {busy === "loading"
              ? "Loading…"
              : error && !profile
                ? "Unavailable"
                : profile?.values
                  ? "Configured"
                  : profile
                    ? "Not configured"
                    : "Not loaded"}
            {dirty ? " · Unsaved changes" : ""}
          </Typography>
        </Box>
        <Button
          variant="outlined"
          onClick={() => {
            setOpen(true);
            if (!profile) void load();
          }}
          disabled={!!busy}
        >
          {profile?.values ? "Edit" : "Configure"}
        </Button>
      </Box>
      <Dialog
        open={open}
        onClose={() => busy !== "saving" && setOpen(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby={`build-profile-${environment}`}
        slotProps={{
          paper: { component: "form", onSubmit: save, noValidate: true },
        }}
      >
        <DialogTitle id={`build-profile-${environment}`}>
          Source &amp; build
          <Typography color="text.secondary" fontSize={13} sx={{ mt: 1 }}>
            {id} · {names[environment]}
          </Typography>
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            {error && <Alert severity="error">{error}</Alert>}
            {message && <Alert severity="success">{message}</Alert>}
            {confirmReload && (
              <Alert severity="warning">
                Reloading discards the unsaved edits for {names[environment]}.
              </Alert>
            )}
            {!draft ? (
              <Typography role="status">
                {busy
                  ? "Loading source profile…"
                  : "Source profile unavailable. Retry loading below."}
              </Typography>
            ) : (
              <>
                <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
                  {[
                    ["dockerfile", "Dockerfile"],
                    ["context", "Build context"],
                  ].map(([key, label]) => (
                    <Field
                      key={key}
                      label={label}
                      value={draft[key]}
                      fullWidth
                      disabled={!!busy}
                      error={submitted && !!problems[key]}
                      helperText={
                        submitted ? problems[key] || undefined : undefined
                      }
                      inputProps={{ maxLength: 240, spellCheck: false }}
                      onChange={(event) => update(key, event.target.value)}
                    />
                  ))}
                </Stack>
                <Field
                  label="Build stage (optional)"
                  value={draft.target}
                  fullWidth
                  disabled={!!busy}
                  error={submitted && !!problems.target}
                  helperText={
                    submitted ? problems.target || undefined : undefined
                  }
                  onChange={(event) => update("target", event.target.value)}
                />
                <Box>
                  <Typography fontWeight={600}>
                    HTTP endpoint{" "}
                    <Hint label="About the HTTP endpoint">
                      Declare the container's HTTP port and health path for
                      future routing. Leave the port blank for services without
                      HTTP. Saving this metadata does not publish a domain,
                      configure routing or deploy a container.
                    </Hint>
                  </Typography>
                  <Stack
                    direction={{ xs: "column", sm: "row" }}
                    spacing={2}
                    sx={{ mt: 2 }}
                  >
                    <Field
                      label="HTTP port (optional)"
                      value={draft.http_port}
                      fullWidth
                      disabled={!!busy}
                      error={submitted && !!problems.http_port}
                      helperText={
                        submitted ? problems.http_port || undefined : undefined
                      }
                      inputProps={{ inputMode: "numeric", maxLength: 5 }}
                      onChange={(event) =>
                        update("http_port", event.target.value)
                      }
                    />
                    <Field
                      label="HTTP health path"
                      value={draft.http_health_path}
                      fullWidth
                      disabled={!!busy || !draft.http_port}
                      error={submitted && !!problems.http_health_path}
                      helperText={
                        submitted
                          ? problems.http_health_path || undefined
                          : undefined
                      }
                      inputProps={{
                        maxLength: 240,
                        spellCheck: false,
                        autoCapitalize: "none",
                      }}
                      onChange={(event) =>
                        update("http_health_path", event.target.value)
                      }
                    />
                  </Stack>
                </Box>
                <Typography fontWeight={600}>
                  Commands{" "}
                  <Hint label="How command arguments work">
                    Enter the executable on the first line and each argument on
                    its own line. Spaces stay inside an argument; do not add
                    shell quotes. Commands run inside the build or application
                    container, not a host shell.
                  </Hint>
                </Typography>
                {argvFields.map((key) => (
                  <Field
                    key={key}
                    label={commandNames[key]}
                    value={draft[key]}
                    multiline
                    minRows={2}
                    maxRows={8}
                    fullWidth
                    disabled={!!busy}
                    error={submitted && !!problems[key]}
                    helperText={
                      submitted && problems[key]
                        ? problems[key]
                        : "One argument per line"
                    }
                    inputProps={{ spellCheck: false }}
                    onChange={(event) => update(key, event.target.value)}
                  />
                ))}
                <Field
                  label="Container user (UID:GID)"
                  placeholder="1000:1000"
                  value={draft.user}
                  fullWidth
                  disabled={!!busy}
                  error={submitted && !!problems.user}
                  helperText={
                    submitted ? problems.user || undefined : undefined
                  }
                  onChange={(event) => update("user", event.target.value)}
                />
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
          <Button disabled={busy === "saving"} onClick={() => setOpen(false)}>
            Close
          </Button>
          {dirty && (
            <Button
              disabled={!!busy}
              onClick={() => {
                setDraft(profileDraft(profile.values));
                setError("");
                setConfirmReload(false);
                setSubmitted(false);
              }}
            >
              Discard changes
            </Button>
          )}
          <Button
            disabled={!!busy}
            onClick={() =>
              dirty && !confirmReload ? setConfirmReload(true) : load()
            }
          >
            {confirmReload
              ? "Discard and reload"
              : profile
                ? "Reload"
                : "Retry loading"}
          </Button>
          {draft && (
            <Button
              type="submit"
              variant="contained"
              disabled={!!busy || !dirty}
            >
              {busy === "saving" ? "Saving…" : "Save settings"}
            </Button>
          )}
        </DialogActions>
      </Dialog>
    </>
  );
}
export default function BuildProfile({
  id,
  environment,
  active = true,
  api,
  onDirty,
  onPending,
  onSaved,
}) {
  const [dirty, setDirty] = useState({}),
    [pending, setPending] = useState({});
  const updateDirty = useCallback(
    (name, value) =>
      setDirty((current) =>
        current[name] === value ? current : { ...current, [name]: value },
      ),
    [],
  );
  const updatePending = useCallback(
    (name, value) =>
      setPending((current) =>
        current[name] === value ? current : { ...current, [name]: value },
      ),
    [],
  );
  useEffect(() => {
    onDirty?.(Object.values(dirty).some(Boolean));
    return () => onDirty?.(false);
  }, [dirty, onDirty]);
  useEffect(() => {
    onPending?.(Object.values(pending).some(Boolean));
    return () => onPending?.(false);
  }, [pending, onPending]);
  return Object.keys(names).map((name) => (
    <Box
      key={id + name}
      sx={{ display: environment === name ? "block" : "none" }}
    >
      <EnvironmentProfile
        id={id}
        environment={name}
        active={active && environment === name}
        api={api}
        onDirty={updateDirty}
        onPending={updatePending}
        onSaved={onSaved}
      />
    </Box>
  ));
}
