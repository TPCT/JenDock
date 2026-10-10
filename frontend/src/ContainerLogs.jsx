import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  FormControlLabel,
  Stack,
  Switch,
  Typography,
} from "@mui/material";
import { Refresh } from "./icons";
import { createLogPolling } from "./logPolling";

export default function ContainerLogs({
  api,
  project,
  name,
  containerId,
  enabled,
}) {
  const [live, setLive] = useState(true);
  const polling = useRef(null);
  const liveRef = useRef(live);
  liveRef.current = live;
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setSnapshot(null);
    setError("");
  }, [project, name, containerId]);
  useEffect(() => {
    setLoading(false);
    if (!enabled || !containerId) return;
    const controller = createLogPolling({
      live: liveRef.current,
      fetchLogs: async () => {
        const result = await api("action", {
          action: "container-logs",
          project,
          container: name,
          container_id: containerId,
          tail: 200,
        });
        if (
          result.container_id !== containerId ||
          typeof result.text !== "string" ||
          !Number.isFinite(result.observed) ||
          !Number.isInteger(result.tail)
        )
          throw Error(
            "Log response does not match this container. Reload its details.",
          );
        return result;
      },
      onResult: (result) => {
        setSnapshot(result);
        setError("");
      },
      onError: (failure) =>
        setError(
          failure.message || "Logs could not be loaded. Retry to reconnect.",
        ),
      onLoading: setLoading,
    });
    polling.current = controller;
    return () => {
      controller.stop();
      polling.current = null;
    };
  }, [api, project, name, containerId, enabled]);
  useEffect(() => {
    polling.current?.setLive(live);
  }, [live]);
  return (
    <Stack spacing={2} sx={{ pt: 2 }}>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        flexWrap="wrap"
        gap={1}
      >
        <Typography variant="h6">Container logs</Typography>
        <Stack direction="row" alignItems="center" gap={1}>
          <FormControlLabel
            control={
              <Switch
                checked={live}
                onChange={(event) => setLive(event.target.checked)}
                disabled={!enabled || !containerId}
              />
            }
            label="Live"
          />
          <Button
            startIcon={<Refresh />}
            disabled={!enabled || !containerId || loading}
            onClick={() => polling.current?.reload()}
          >
            Reload
          </Button>
        </Stack>
      </Stack>
      {(!enabled || !containerId) && (
        <Alert severity="warning">
          Live updates are unavailable. Reload the container details to verify
          its identity.
          {snapshot ? " Displaying the last received snapshot." : ""}
        </Alert>
      )}
      {error && (
        <Alert severity="error">
          {error}
          {snapshot ? " Displaying the last received snapshot." : ""}
        </Alert>
      )}
      {loading && !snapshot && (
        <Typography role="status">Loading logs…</Typography>
      )}
      {snapshot && (
        <>
          <Typography color="text.secondary" fontSize={12}>
            {!enabled || !containerId
              ? "Updates unavailable"
              : live
                ? "Updates every 5 seconds"
                : "Live updates paused"}{" "}
            · Last received{" "}
            {new Date(snapshot.observed * 1000).toLocaleTimeString()} · Latest{" "}
            {snapshot.tail} lines
          </Typography>
          {snapshot.truncated && (
            <Alert severity="info">
              The log response exceeded 128 KiB. This view contains a bounded
              excerpt.
            </Alert>
          )}
          <Box
            component="pre"
            role="region"
            tabIndex={0}
            aria-label="Container log output"
            sx={{
              m: 0,
              p: 2,
              border: 1,
              borderColor: "divider",
              borderRadius: 1,
              bgcolor: "action.hover",
              maxHeight: 480,
              overflow: "auto",
              whiteSpace: "pre-wrap",
              overflowWrap: "anywhere",
              fontSize: 12,
              fontFamily: "ui-monospace, monospace",
            }}
          >
            {snapshot.text || (snapshot.truncated ? "Log lines exceeded the response limit. No complete line fits this excerpt." : "No log output recorded.")}
          </Box>
        </>
      )}
    </Stack>
  );
}
