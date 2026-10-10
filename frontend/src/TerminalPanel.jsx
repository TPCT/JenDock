import React, { useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Chip, Stack, Typography } from "@mui/material";
import TerminalOutlined from "@mui/icons-material/TerminalOutlined";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

// Preserve Unicode code points across poll boundaries (for pasted commands).
export function terminalInputChunk(input, limit = 1000) {
  let end = Math.min(limit, input.length);
  const last = input.charCodeAt(end - 1);
  const next = input.charCodeAt(end);
  if (last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff)
    end -= 1;
  return input.slice(0, end);
}

// One serialized exchange loop owns the input sequence and output cursor. A failed
// exchange ends the session: input may already have reached the container.
export default function TerminalPanel({
  project,
  name,
  containerId,
  available,
  reason,
  api,
}) {
  const host = useRef(null);
  const active = useRef(null);
  const mounted = useRef(true);
  const apiRef = useRef(api);
  apiRef.current = api;
  const [status, setStatus] = useState("Disconnected");
  const [phase, setPhase] = useState("idle");
  const [error, setError] = useState("");
  const [closeWarning, setCloseWarning] = useState("");
  const closeRemote = async (session) => {
    if (!session.id || session.closeSent) return;
    session.closeSent = true;
    try {
      await apiRef.current("action", {
        action: "terminal-close",
        project: session.project,
        container: session.name,
        terminal_id: session.id,
      });
    } catch {
      if (mounted.current)
        setCloseWarning(
          "The close request could not be confirmed. The server expires disconnected sessions after two minutes.",
        );
    }
  };
  const dispose = (session) => {
    if (!session) return;
    clearTimeout(session.timer);
    session.observer?.disconnect();
    session.term?.dispose();
    if (active.current === session) active.current = null;
    void closeRemote(session);
  };
  const disconnect = (message = "Disconnected") => {
    dispose(active.current);
    if (mounted.current) {
      setPhase("idle");
      setStatus(message);
    }
  };
  useEffect(() => {
    mounted.current = true;
    const leave = () => dispose(active.current);
    window.addEventListener("pagehide", leave);
    return () => {
      mounted.current = false;
      window.removeEventListener("pagehide", leave);
      dispose(active.current);
    };
  }, [project, name, containerId]); // Target identity owns the terminal lifetime.
  useEffect(() => {
    if (!available) disconnect("Disconnected · service unavailable");
  }, [available]);
  const queue = (value) => {
    const session = active.current;
    if (!session?.id) return;
    if (session.input.length + value.length > 8000) {
      setError(
        "Input buffer is full. Wait for the pending input to be sent, then type or paste again.",
      );
      return;
    }
    session.input += value;
    session.term.focus();
  };
  const connect = async () => {
    if (!available || active.current) return;
    setError("");
    setCloseWarning("");
    setStatus("Connecting…");
    setPhase("connecting");
    const session = {
      project,
      name,
      input: "",
      cursor: 0,
      sequence: 0,
      id: null,
    };
    active.current = session;
    try {
      const term = new Terminal({
        cursorBlink: true,
        fontSize: 14,
        fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
        scrollback: 1500,
        theme: {
          background: "#171b24",
          foreground: "#edf0f7",
          cursor: "#b6a9ff",
          selectionBackground: "#45405d",
        },
      });
      session.term = term;
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(host.current);
      fit.fit();
      session.observer = new ResizeObserver(() => {
        if (active.current === session && host.current?.isConnected) fit.fit();
      });
      session.observer.observe(host.current);
      const result = await apiRef.current("action", {
        action: "terminal-open",
        project,
        container: name,
        container_id: containerId,
        request_id: crypto.randomUUID(),
        cols: term.cols,
        rows: term.rows,
      });
      session.id = result.terminal_id;
      if (active.current !== session || !mounted.current) {
        void closeRemote(session);
        return;
      }
      if (result.closed || !session.id || result.container_id !== containerId)
        throw new Error(
          "The terminal session is no longer available. Refresh the service details before reconnecting.",
        );
      term.onData(queue);
      setPhase("connected");
      setStatus("Connected · /bin/sh");
      term.focus();
      const poll = async () => {
        if (active.current !== session) return;
        const input = terminalInputChunk(session.input);
        session.input = session.input.slice(input.length);
        if (input) session.sequence += 1;
        try {
          const result = await apiRef.current("action", {
            action: "terminal-poll",
            project,
            container: name,
            terminal_id: session.id,
            cursor: session.cursor,
            sequence: session.sequence,
            input,
            cols: term.cols,
            rows: term.rows,
          });
          if (active.current !== session) return;
          if (result.truncated) term.write("\r\n[Earlier output expired]\r\n");
          term.write(
            Uint8Array.from(atob(result.output), (c) => c.charCodeAt(0)),
          );
          session.cursor = result.cursor;
          if (result.closed) {
            disconnect("Session ended");
            return;
          }
          if (result.sequence !== session.sequence)
            throw new Error(
              "The server did not acknowledge the input sequence.",
            );
          session.timer = setTimeout(poll, 180);
        } catch (failure) {
          if (active.current === session) {
            setError(`Disconnected. Input was not retried: ${failure.message}`);
            disconnect();
          }
        }
      };
      void poll();
    } catch (failure) {
      if (active.current === session) {
        setError(failure.message);
        disconnect();
      }
    }
  };
  return (
    <Box sx={{ maxWidth: 1100 }}>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        spacing={2}
        justifyContent="space-between"
        sx={{ mb: 2 }}
      >
        <Box>
          <Typography variant="h6">Container terminal</Typography>
          <Typography color="text.secondary" sx={{ mt: 1 }}>
            Interactive shell using this container’s configured user.
            Disconnected sessions expire after two minutes.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} alignItems="center">
          <Button
            variant="contained"
            startIcon={<TerminalOutlined />}
            onClick={connect}
            disabled={!available || phase !== "idle"}
          >
            Connect
          </Button>
          <Button
            variant="outlined"
            color="inherit"
            onClick={() => disconnect()}
            disabled={phase === "idle"}
          >
            Disconnect
          </Button>
        </Stack>
      </Stack>
      {!available && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {reason || "Start the container before connecting."}
        </Alert>
      )}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError("")}>
          {error}
        </Alert>
      )}
      {closeWarning && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          {closeWarning}
        </Alert>
      )}
      <Box
        sx={{
          border: "1px solid",
          borderColor: "divider",
          borderRadius: 2,
          overflow: "hidden",
          bgcolor: "#171b24",
        }}
      >
        <Stack
          direction="row"
          alignItems="center"
          spacing={1}
          sx={{ px: 2, py: 1.5, bgcolor: "#222632", color: "#edf0f7" }}
        >
          <TerminalOutlined fontSize="small" />
          <Typography noWrap sx={{ flex: 1 }}>
            {name}
          </Typography>
          <Chip
            size="small"
            label={status}
            aria-live="polite"
            sx={{ color: "inherit" }}
          />
        </Stack>
        <Box
          ref={host}
          aria-label={`Interactive terminal for ${name}`}
          sx={{
            height: { xs: 330, md: 430 },
            p: 1.5,
            "& .xterm": { height: "100%" },
          }}
        />
      </Box>
      <Stack
        direction="row"
        useFlexGap
        flexWrap="wrap"
        spacing={1}
        sx={{ mt: 2 }}
        aria-label="Terminal keys"
      >
        {[
          ["Esc", "\x1b"],
          ["Tab", "\t"],
          ["Ctrl+C", "\x03"],
          ["Ctrl+D", "\x04"],
          ["↑", "\x1b[A"],
          ["↓", "\x1b[B"],
          ["Enter", "\r"],
        ].map(([label, value]) => (
          <Button
            key={label}
            variant="outlined"
            color="inherit"
            size="small"
            disabled={phase !== "connected"}
            aria-label={
              label === "↑" ? "Arrow up" : label === "↓" ? "Arrow down" : label
            }
            onClick={() => queue(value)}
          >
            {label}
          </Button>
        ))}
      </Stack>
    </Box>
  );
}
