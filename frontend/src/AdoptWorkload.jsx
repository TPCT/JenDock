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
import Hint from "./Hint";

export default function AdoptWorkload({
  open,
  project,
  environment,
  api,
  onClose,
  onLinked,
  onDirty,
  onPending,
}) {
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [uncertain, setUncertain] = useState(false);
  const [linked, setLinked] = useState(false);
  const linkedRecord = useRef(null);
  const lock = useRef(false);
  const title = environment === "production" ? "Production" : "Development";
  useEffect(() => {
    if (!open || uncertain || linked) return;
    let active = true;
    setLoading(true);
    setPreview(null);
    setError("");
    api("action", { action: "adoption-preview", project, environment })
      .then((result) => {
        if (!active) return;
        if (
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
  }, [open, project, environment, api, reload]);
  useEffect(() => {
    onDirty(uncertain);
    return () => onDirty(false);
  }, [uncertain, onDirty]);
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
    if (lock.current || !preview || linked) return;
    lock.current = true;
    setSaving(true);
    setError("");
    try {
      const result = await api("action", {
        action: "adoption-save",
        project: preview.project,
        environment: preview.environment,
        fingerprint: preview.fingerprint,
      });
      if (
        result.project?.id !== project ||
        result.project.legacy_binding !== environment
      )
        throw new Error(
          "The saved result could not be verified. Check the original request before starting another review.",
        );
      setUncertain(false);
      setLinked(true);
      linkedRecord.current = result.project;
      await finish();
    } catch (error) {
      setUncertain(!error.status || error.status >= 500);
      setError(error.message || "The linking result could not be confirmed.");
    } finally {
      lock.current = false;
      setSaving(false);
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
        <Stack spacing={3}>
          <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
            {project} · {title}
          </Typography>
          {loading && (
            <Typography role="status">
              Inspecting existing containers and database identities…
            </Typography>
          )}
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
          {preview && (
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
          disabled={loading || saving || (!linked && !preview)}
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
