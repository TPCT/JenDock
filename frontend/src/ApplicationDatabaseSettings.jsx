import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from "@mui/material";
import Field from "./Field";
import { attachmentPin, sameAttachment } from "./applicationRuntime.js";
import Hint from "./Hint";
import {
  currentSavedCredential,
  mergeCredential,
  saveProvenUncommitted,
} from "./databaseConfiguration.js";
import { sameMigrationRevision } from "./migrationSettings.js";
import {
  applicationPrefix,
  applicationDraft,
  applicationValues,
  endpointKey,
  scopedApplicationBinding,
} from "./applicationDatabaseSettings.js";

export default function ApplicationDatabaseSettings({
  api,
  project,
  environment,
  serviceId,
  onDirty,
  onPending,
  disabled = false,
  onApply,
  appliedAttachment,
}) {
  const [binding, setBinding] = useState(null),
    [draft, setDraft] = useState(applicationDraft(null));
  const [endpoints, setEndpoints] = useState([]),
    [accounts, setAccounts] = useState([]);
  const [loaded, setLoaded] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [pending, setPending] = useState(null),
    [receipt, setReceipt] = useState(null),
    [dialog, setDialog] = useState(null);
  const [username, setUsername] = useState(""),
    [password, setPassword] = useState("");
  const alive = useRef(true),
    running = useRef(false),
    apiRef = useRef(api);
  apiRef.current = api;
  const dirty =
    JSON.stringify(draft) !== JSON.stringify(applicationDraft(binding));
  const blocked = busy || !!pending || !!receipt || disabled;
  const selected = accounts.find((row) => row.id === draft.credential) || null;
  const savedAccount = accounts.find(
    (row) => row.id === binding?.definition.secret_id,
  );
  const stale =
    !!binding &&
    (!savedAccount?.enabled ||
      savedAccount.revision !== binding.definition.secret_revision);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    onDirty?.(dirty || !!username || !!password || !!pending || !!receipt);
    return () => onDirty?.(false);
  }, [dirty, username, password, pending, receipt, onDirty]);
  useEffect(() => {
    onPending?.(busy || !!pending || !!receipt);
    return () => onPending?.(false);
  }, [busy, pending, receipt, onPending]);
  const call = (action, values = {}) =>
    apiRef.current("action", { action, project, environment, ...values });
  async function read(action, values) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await call(action, values);
      } catch (e) {
        if (
          attempt >= 2 ||
          !alive.current ||
          !e.message?.includes("Another operation is running")
        )
          throw e;
        await new Promise((resolve) =>
          setTimeout(resolve, 150 * (attempt + 1)),
        );
      }
    }
  }
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
      running.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function catalog(action) {
    const rows = [],
      seen = new Set();
    let cursor;
    do {
      const result = await read(action, {
        limit: 25,
        ...(cursor ? { cursor } : {}),
      });
      const page = result.bindings || result.credentials;
      if (
        !Array.isArray(page) ||
        page.some(
          (row) => row.project !== project || row.environment !== environment,
        )
      )
        throw Error("Database catalog scope changed. Reload this container.");
      rows.push(...page);
      cursor = result.next_cursor;
      if (cursor && (seen.has(cursor) || seen.size >= 100))
        throw Error("Database catalog did not finish loading. Retry loading.");
      if (cursor) seen.add(cursor);
    } while (cursor);
    return rows;
  }
  async function current() {
    const rows = (await catalog(applicationPrefix + "bindings")).filter(
      (row) => row.service_id === serviceId,
    );
    if (rows.length > 1)
      throw Error(
        "Multiple database connections were returned for this container.",
      );
    return rows.length
      ? scopedApplicationBinding(rows[0], project, environment, serviceId)
      : null;
  }
  async function load() {
    const saved = await current();
    const targets = await catalog("database-bindings");
    const users = await catalog(applicationPrefix + "credentials");
    if (!alive.current) return;
    setBinding(saved);
    setDraft(applicationDraft(saved));
    setEndpoints([
      ...new Map(
        targets
          .filter(
            (row) =>
              row.enabled &&
              row.definition?.engine === "postgresql" &&
              row.definition.ownership?.kind === "adopted-compose",
          )
          .map((row) => [endpointKey(row.definition), row]),
      ).values(),
    ]);
    setAccounts(users);
    setLoaded(true);
  }
  useEffect(() => {
    run(load);
  }, []);
  const available =
    binding &&
    !endpoints.some(
      (row) => endpointKey(row.definition) === endpointKey(binding.definition),
    )
      ? [...endpoints, { id: "saved", definition: binding.definition }]
      : endpoints;
  function edit(values) {
    setDraft((old) => ({ ...old, ...values }));
    setNotice("");
  }
  async function reconcile(saved) {
    if (saved.kind === "credential") {
      const user = currentSavedCredential(
        await read(applicationPrefix + "credential-get", {
          credential_id: saved.id,
        }),
        { id: saved.id, project, environment },
      );
      if (!alive.current) return;
      setAccounts((old) => mergeCredential(old, user));
      setDraft((old) => ({ ...old, credential: user.id }));
      setUsername("");
      setPassword("");
      setDialog(null);
    } else {
      const result = await read(applicationPrefix + "binding-get", {
        binding_id: saved.id,
      });
      const row = scopedApplicationBinding(
        result.binding,
        project,
        environment,
        serviceId,
      );
      if (row.id !== saved.id)
        throw Error(
          "Saved connection identity changed. Reload its current state.",
        );
      if (!alive.current) return;
      setBinding(row);
      setDraft(applicationDraft(row));
      setDialog(null);
    }
    setReceipt(null);
    setNotice(
      "Saved configuration loaded. Runtime application has not been verified.",
    );
  }
  async function execute(request) {
    let result;
    try {
      result = await call(applicationPrefix + request.action, request.values);
    } catch (e) {
      if (saveProvenUncommitted(e)) setPending(null);
      throw e;
    }
    const id = result?.[request.kind]?.id;
    if (!id)
      throw Error(
        "Save response was incomplete. Retry the same request to recover its receipt.",
      );
    const saved = { kind: request.kind, id };
    setPending(null);
    setReceipt(saved);
    await reconcile(saved);
  }
  async function save() {
    const latest = await current();
    if (!sameMigrationRevision(binding, latest))
      throw Error(
        "Connection settings changed. Discard and reload before saving.",
      );
    const request = {
      kind: "binding",
      action: "binding-save",
      values: {
        values: applicationValues(draft, available, accounts, serviceId),
        expected_revision: binding?.revision || 0,
        ...(binding ? { binding_id: binding.id } : {}),
        request_id: crypto.randomUUID(),
      },
    };
    setPending(request);
    await execute(request);
  }
  async function saveAccount() {
    if (!username.trim() || !password)
      throw Error("Enter the application username and password.");
    const request = {
      kind: "credential",
      action: "credential-save",
      values: {
        values: { username: username.trim(), password },
        expected_revision: dialog.account?.revision || 0,
        ...(dialog.account ? { credential_id: dialog.account.id } : {}),
        request_id: crypto.randomUUID(),
      },
    };
    setPending(request);
    await execute(request);
  }
  async function remove(kind, row) {
    const saved = { kind, id: row.id };
    setReceipt(saved);
    try {
      await call(
        applicationPrefix +
          (kind === "credential" ? "credential-revoke" : "binding-disable"),
        {
          [kind === "credential" ? "credential_id" : "binding_id"]: row.id,
          expected_revision: row.revision,
        },
      );
    } finally {
      await reconcile(saved);
    }
  }
  function closeDialog() {
    setDialog(null);
    setUsername("");
    setPassword("");
  }
  const isApplied =
    binding &&
    sameAttachment(appliedAttachment, {
      binding_id: binding.id,
      revision: binding.revision,
      digest: binding.definition_digest,
    });
  const label = (row) =>
    `${row.definition.database_name} · ${row.definition.ownership.reference}`;
  return (
    <Stack spacing={2}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <Typography variant="h6">Application database</Typography>
        <Hint label="About the application connection">
          This connection belongs to this container in {environment}. Saving
          stores connection intent. A verified runtime operation is required
          before it is applied.
        </Hint>
      </Stack>
      {error && <Alert severity="error">{error}</Alert>}
      {notice && <Alert severity="info">{notice}</Alert>}
      {pending && (
        <Alert
          severity="warning"
          action={
            <Button disabled={busy} onClick={() => run(() => execute(pending))}>
              Retry save
            </Button>
          }
        >
          The save outcome is unknown. Retry the same request before making
          another change.
        </Alert>
      )}
      {receipt && (
        <Alert
          severity="warning"
          action={
            <Button
              disabled={busy}
              onClick={() => run(() => reconcile(receipt))}
            >
              Reload saved state
            </Button>
          }
        >
          Checking the saved state. New changes are locked until it is reloaded.
        </Alert>
      )}
      {!loaded ? (
        <Button disabled={busy} onClick={() => run(load)}>
          {busy ? "Loading connection…" : "Retry loading"}
        </Button>
      ) : (
        <>
          <Typography variant="body2" color="text.secondary">
            {binding
              ? `${binding.status === "disabled" ? "Disabled" : stale ? "Account changed — save an enabled account" : "Configuration saved"} · ${isApplied ? "Applied to container" : "Runtime not verified"}`
              : "No application connection configured"}
          </Typography>
          {!available.length && (
            <Alert severity="info">
              Configure database inspection for a PostgreSQL container in this
              environment to make its endpoint available here.
            </Alert>
          )}
          <Autocomplete
            disabled={blocked || !!binding}
            options={available}
            value={
              available.find(
                (row) => endpointKey(row.definition) === draft.endpoint,
              ) || null
            }
            isOptionEqualToValue={(a, b) =>
              endpointKey(a.definition) === endpointKey(b.definition)
            }
            getOptionLabel={label}
            onChange={(_, row) =>
              edit({ endpoint: row ? endpointKey(row.definition) : "" })
            }
            renderInput={(params) => <Field {...params} label="Database" />}
          />
          <Autocomplete
            disabled={blocked}
            options={accounts.filter(
              (row) => row.enabled || row.id === draft.credential,
            )}
            value={selected}
            isOptionEqualToValue={(a, b) => a.id === b.id}
            getOptionLabel={(row) =>
              `Account ${row.id.slice(0, 8)} · revision ${row.revision}${row.enabled ? "" : " · revoked"}`
            }
            onChange={(_, row) => edit({ credential: row?.id || "" })}
            renderInput={(params) => (
              <Field {...params} label="Application account" />
            )}
          />
          <Stack direction="row" spacing={1} flexWrap="wrap">
            <Button
              disabled={blocked}
              onClick={() => setDialog({ kind: "account" })}
            >
              Add account
            </Button>
            <Button
              disabled={blocked || !selected}
              onClick={() => setDialog({ kind: "account", account: selected })}
            >
              Rotate account
            </Button>
            <Button
              color="error"
              disabled={blocked || !selected?.enabled}
              onClick={() => setDialog({ kind: "revoke", account: selected })}
            >
              Revoke account
            </Button>
          </Stack>
          <Autocomplete
            disabled={blocked}
            options={["fields", "url"]}
            value={draft.mode}
            disableClearable
            getOptionLabel={(mode) =>
              mode === "url" ? "Connection URL" : "Separate variables"
            }
            onChange={(_, mode) =>
              edit({
                mode,
                mapping:
                  mode === "url"
                    ? { url: "DATABASE_URL" }
                    : {
                        host: "DB_HOST",
                        port: "DB_PORT",
                        database: "DB_DATABASE",
                        username: "DB_USERNAME",
                        password: "DB_PASSWORD",
                      },
              })
            }
            renderInput={(params) => (
              <Field {...params} label="Connection format" />
            )}
          />
          <Stack spacing={2}>
            {(draft.mode === "url"
              ? ["url"]
              : ["host", "port", "database", "username", "password"]
            ).map((key) => (
              <Field
                key={key}
                disabled={blocked}
                label={`${key === "url" ? "Connection URL" : key[0].toUpperCase() + key.slice(1)} variable`}
                value={draft.mapping[key] || ""}
                onChange={(event) =>
                  edit({
                    mapping: { ...draft.mapping, [key]: event.target.value },
                  })
                }
              />
            ))}
          </Stack>
          <Stack direction="row" spacing={1} flexWrap="wrap">
            <Button
              variant="contained"
              disabled={blocked || (!dirty && !stale && binding?.enabled)}
              onClick={() => run(save)}
            >
              Save connection
            </Button>
            {onApply && (
              <Button
                variant="outlined"
                disabled={
                  blocked || dirty || stale || !binding?.enabled || isApplied
                }
                onClick={() =>
                  run(async () => {
                    const latest = await current();
                    if (!sameMigrationRevision(binding, latest))
                      throw Error(
                        "Connection changed. Reload before applying it.",
                      );
                    onApply(attachmentPin(latest));
                  })
                }
              >
                Apply connection
              </Button>
            )}
            <Button
              disabled={blocked || !dirty}
              onClick={() => setDialog({ kind: "discard" })}
            >
              Discard
            </Button>
            <Button
              disabled={blocked || dirty || !binding?.enabled}
              color="error"
              onClick={() => setDialog({ kind: "disable" })}
            >
              Disable connection
            </Button>
          </Stack>
        </>
      )}
      <Dialog
        open={!!dialog}
        fullWidth
        maxWidth="sm"
        onClose={() => !blocked && closeDialog()}
        aria-labelledby="application-database-dialog-title"
      >
        <DialogTitle id="application-database-dialog-title">
          {dialog?.kind === "account"
            ? dialog.account
              ? "Rotate application account"
              : "Add application account"
            : dialog?.kind === "revoke"
              ? "Revoke application account"
              : dialog?.kind === "discard"
                ? "Discard connection changes"
                : "Disable connection"}
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            {error && <Alert severity="error">{error}</Alert>}
            {dialog?.kind === "account" ? (
              <>
                <Field
                  label="Username"
                  autoComplete="off"
                  disabled={blocked}
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                />
                <Field
                  label="Password"
                  type="password"
                  autoComplete="new-password"
                  disabled={blocked}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
                <Typography variant="body2" color="text.secondary">
                  Enter an existing database account. This saves connection
                  credentials; it does not create or change the account inside
                  PostgreSQL.
                </Typography>
              </>
            ) : (
              <Typography>
                {dialog?.kind === "discard"
                  ? "Discard the unsaved connection draft and reload its saved settings?"
                  : "This blocks future use of the saved connection. It does not change PostgreSQL accounts or disconnect a running container."}
              </Typography>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          {pending && (
            <Button disabled={busy} onClick={() => run(() => execute(pending))}>
              Retry save
            </Button>
          )}
          {receipt && (
            <Button
              disabled={busy}
              onClick={() => run(() => reconcile(receipt))}
            >
              Reload saved state
            </Button>
          )}
          <Button disabled={blocked} onClick={closeDialog}>
            Cancel
          </Button>
          <Button
            variant="contained"
            disabled={blocked}
            onClick={() =>
              run(async () => {
                if (dialog.kind === "account") await saveAccount();
                else if (dialog.kind === "revoke")
                  await remove("credential", dialog.account);
                else if (dialog.kind === "disable")
                  await remove("binding", binding);
                else {
                  await load();
                  setDialog(null);
                }
              })
            }
          >
            {dialog?.kind === "account"
              ? "Save account"
              : dialog?.kind === "discard"
                ? "Discard"
                : dialog?.kind === "revoke"
                  ? "Revoke"
                  : "Disable"}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
