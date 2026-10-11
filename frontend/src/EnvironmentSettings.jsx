import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  IconButton,
  Tooltip,
  Autocomplete,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  Stack,
  Switch,
  Tab,
  Tabs,
  Typography,
} from "@mui/material";
import Field from "./Field";
import Hint from "./Hint";
import { Refresh } from "./icons";

const names = { development: "Development", production: "Production" };
const preferences = (environment) => ({
  branch: environment.branch || "",
  automatic: !!environment.automatic,
});
const changed = (saved, draft) =>
  !!saved &&
  !!draft &&
  (saved.branch !== draft.branch || !!saved.automatic !== draft.automatic);

export default function EnvironmentSettings({
  id,
  api,
  branches = [],
  onDirty,
  onPending,
  environment = "development",
  BranchControl,
  onSaved,
  linkedProject,
}) {
  const [project, setProject] = useState(null);
  const [drafts, setDrafts] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [reload, setReload] = useState(0);
  const [confirmReload, setConfirmReload] = useState(false);
  const apiRef = useRef(api);
  const lock = useRef(false);
  apiRef.current = api;
  const dirty =
    !!project &&
    !!drafts &&
    Object.keys(names).some((name) =>
      changed(project.environments[name], drafts[name]),
    );

  useEffect(() => {
    let active = true;
    setBusy("loading");
    setError("");
    apiRef
      .current("action", { action: "registry-get", project: id })
      .then((result) => {
        if (!active) return;
        if (
          !result.project?.environments?.development ||
          !result.project?.environments?.production
        )
          throw new Error(
            "Both Development and Production records are required. The environment registry is incomplete.",
          );
        setProject(result.project);
        setDrafts(
          Object.fromEntries(
            Object.keys(names).map((name) => [
              name,
              preferences(result.project.environments[name]),
            ]),
          ),
        );
        setMessage("");
        setSubmitted(false);
      })
      .catch(
        (error) =>
          active &&
          setError(
            error.message ||
              "Environment settings could not be loaded. Retry loading.",
          ),
      )
      .finally(() => active && setBusy(""));
    return () => {
      active = false;
    };
  }, [id, reload]);
  useEffect(() => {
    if (linkedProject?.id !== id || !linkedProject.environments) return;
    setDrafts((current) =>
      Object.fromEntries(
        Object.keys(names).map((name) => [
          name,
          changed(project?.environments?.[name], current?.[name])
            ? current[name]
            : preferences(linkedProject.environments[name]),
        ]),
      ),
    );
    setProject(linkedProject);
  }, [linkedProject, id]);
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);
  useEffect(() => {
    onPending(!!busy);
    return () => onPending(false);
  }, [busy, onPending]);

  useEffect(() => {
    setSubmitted(false);
    setError("");
    setMessage("");
  }, [environment]);

  const refresh = () => {
    setConfirmReload(false);
    setReload((value) => value + 1);
  };
  if (!project || !drafts)
    return (
      <Stack spacing={2} sx={{ py: 3 }}>
        {error ? (
          <>
            <Alert severity="error">{error}</Alert>
            <Button disabled={!!busy} onClick={refresh}>
              Retry loading
            </Button>
          </>
        ) : (
          <Typography role="status">Loading environment settings…</Typography>
        )}
      </Stack>
    );
  const saved = project.environments[environment];
  const draft = drafts[environment];
  const selectedDirty = changed(saved, draft);
  const branchMissing = submitted && !draft.branch.trim();
  const options = [
    ...new Set(
      [
        ...branches.filter((branch) => typeof branch === "string"),
        saved.branch,
        draft.branch,
      ].filter(Boolean),
    ),
  ];
  const edit = (changes) => {
    setMessage("");
    setDrafts((current) => ({
      ...current,
      [environment]: { ...current[environment], ...changes },
    }));
  };
  const save = async () => {
    setSubmitted(true);
    if (!draft.branch.trim() || lock.current) return;
    lock.current = true;
    setBusy("save");
    setError("");
    setMessage("");
    const name = environment;
    try {
      const result = await api("action", {
        action: "environment-settings",
        project: id,
        environment: name,
        revision: saved.revision,
        changes: { branch: draft.branch, automatic: draft.automatic },
      });
      if (!result.environment || result.environment.name !== name)
        throw new Error(
          "The save response could not be verified. Reload saved settings before retrying.",
        );
      setProject((current) => ({
        ...current,
        environments: { ...current.environments, [name]: result.environment },
      }));
      setDrafts((current) => ({
        ...current,
        [name]: preferences(result.environment),
      }));
      setMessage(result.message || `${names[name]} preferences saved.`);
      setSubmitted(false);
      onSaved?.(result.environment);
    } catch (error) {
      setError(
        error.message || "Settings could not be saved. Your draft is retained.",
      );
    } finally {
      lock.current = false;
      setBusy("");
    }
  };
  return (
    <Stack spacing={2} sx={{ pb: "var(--space-group)", maxWidth: 900, minWidth: 0 }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between">
        <Box sx={{ display: "flex", alignItems: "center", minWidth: 0 }}>
          <Typography component="h2" variant="h6">
            {names[environment]} deployment
          </Typography>
          <Hint label="About environment settings">
            Settings apply only to the selected environment. Saving does not
            provision or start containers.
          </Hint>
        </Box>
        <Tooltip title="Reload saved environments">
          <span>
            <IconButton
              aria-label="Reload saved environments"
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
      <Stack
        spacing={2}
        id="environment-settings-panel"
        aria-label={`${names[environment]} deployment settings`}
      >
        {saved.state === "not-provisioned" && (
          <Typography color="text.secondary">
            Deployment not configured{" "}
            <Hint label="About deployment readiness">
              This environment is not provisioned. Saved preferences do not
              establish an active deployment pipeline.
            </Hint>
          </Typography>
        )}
        {BranchControl ? (
          <BranchControl
            label="Branch"
            value={draft.branch || null}
            options={options}
            disabled={!!busy}
            onChange={(_, branch) => edit({ branch: branch || "" })}
            error={branchMissing}
            helperText={
              branchMissing ? "Choose a repository branch." : undefined
            }
            onRefresh={() =>
              api("action", { action: "refresh-branches", project: id }).catch(
                (error) => setError(error.message),
              )
            }
          />
        ) : (
          <Autocomplete
            value={draft.branch || null}
            options={options}
            disabled={!!busy}
            onChange={(_, branch) => edit({ branch: branch || "" })}
            noOptionsText="No repository branches have been observed"
            renderInput={(params) => (
              <Field
                {...params}
                label="Branch"
                required
                error={branchMissing}
                helperText={
                  branchMissing ? "Choose a repository branch." : undefined
                }
              />
            )}
          />
        )}
        {!branches.length && (
          <Typography color="text.secondary">
            Branch list unavailable. Use the refresh icon in the branch field.
          </Typography>
        )}
        <Box>
          <FormControlLabel
            control={
              <Switch
                checked={draft.automatic}
                disabled={!!busy}
                onChange={(event) => edit({ automatic: event.target.checked })}
              />
            }
            label="Automatic deployment"
          />
          <Hint label="About automatic deployment">
            This saved preference takes effect only with a configured deployment
            flow. It does not enable provisioning or wake an inactive
            environment. Provision on push:{" "}
            {saved.provision_on_push ? "enabled" : "disabled"}. Wake on push:{" "}
            {saved.wake_on_push ? "enabled" : "disabled"}.
          </Hint>
        </Box>

        <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
          <Button
            variant="contained"
            disabled={!!busy || !selectedDirty}
            onClick={save}
          >
            {busy === "save" ? "Saving…" : "Save settings"}
          </Button>
          <Button
            color="inherit"
            disabled={!!busy || !selectedDirty}
            onClick={() => {
              setDrafts((current) => ({
                ...current,
                [environment]: preferences(saved),
              }));
              setSubmitted(false);
              setError("");
              setMessage("");
            }}
          >
            Discard changes
          </Button>
        </Stack>
      </Stack>

      <Dialog
        open={confirmReload}
        onClose={() => setConfirmReload(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="environment-reload-title"
      >
        <DialogTitle id="environment-reload-title">
          Discard drafts and reload environments?
        </DialogTitle>
        <DialogContent dividers>
          Unsaved changes in both Development and Production will be replaced
          with the latest saved preferences. Existing containers will not
          change.
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmReload(false)}>Keep editing</Button>
          <Button onClick={refresh}>Discard and reload</Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
