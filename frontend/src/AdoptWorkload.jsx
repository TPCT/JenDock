import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from "@mui/material";
import Hint from "./Hint";
import Field from "./Field";
import { selectionPayload, reviewedSelection, matchesReview, linkedEnvironment, adoptionRecoveryKey, adoptionCheckpoint, restoreAdoption } from "./adoption.js";

export default function AdoptWorkload({
  open,
  owner,
  project,
  environment,
  api,
  onClose,
  onLinked,
  onDirty,
  onPending,
}) {
  const scope = `${project}:${environment}`;
  const [recovery] = useState(() => {
    try {
      const key = adoptionRecoveryKey(owner, project, environment);
      return { key, request: restoreAdoption(sessionStorage, key, project, environment) };
    } catch {
      return { error: "The saved linking request could not be read. Resolve recovery before starting another request." };
    }
  });
  const recovered = recovery.request;
  const [storageError, setStorageError] = useState(recovery.error || "");
  const [draft, setDraft] = useState({ scope, recipe_revision: recovered?.recipe_revision ?? null, services: recovered?.services ?? null });
  const selection = draft.scope === scope ? draft : { recipe_revision: null, services: null };
  const selectionKey = JSON.stringify(selectionPayload(selection));
  const [options, setOptions] = useState(null);
  const [preview, setPreview] = useState(recovered ? {
    ...recovered, selected_recipe_revision: recovered.recipe_revision,
    selected_services: recovered.services, recovered: true,
  } : null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [uncertain, setUncertain] = useState(!!recovered);
  const [linked, setLinked] = useState(false);
  const linkedRecord = useRef(null);
  const lock = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const title = environment === "production" ? "Production" : "Development";
  useEffect(() => {
    if (!open || uncertain || linked || storageError) return;
    let active = true;
    setLoading(true);
    setPreview(null);
    setError("");
    const captured = JSON.parse(selectionKey);
    api("action", { action: "adoption-options", project, environment })
      .then((catalog) => {
        if (!active) return null;
        if (catalog.project !== project || catalog.environment !== environment || !Array.isArray(catalog.recipes))
          throw new Error("The registered recipe choices could not be verified.");
        setOptions(catalog);
        const chosen = captured.recipe_revision == null ? catalog.default : catalog.recipes.find((item) => item.recipe_revision === captured.recipe_revision);
        if (!chosen) throw new Error("Select a registered recipe to inspect its existing workload.");
        if (captured.services && !captured.services.length) throw new Error("Select at least one service to inspect.");
        return api("action", { action: "adoption-preview", project, environment, ...captured });
      })
      .then((result) => {
        if (!active || !result) return;
        if (
          !matchesReview(result, project, environment, captured) ||
          result.project !== project ||
          result.environment !== environment ||
          !/^[a-f0-9]{64}$/.test(result.fingerprint || "") ||
          !Number.isInteger(result.project_revision) ||
          !Number.isInteger(result.environment_revision) ||
          !["active", "stopped", "degraded"].includes(result.state) ||
          !Array.isArray(result.containers) ||
          !result.containers.length ||
          !Array.isArray(result.databases)
        )
          throw new Error(
            "The inspected workload could not be verified. Refresh the review before linking.",
          );
        setPreview(result);
      })
      .catch(
        (error) =>
          active &&
          setError(
            error.message || "The existing workload could not be inspected.",
          ),
      )
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [open, project, environment, api, reload, selectionKey, uncertain, linked, storageError]);
  useEffect(() => {
    onDirty(!linked && !uncertain && draft.scope === scope && (draft.recipe_revision != null || draft.services != null));
    return () => onDirty(false);
  }, [uncertain, linked, onDirty, draft, scope]);
  useEffect(() => {
    onPending(loading || saving);
    return () => onPending(false);
  }, [loading, saving, onPending]);
  const finish = async () => {
    try {
      await onLinked(linkedRecord.current);
    } catch {
      setError(
        "The workload was linked, but workspace refresh failed. Refresh the workspace to see the saved result.",
      );
    }
  };
  const save = async () => {
    if (lock.current || storageError || !preview || linked || !matchesReview(preview, project, environment, selection)) return;
    lock.current = true;
    setSaving(true);
    setError("");
    try {
      // Persist before dispatch: an interrupted response must replay this fingerprint.
      try {
        sessionStorage.setItem(recovery.key, JSON.stringify(adoptionCheckpoint(preview)));
      } catch {
        setStorageError("The linking request could not be saved for recovery. No request was sent.");
        return;
      }
      const result = await api("action", {
        action: "adoption-save",
        project: preview.project,
        environment: preview.environment,
        fingerprint: preview.fingerprint,
        ...reviewedSelection(preview),
      });
      if (
        !linkedEnvironment(result, project, environment, preview.fingerprint)
      )
        throw new Error(
          "The saved result could not be verified. Check the original request before starting another review.",
        );
      sessionStorage.removeItem(recovery.key);
      if (!alive.current) return;
      setUncertain(false);
      setDraft({ scope, recipe_revision: null, services: null });
      setLinked(true);
      linkedRecord.current = result.project;
      await finish();
    } catch (error) {
      if (!alive.current) return;
      const unknown = uncertain || !error.status || error.status >= 500;
      setUncertain(unknown);
      if (!unknown) {
        try { sessionStorage.removeItem(recovery.key); }
        catch { setStorageError("The rejected request could not be cleared from recovery storage."); }
      }
      setError(error.message || "The linking result could not be confirmed.");
    } finally {
      lock.current = false;
      if (alive.current) setSaving(false);
    }
  };
  return (
    <Dialog
      open={open}
      onClose={() => !loading && !saving && onClose()}
      fullWidth
      maxWidth="md"
      aria-labelledby="adopt-workload-title"
    >
      <DialogTitle id="adopt-workload-title">
        Link existing workload to {title}
        <Hint label="About linking a workload">
          The server rechecks the inspected container, Compose and database
          identities before saving this association. This does not start
          containers or move data.
        </Hint>
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
            {project} · {title}
          </Typography>
          {options?.project === project && options?.environment === environment && !linked && (
            <>
              <Autocomplete
                options={[...(options.default ? [options.default] : []), ...options.recipes]}
                value={(selection.recipe_revision == null ? options.default : options.recipes.find((item) => item.recipe_revision === selection.recipe_revision)) || null}
                getOptionLabel={(item) => item.recipe_revision == null ? `Default workload · ${item.compose_project}` : `Recipe ${item.recipe_revision} · ${item.compose_project}`}
                isOptionEqualToValue={(a, b) => a.recipe_revision === b.recipe_revision}
                disableClearable
                disabled={loading || saving || uncertain}
                onChange={(_, value) => { if (value) { setPreview(null); setDraft({ scope, recipe_revision: value.recipe_revision, services: null }); } }}
                renderInput={(params) => <Field {...params} label="Registered recipe" />}
              />
              <Autocomplete
                multiple
                options={(selection.recipe_revision == null ? options.default : options.recipes.find((item) => item.recipe_revision === selection.recipe_revision))?.services || []}
                value={selection.services ?? (selection.recipe_revision == null ? options.default : options.recipes.find((item) => item.recipe_revision === selection.recipe_revision))?.services ?? []}
                disabled={loading || saving || uncertain}
                onChange={(_, services) => { setPreview(null); setDraft({ scope, recipe_revision: selection.recipe_revision, services }); }}
                renderInput={(params) => <Field {...params} label="Services to link" />}
              />
              <Hint label="About service selection">Choose only the services owned by this environment. Registered recipes are managed by trusted project configuration. Linking does not create or start containers.</Hint>
              {!options.default && options.recipes.length === 0 && <Alert severity="info">No eligible registered Compose recipes are available. Register a project recipe before linking existing containers.</Alert>}
            </>
          )}
          {loading && (
            <Typography role="status">
              Inspecting existing containers and database identities…
            </Typography>
          )}
          {storageError && <Alert severity="error">{storageError}</Alert>}
          {uncertain && <Alert severity="warning">A linking request has an unconfirmed result. Check the original request to recover its exact result before changing the selection.</Alert>}
          {error && (
            <Alert severity="error" sx={{ overflowWrap: "anywhere" }}>
              {error}
            </Alert>
          )}
          {uncertain && (
            <Typography color="text.secondary">
              Check the original request using the same reviewed identity before
              refreshing this review.
            </Typography>
          )}
          {linked && (
            <Alert severity="success" role="status">
              The workload is linked to {title}.
            </Alert>
          )}
          {preview && !preview.recovered && (
            <>
              <Box>
                <Typography variant="h6" sx={{ mb: 1 }}>
                  Containers · {preview.containers.length}
                </Typography>
                <Stack
                  spacing={2}
                  divider={
                    <Box sx={{ borderTop: 1, borderColor: "divider" }} />
                  }
                >
                  {preview.containers.map((container) => (
                    <Box key={container.id} sx={{ overflowWrap: "anywhere" }}>
                      <Typography fontWeight={600}>{container.name}</Typography>
                      <Typography color="text.secondary" fontSize={13}>
                        Compose service: {container.service} ·{" "}
                        {container.running ? "Running" : "Stopped"}
                      </Typography>
                      <Typography
                        component="code"
                        display="block"
                        fontSize={12}
                      >
                        Container: {container.id}
                      </Typography>
                      <Typography
                        component="code"
                        display="block"
                        fontSize={12}
                      >
                        Image: {container.image_id}
                      </Typography>
                    </Box>
                  ))}
                </Stack>
              </Box>
              <Box>
                <Typography variant="h6" sx={{ mb: 1 }}>
                  Database identities
                </Typography>
                {preview.databases.length ? (
                  <Stack spacing={2}>
                    {preview.databases.map((database, index) => (
                      <Box
                        key={`${database.client}-${index}`}
                        sx={{ overflowWrap: "anywhere" }}
                      >
                        <Typography fontWeight={600}>
                          {database.engine} · {database.database}
                        </Typography>
                        {database.host && (
                          <Typography fontSize={13}>
                            Host: {database.host}
                          </Typography>
                        )}
                        <Typography fontSize={12}>
                          Client container: {database.client}
                        </Typography>
                        {database.container_id && (
                          <Typography fontSize={12}>
                            Database container: {database.container_id}
                          </Typography>
                        )}
                      </Box>
                    ))}
                  </Stack>
                ) : (
                  <Typography color="text.secondary">
                    No database endpoint was identified in the inspected
                    workload.
                  </Typography>
                )}
              </Box>
              <Typography color="text.secondary">
                Link existing workload · No containers or data moved.
              </Typography>
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button disabled={loading || saving} onClick={onClose}>
          Close
        </Button>
        {!linked && (
          <Button
            disabled={loading || saving || uncertain}
            onClick={() => setReload((value) => value + 1)}
          >
            Refresh review
          </Button>
        )}
        <Button
          variant="contained"
          disabled={loading || saving || !!storageError || (!linked && !preview)}
          onClick={linked ? finish : save}
        >
          {saving
            ? "Linking…"
            : linked
              ? "Refresh workspace"
              : uncertain
                ? "Check original request"
                : `Link to ${title}`}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
