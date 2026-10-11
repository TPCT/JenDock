import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Chip,
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
import Hint from "./Hint";
import { Refresh } from "./icons";
import {
  currentSavedCredential,
  mergeCredential,
  saveProvenUncommitted,
} from "./databaseConfiguration.js";
import {
  migrationPrefix,
  scopedMigrationBinding,
  endpointKey,
  migrationDraft,
  migrationValues,
  sameMigrationRevision,
} from "./migrationSettings.js";

export default function MigrationSettings({
  api,
  project,
  environment,
  onDirty,
  onPending,
}) {
  const [binding, setBinding] = useState(null),
    [draft, setDraft] = useState(migrationDraft(null));
  const [endpoints, setEndpoints] = useState([]),
    [credentials, setCredentials] = useState([]);
  const [loaded, setLoaded] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [pending, setPending] = useState(null),
    [reloadOnly, setReloadOnly] = useState(null);
  const [dialog, setDialog] = useState(null),
    [username, setUsername] = useState(""),
    [password, setPassword] = useState("");
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const apiRef = useRef(api),
    alive = useRef(true),
    running = useRef(false);
  apiRef.current = api;
  const dirty =
    JSON.stringify(draft) !== JSON.stringify(migrationDraft(binding));
  const blocked = busy || !!pending || !!reloadOnly;
  const selectedCredential = credentials.find(
    (value) => value.id === draft.credential,
  );
  const savedAccount = credentials.find(
    (value) => value.id === binding?.definition.secret_id,
  );
  const accountStale =
    !!binding &&
    loaded &&
    (!savedAccount?.enabled ||
      savedAccount.revision !== binding.definition.secret_revision);
  const call = (action, values = {}) =>
    apiRef.current("action", { action, project, environment, ...values });
  const migration = (action, values) => call(migrationPrefix + action, values);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    onDirty?.(dirty || !!pending || !!reloadOnly || !!username || !!password);
    return () => onDirty?.(false);
  }, [dirty, pending, reloadOnly, username, password, onDirty]);
  useEffect(() => {
    onPending?.(busy || !!pending || !!reloadOnly);
    return () => onPending?.(false);
  }, [busy, pending, reloadOnly, onPending]);
  async function run(work) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (e) {
      if (alive.current)
        setError(e.message || "Request failed. Your draft is retained.");
    } finally {
      if (alive.current) {
        running.current = false;
        setBusy(false);
      }
    }
  }
  async function read(action, values) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await call(action, values);
      } catch (error) {
        if (
          attempt >= 2 ||
          !alive.current ||
          !error.message?.includes("Another operation is running")
        )
          throw error;
        await new Promise((resolve) =>
          setTimeout(resolve, 150 * (attempt + 1)),
        );
      }
    }
  }
  async function pages(action) {
    let cursor,
      values = [];
    const seen = new Set();
    do {
      const response = await read(action, {
        limit: 25,
        ...(cursor ? { cursor } : {}),
      });
      const rows = response.credentials || response.bindings;
      if (
        !Array.isArray(rows) ||
        rows.some((v) => v.project !== project || v.environment !== environment)
      )
        throw new Error(
          "Database catalog scope changed. Reload this environment.",
        );
      values.push(...rows);
      cursor = response.next_cursor;
      if (cursor && (seen.has(cursor) || seen.size >= 100))
        throw new Error(
          "Database catalog could not finish loading. Retry loading.",
        );
      if (cursor) seen.add(cursor);
    } while (cursor);
    return values;
  }
  async function current() {
    return scopedMigrationBinding(
      (await read(migrationPrefix + "binding-current")).binding,
      project,
      environment,
    );
  }
  async function credential(id) {
    return currentSavedCredential(
      await read(migrationPrefix + "credential-get", { credential_id: id }),
      { id, project, environment },
    );
  }
  async function load() {
    // Database control has one exclusive worker. Keep its reads sequential.
    const saved = await current();
    const endpointRows = await pages("database-bindings");
    const accountRows = await pages(migrationPrefix + "credentials");
    if (!alive.current) return;
    setBinding(saved);
    setDraft(migrationDraft(saved));
    setEndpoints([
      ...new Map(
        endpointRows
          .filter((v) => v.enabled && v.definition?.engine === "postgresql")
          .map((value) => [endpointKey(value.definition), value]),
      ).values(),
    ]);
    setCredentials(accountRows);
    setLoaded(true);
    setConfirmDiscard(false);
  }
  useEffect(() => {
    run(load);
  }, []);
  function edit(values) {
    setDraft((old) => ({ ...old, ...values }));
    setNotice("");
  }
  // After a confirmed save, only read current state. Never replay a mutation merely
  // because the metadata reload failed.
  async function reconcile(receipt) {
    if (receipt.kind === "credential") {
      const account = await credential(receipt.id);
      if (!alive.current) return;
      setCredentials((old) => mergeCredential(old, account));
      setDraft((old) => ({ ...old, credential: account.id }));
      setDialog(null);
      setUsername("");
      setPassword("");
    } else {
      const saved = await current();
      if (!saved || saved.id !== receipt.id)
        throw new Error(
          "Saved configuration is unavailable. Reload its current state.",
        );
      if (!alive.current) return;
      setBinding(saved);
      setDraft(migrationDraft(saved));
    }
    setReloadOnly(null);
    setNotice("Current saved state loaded.");
  }
  async function executeSave(request) {
    let response;
    try {
      response = await migration(request.action, request.values);
    } catch (e) {
      if (saveProvenUncommitted(e)) setPending(null);
      throw e;
    }
    const id = response?.[request.kind]?.id;
    if (!id)
      throw new Error(
        "Save response was incomplete. Retry the same request to recover its receipt.",
      );
    const receipt = { kind: request.kind, id };
    if (!alive.current) return;
    setPending(null);
    setReloadOnly(receipt);
    await reconcile(receipt);
  }
  async function saveBinding() {
    const latest = await current();
    if (!sameMigrationRevision(binding, latest))
      throw new Error(
        "Migration configuration changed. Discard and reload before saving.",
      );
    const values = migrationValues(draft, availableEndpoints, credentials);
    const request = {
      action: "binding-save",
      kind: "binding",
      values: {
        values,
        expected_revision: binding?.revision || 0,
        ...(binding ? { binding_id: binding.id } : {}),
        request_id: crypto.randomUUID(),
      },
    };
    setPending(request);
    await executeSave(request);
  }
  async function saveCredential() {
    if (!username.trim() || !password)
      throw new Error("Enter the migration username and password.");
    const request = {
      action: "credential-save",
      kind: "credential",
      values: {
        values: { username: username.trim(), password },
        expected_revision: dialog?.credential?.revision || 0,
        ...(dialog?.credential ? { credential_id: dialog.credential.id } : {}),
        request_id: crypto.randomUUID(),
      },
    };
    setPending(request);
    await executeSave(request);
  }
  async function revoke() {
    const receipt = { kind: "credential", id: dialog.credential.id };
    // Revoke has no replay receipt. Re-read it after any outcome before allowing
    // another mutation; a failed response must not trigger a blind second revoke.
    setReloadOnly(receipt);
    try {
      await migration("credential-revoke", {
        credential_id: receipt.id,
        expected_revision: dialog.credential.revision,
      });
    } finally {
      await reconcile(receipt);
    }
  }
  async function disable() {
    const receipt = { kind: "binding", id: binding.id };
    setReloadOnly(receipt);
    try {
      await migration("binding-disable", {
        binding_id: binding.id,
        expected_revision: binding.revision,
      });
    } finally {
      await reconcile(receipt);
    }
  }
  const availableEndpoints =
    binding &&
    !endpoints.some(
      (v) => endpointKey(v.definition) === endpointKey(binding.definition),
    )
      ? [...endpoints, { id: "saved", definition: binding.definition }]
      : endpoints;
  const selectedEndpoint =
    availableEndpoints.find(
      (v) => endpointKey(v.definition) === draft.endpoint,
    ) || null;
  const endpointLabel = (value) =>
    `${value.definition.database_name} · ${value.definition.ownership.reference}`;
  return (
    <Stack
      spacing={2}
      sx={{
        borderTop: "1px solid",
        borderColor: "divider",
        pt: "var(--space-group)",
        mt: "var(--space-group)",
      }}
    >
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        gap={1}
      >
        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <Typography component="h2" variant="h6">
            {environment === "production" ? "Production" : "Development"}{" "}
            migrations
          </Typography>
          <Hint label="About automatic migrations">
            Pending supported migrations run during deployment. This
            configuration does not run migrations now. Inspection and migration
            accounts are separate.
          </Hint>
        </Box>
        <Tooltip title="Reload migration settings">
          <span>
            <IconButton
              aria-label="Reload migration settings"
              disabled={blocked}
              onClick={() => (dirty ? setConfirmDiscard(true) : run(load))}
            >
              <Refresh />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}
      {notice && (
        <Alert severity="success" role="status">
          {notice}
        </Alert>
      )}
      {pending && (
        <Alert
          severity="warning"
          action={
            <Button
              disabled={busy}
              onClick={() => run(() => executeSave(pending))}
            >
              Retry same save
            </Button>
          }
        >
          Save outcome needs reconciliation. Your request is retained.
        </Alert>
      )}
      {reloadOnly && (
        <Alert
          severity="warning"
          action={
            <Button
              disabled={busy}
              onClick={() => run(() => reconcile(reloadOnly))}
            >
              Reload saved state
            </Button>
          }
        >
          Reload current state before making another change.
        </Alert>
      )}
      {!loaded ? (
        <>
          <Typography role="status">
            {busy
              ? "Loading migration settings…"
              : "Migration settings could not be loaded."}
          </Typography>
          {!busy && <Button onClick={() => run(load)}>Retry loading</Button>}
        </>
      ) : (
        <>
          <Box>
            <Chip
              size="small"
              label={
                accountStale && binding?.enabled
                  ? "Account needs updating"
                  : binding?.status === "disabled"
                    ? "Disabled"
                    : binding
                      ? "Configured"
                      : "Not configured"
              }
            />
            <Hint label="About migration status">
              Configured means saved. Database identity and pending migrations
              are verified when the pipeline runs.
            </Hint>
          </Box>
          <Autocomplete
            options={availableEndpoints}
            value={selectedEndpoint}
            disabled={blocked}
            getOptionLabel={endpointLabel}
            isOptionEqualToValue={(a, b) =>
              endpointKey(a.definition) === endpointKey(b.definition)
            }
            onChange={(_, value) =>
              edit({ endpoint: endpointKey(value?.definition) })
            }
            noOptionsText="No PostgreSQL database binding in this environment"
            renderInput={(params) => <Field {...params} label="Database" />}
          />
          {!availableEndpoints.length && (
            <Typography color="text.secondary" fontSize={13}>
              Configure a PostgreSQL database binding from its container’s
              Database view first.
            </Typography>
          )}
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
            <Autocomplete
              sx={{ flex: 1 }}
              options={["laravel", "alembic"]}
              value={draft.framework}
              disabled={blocked}
              getOptionLabel={(v) => (v === "laravel" ? "Laravel" : "Alembic")}
              onChange={(_, framework) => {
                if (framework)
                  edit({
                    framework,
                    paths:
                      draft.paths === "database/migrations" ||
                      draft.paths === "alembic/versions"
                        ? framework === "laravel"
                          ? "database/migrations"
                          : "alembic/versions"
                        : draft.paths,
                  });
              }}
              renderInput={(params) => <Field {...params} label="Framework" />}
            />
            <Autocomplete
              sx={{ flex: 1 }}
              options={credentials}
              value={selectedCredential || null}
              disabled={blocked}
              getOptionLabel={(v) =>
                `Account ${v.id.slice(0, 8)} · v${v.revision}${v.enabled ? "" : " · revoked"}`
              }
              isOptionEqualToValue={(a, b) => a.id === b.id}
              getOptionDisabled={(v) => !v.enabled}
              onChange={(_, value) => edit({ credential: value?.id || "" })}
              renderInput={(params) => (
                <Field {...params} label="Migration account" />
              )}
            />
          </Stack>
          <Stack direction="row" flexWrap="wrap" gap={1}>
            <Button
              disabled={blocked}
              onClick={() => setDialog({ type: "account" })}
            >
              Add account
            </Button>
            {selectedCredential && (
              <>
                <Button
                  disabled={blocked}
                  onClick={() =>
                    setDialog({
                      type: "account",
                      credential: selectedCredential,
                    })
                  }
                >
                  Rotate account
                </Button>
                <Button
                  color="error"
                  disabled={blocked || !selectedCredential.enabled}
                  onClick={() =>
                    setDialog({
                      type: "revoke",
                      credential: selectedCredential,
                    })
                  }
                >
                  Revoke account
                </Button>
              </>
            )}
          </Stack>
          <Field
            label="Migration directories"
            value={draft.paths}
            multiline
            minRows={2}
            disabled={blocked}
            onChange={(e) => edit({ paths: e.target.value })}
            helperText="Repository paths, one per line."
          />
          <Stack direction={{ xs: "column", sm: "row" }} spacing={1}>
            <Button
              variant="contained"
              disabled={
                blocked || (!dirty && binding?.enabled && !accountStale)
              }
              onClick={() => run(saveBinding)}
            >
              {binding?.status === "disabled"
                ? "Enable migrations"
                : "Save migrations"}
            </Button>
            <Button
              color="inherit"
              disabled={blocked || !dirty}
              onClick={() => {
                setDraft(migrationDraft(binding));
                setError("");
              }}
            >
              Discard changes
            </Button>
            {binding?.enabled && (
              <Button
                color="inherit"
                disabled={blocked || dirty}
                onClick={() => run(disable)}
              >
                Disable migrations
              </Button>
            )}
          </Stack>
        </>
      )}
      <Dialog
        open={!!dialog}
        onClose={() => {
          if (!blocked) {
            setDialog(null);
            setUsername("");
            setPassword("");
          }
        }}
        fullWidth
        maxWidth="sm"
        aria-labelledby="migration-account-title"
      >
        <DialogTitle id="migration-account-title">
          {dialog?.type === "revoke"
            ? "Revoke migration account"
            : dialog?.credential
              ? "Rotate migration account"
              : "Add migration account"}
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            {error && <Alert severity="error">{error}</Alert>}
            {dialog?.type === "revoke" ? (
              <Typography>
                Deployments using this account will need an updated migration
                account. The database login itself is not deleted.
              </Typography>
            ) : (
              <>
                <Field
                  label="Username"
                  value={username}
                  disabled={blocked}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="off"
                />
                <Field
                  label="Password"
                  type="password"
                  value={password}
                  disabled={blocked}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password"
                />
                <Typography color="text.secondary" fontSize={13}>
                  Use the existing database migration login. Saving does not
                  create a database user.
                </Typography>
              </>
            )}
            {pending && (
              <Button
                disabled={busy}
                onClick={() => run(() => executeSave(pending))}
              >
                Retry same save
              </Button>
            )}
            {reloadOnly && (
              <Button
                disabled={busy}
                onClick={() => run(() => reconcile(reloadOnly))}
              >
                Reload saved state
              </Button>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button
            disabled={blocked}
            onClick={() => {
              setDialog(null);
              setUsername("");
              setPassword("");
            }}
          >
            Cancel
          </Button>
          <Button
            variant="contained"
            color={dialog?.type === "revoke" ? "error" : "primary"}
            disabled={blocked}
            onClick={() =>
              run(dialog?.type === "revoke" ? revoke : saveCredential)
            }
          >
            {dialog?.type === "revoke" ? "Revoke account" : "Save account"}
          </Button>
        </DialogActions>
      </Dialog>
      <Dialog
        open={confirmDiscard}
        onClose={() => setConfirmDiscard(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="migration-discard-title"
      >
        <DialogTitle id="migration-discard-title">
          Discard migration changes?
        </DialogTitle>
        <DialogContent dividers>
          Reload the saved configuration for this environment.
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDiscard(false)}>Keep editing</Button>
          <Button onClick={() => run(load)}>Discard and reload</Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
