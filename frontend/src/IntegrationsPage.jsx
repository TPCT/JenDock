import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import Field from "./Field";
import McpAccess from "./McpAccess";
import Hint from "./Hint";
import { Refresh } from "./icons";

const providers = { github: "GitHub", cloudflare: "Cloudflare" };
const emptyDrafts = () => ({
  github: { credential: "", account_id: "" },
  cloudflare: { credential: "", account_id: "" },
});
const timestamp = (value) =>
  value ? new Date(value * 1000).toLocaleString() : "Not tested";

export default function IntegrationsPage({
  api,
  setDirty,
  setOperationPending,
}) {
  const [mcpDirty, setMcpDirty] = useState(false);
  const [mcpPending, setMcpPending] = useState(false);
  const [connections, setConnections] = useState(null);
  const [drafts, setDrafts] = useState(emptyDrafts);
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [reload, setReload] = useState(0);
  const [confirmReload, setConfirmReload] = useState(false);
  const [disable, setDisable] = useState(null);
  const lock = useRef(false);
  const credentialInput = useRef(null);
  const accountInput = useRef(null);
  const apiRef = useRef(api);
  apiRef.current = api;
  const dirty = Object.values(drafts).some(
    (draft) => !!draft.credential || !!draft.account_id,
  );
  useEffect(() => {
    let active = true;
    setBusy("loading");
    setError("");
    apiRef
      .current("action", { action: "integrations-list" })
      .then((result) => {
        if (!active) return;
        if (!result.connections?.github || !result.connections?.cloudflare)
          throw new Error(
            "Connection status is incomplete. Reload to try again.",
          );
        setConnections(result.connections);
        setDrafts(emptyDrafts());
        setMessage("");
      })
      .catch(
        (error) =>
          active &&
          setError(error.message || "Connections could not be loaded."),
      )
      .finally(() => active && setBusy(""));
    return () => {
      active = false;
    };
  }, [reload]);
  useEffect(() => {
    setDirty(dirty || mcpDirty);
    return () => setDirty(false);
  }, [dirty, mcpDirty, setDirty]);
  useEffect(() => {
    setOperationPending(!!busy || mcpPending);
    return () => setOperationPending(false);
  }, [busy, mcpPending, setOperationPending]);
  const act = async (key, callback) => {
    if (lock.current) return;
    lock.current = true;
    setBusy(key);
    setError("");
    setMessage("");
    try {
      await callback();
    } catch (error) {
      setError(
        error.message ||
          "The request could not be completed. Reload status before retrying.",
      );
    } finally {
      lock.current = false;
      setBusy("");
    }
  };
  const accept = (provider, result) => {
    if (!result.connection || result.connection.provider !== provider)
      throw new Error(
        "The response could not be verified. Reload connection status before retrying.",
      );
    setConnections((current) => ({
      ...current,
      [provider]: result.connection,
    }));
    setMessage(result.message || "Connection updated.");
  };
  const refresh = () => {
    setConfirmReload(false);
    setReload((value) => value + 1);
  };
  const draft = selected ? drafts[selected] : null;
  const credentialError =
    submitted && (!draft?.credential || /\s/.test(draft.credential))
      ? "Enter a token without spaces or line breaks."
      : "";
  const accountError =
    submitted &&
    selected === "cloudflare" &&
    draft?.account_id &&
    !/^[a-f0-9]{32}$/.test(draft.account_id)
      ? "Use the 32-character hexadecimal account ID."
      : "";
  const save = (event) => {
    event.preventDefault();
    setSubmitted(true);
    if (!draft.credential || /\s/.test(draft.credential)) {
      credentialInput.current?.focus();
      return;
    }
    if (
      selected === "cloudflare" &&
      draft.account_id &&
      !/^[a-f0-9]{32}$/.test(draft.account_id)
    ) {
      accountInput.current?.focus();
      return;
    }
    const provider = selected;
    act(provider + "-save", async () => {
      const result = await api("action", {
        action: "integrations-save",
        provider,
        revision: connections[provider].revision,
        credential: draft.credential,
        options:
          provider === "cloudflare" && draft.account_id
            ? { account_id: draft.account_id }
            : {},
      });
      accept(provider, result);
      setDrafts((current) => ({
        ...current,
        [provider]: { credential: "", account_id: "" },
      }));
      setSelected(null);
      setSubmitted(false);
    });
  };
  if (!connections)
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
          <Typography role="status">Loading integrations…</Typography>
        )}
      </Stack>
    );
  return (
    <Stack spacing={3} sx={{ maxWidth: 1000 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Box>
          <Typography variant="h4" component="h1" sx={{ display: "inline" }}>
            Integrations
          </Typography>
          <Hint label="About integrations">
            Connections verify provider access before credentials are encrypted
            and saved. Deployment, webhook management and DNS changes require
            their own verified setup.
          </Hint>
        </Box>
        <Tooltip title="Reload connections">
          <span>
            <IconButton
              aria-label="Reload connections"
              disabled={!!busy}
              onClick={() => (dirty ? setConfirmReload(true) : refresh())}
            >
              <Refresh />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      {error && !selected && !disable && (
        <Alert severity="error" sx={{ overflowWrap: "anywhere" }}>
          {error}
        </Alert>
      )}
      {message && (
        <Alert severity="success" role="status">
          {message}
        </Alert>
      )}
      {Object.entries(providers).map(([provider, title]) => {
        const connection = connections[provider],
          metadata = connection.metadata || {};
        const hasDraft =
          !!drafts[provider].credential || !!drafts[provider].account_id;
        return (
          <Box
            key={provider}
            sx={{
              border: 1,
              borderColor: "divider",
              borderRadius: 1,
              p: "var(--space-panel)",
              minWidth: 0,
            }}
          >
            <Stack spacing={2}>
              <Stack
                direction="row"
                alignItems="center"
                justifyContent="space-between"
                gap={2}
              >
                <Typography variant="h6" component="h2">
                  {title}
                </Typography>
                <Typography color="text.secondary">
                  {!connection.configured
                    ? "Not connected"
                    : connection.enabled
                      ? "Enabled"
                      : "Disabled"}
                </Typography>
              </Stack>
              {connection.configured && (
                <Box>
                  <Typography sx={{ overflowWrap: "anywhere" }}>
                    {provider === "github"
                      ? metadata.identity || "Saved GitHub connection"
                      : metadata.token_kind === "account"
                        ? "Account token"
                        : "User token"}
                  </Typography>
                  <Typography color="text.secondary" fontSize={14}>
                    Last verified: {timestamp(connection.tested)}
                  </Typography>
                </Box>
              )}
              <Typography color="text.secondary" fontSize={14}>
                {connection.configured
                  ? provider === "github"
                    ? `${metadata.repositories?.length ?? 0}${metadata.repositories_truncated ? "+" : ""} repositories visible`
                    : `${metadata.zones?.length ?? 0}${metadata.zones_truncated ? "+" : ""} zones visible`
                  : provider === "github"
                    ? "Connect to verify repository access."
                    : "Connect to verify zone access."}
                <Hint label={`${title} access details`}>
                  {provider === "github"
                    ? "Verification checks user identity and repository metadata. Clone access and webhook management are not verified."
                    : "Verification checks token activity and zone read access. DNS write permissions, origin routing and TLS are not verified."}
                </Hint>
              </Typography>
              {hasDraft && (
                <Typography role="status" color="text.secondary" fontSize={14}>
                  Unsaved credential draft
                </Typography>
              )}
              <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
                <Button
                  variant={connection.configured ? "outlined" : "contained"}
                  disabled={!!busy}
                  onClick={() => {
                    setSelected(provider);
                    setError("");
                    setSubmitted(false);
                  }}
                >
                  {hasDraft
                    ? "Continue setup"
                    : connection.configured
                      ? "Replace credential"
                      : "Connect"}
                </Button>
                {connection.configured && (
                  <Button
                    disabled={!!busy}
                    onClick={() =>
                      act(provider + "-test", async () =>
                        accept(
                          provider,
                          await api("action", {
                            action: "integrations-test",
                            provider,
                          }),
                        ),
                      )
                    }
                  >
                    {busy === provider + "-test"
                      ? "Testing…"
                      : "Test connection"}
                  </Button>
                )}
                {connection.enabled && (
                  <Button
                    color="inherit"
                    disabled={!!busy}
                    onClick={() => {
                      setDisable(provider);
                      setError("");
                    }}
                  >
                    Disable
                  </Button>
                )}
              </Stack>
            </Stack>
          </Box>
        );
      })}
      <McpAccess api={api} onDirty={setMcpDirty} onPending={setMcpPending} />
      <Dialog
        open={!!selected}
        onClose={() => !busy && setSelected(null)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="integration-editor-title"
        slotProps={{
          paper: { component: "form", noValidate: true, onSubmit: save },
        }}
      >
        <DialogTitle id="integration-editor-title">
          {selected && connections[selected].configured ? "Replace" : "Connect"}{" "}
          {providers[selected]}{" "}
          <Hint label="About credentials">
            Enter a new token. Saved tokens are never displayed. Verification
            failure preserves the previous connection. Tokens are encrypted with
            a key stored separately from the database.
          </Hint>
        </DialogTitle>
        <DialogContent dividers>
          {draft && (
            <Stack spacing={2}>
              {error && (
                <Alert severity="error" sx={{ overflowWrap: "anywhere" }}>
                  {error}
                </Alert>
              )}
              <Field
                fullWidth
                required
                autoFocus
                label={
                  selected === "github" ? "Personal access token" : "API token"
                }
                type="password"
                autoComplete="new-password"
                value={draft.credential}
                inputRef={credentialInput}
                disabled={!!busy}
                error={!!credentialError}
                helperText={credentialError || undefined}
                inputProps={{
                  maxLength: 8192,
                  autoCapitalize: "none",
                  spellCheck: false,
                }}
                onChange={(event) =>
                  setDrafts((current) => ({
                    ...current,
                    [selected]: {
                      ...current[selected],
                      credential: event.target.value,
                    },
                  }))
                }
              />
              {selected === "cloudflare" && (
                <Field
                  fullWidth
                  label="Account ID (optional)"
                  value={draft.account_id}
                  inputRef={accountInput}
                  disabled={!!busy}
                  error={!!accountError}
                  helperText={
                    accountError ||
                    "For an account-owned token if automatic detection fails."
                  }
                  inputProps={{
                    maxLength: 32,
                    autoCapitalize: "none",
                    spellCheck: false,
                  }}
                  onChange={(event) =>
                    setDrafts((current) => ({
                      ...current,
                      [selected]: {
                        ...current[selected],
                        account_id: event.target.value,
                      },
                    }))
                  }
                />
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button disabled={!!busy} onClick={() => setSelected(null)}>
            Close
          </Button>
          {selected && (draft.credential || draft.account_id) && (
            <Button
              color="inherit"
              disabled={!!busy}
              onClick={() => {
                setDrafts((current) => ({
                  ...current,
                  [selected]: { credential: "", account_id: "" },
                }));
                setSubmitted(false);
                setError("");
                setSelected(null);
              }}
            >
              Discard draft
            </Button>
          )}
          <Button variant="contained" type="submit" disabled={!!busy}>
            {busy.endsWith("-save") ? "Verifying…" : "Verify and save"}
          </Button>
        </DialogActions>
      </Dialog>
      <Dialog
        open={!!disable}
        onClose={() => !busy && setDisable(null)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="integration-disable-title"
      >
        <DialogTitle id="integration-disable-title">
          Disable {providers[disable]}?
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            <Typography>
              This disables the saved connection locally. The provider token
              remains valid.
            </Typography>
            {error && <Alert severity="error">{error}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button disabled={!!busy} onClick={() => setDisable(null)}>
            Cancel
          </Button>
          <Button
            disabled={!!busy}
            variant="contained"
            onClick={() =>
              act(disable + "-disable", async () => {
                accept(
                  disable,
                  await api("action", {
                    action: "integrations-disable",
                    provider: disable,
                    revision: connections[disable].revision,
                  }),
                );
                setDisable(null);
              })
            }
          >
            {busy.endsWith("-disable") ? "Disabling…" : "Disable connection"}
          </Button>
        </DialogActions>
      </Dialog>
      <Dialog
        open={confirmReload}
        onClose={() => setConfirmReload(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="integration-reload-title"
      >
        <DialogTitle id="integration-reload-title">
          Discard drafts and reload?
        </DialogTitle>
        <DialogContent dividers>
          Unsaved credentials will be cleared. Saved connections will not
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
