import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, MenuItem, Stack, Typography } from "@mui/material";
import Field from "./Field";
import Hint from "./Hint";
import {
  databaseRowPolicy,
  currentSavedBinding,
  currentSavedCredential,
  mergeCredential,
  bindingDraftConflict,
  bindingDraftRevisions,
  assertSaveScope,
  saveProvenUncommitted,
} from "./databaseConfiguration.js";

export default function DatabaseConfiguration({
  api,
  project,
  environment,
  serviceReference,
  containerId,
  binding,
  onChanged,
  onPending,
  onDirty,
}) {
  const [credentials, setCredentials] = useState([]),
    [cursor, setCursor] = useState(null),
    [loaded, setLoaded] = useState(false),
    [candidate, setCandidate] = useState(null);
  const [draftBase, setDraftBase] = useState(binding || null);
  const draftConflict = bindingDraftConflict(draftBase, binding || null);
  const [savedReload, setSavedReload] = useState(null);
  const [edited, setEdited] = useState(false);
  const [selected, setSelected] = useState(""),
    [username, setUsername] = useState(""),
    [password, setPassword] = useState("");
  const [schemas, setSchemas] = useState(
      binding?.definition.allowed_schemas.join("\n") || "public",
    ),
    [policies, setPolicies] = useState(() =>
      Object.entries(binding?.definition.row_columns || {}).flatMap(
        ([schema, tables]) =>
          Object.entries(tables).map(([table, columns]) => ({
            schema,
            table,
            columns: columns.join(", "),
          })),
      ),
    );
  const [secret, setSecret] = useState(binding?.definition.secret_id || ""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [pending, setPending] = useState(null);
  const generation = useRef(0),
    running = useRef(false),
    apiRef = useRef(api);
  apiRef.current = api;
  const call = (action, values = {}) =>
    apiRef.current("action", { action, project, environment, ...values });
  useEffect(() => {
    onPending?.(busy);
    return () => onPending?.(false);
  }, [busy, onPending]);
  useEffect(() => {
    onDirty?.(
      Boolean(pending || savedReload || username || password || edited),
    );
    return () => onDirty?.(false);
  }, [pending, savedReload, username, password, edited, onDirty]);
  async function run(work) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const current = generation.current;
    const valid = () => current === generation.current;
    try {
      await work(valid);
    } catch (e) {
      if (valid())
        setError(
          e?.message ||
            "Request failed. Reload current state before changing this configuration.",
        );
    } finally {
      if (valid()) {
        running.current = false;
        setBusy(false);
      }
    }
  }
  async function reloadCredentials(valid, next = null) {
    const result = await call("database-credentials", {
      limit: 25,
      ...(next ? { cursor: next } : {}),
    });
    if (valid()) {
      setCredentials((old) =>
        next
          ? result.credentials.reduce(
              (items, item) => mergeCredential(items, item),
              old,
            )
          : result.credentials,
      );
      setCursor(result.next_cursor || null);
      setLoaded(true);
    }
  }
  function reload() {
    setCandidate(null);
    setLoaded(false);
    run(async (valid) => {
      await reloadCredentials(valid);
      let currentBinding = null;
      if (binding) {
        const response = await call("database-binding-get", {
          binding_id: binding.id,
        });
        if (!valid()) return;
        currentBinding = currentSavedBinding(response, {
          id: binding.id,
          project,
          environment,
          serviceReference,
        });
      }
      const result = await call("database-options", {
        reference: serviceReference,
      });
      if (!valid()) return;
      const c = result.candidate;
      if (
        c?.project !== project ||
        c?.environment !== environment ||
        c?.reference !== serviceReference ||
        c?.container_id !== containerId ||
        c?.engine !== "postgresql"
      )
        throw new Error(
          "Registered PostgreSQL ownership does not match this container. Reload its details.",
        );
      setCandidate(c);
      // Publishing the current binding refreshes the parent's catalog. Do it
      // after this sequence so both readers do not contend for the worker lock.
      if (currentBinding)
        onChanged?.({ binding: currentBinding, keepConfigurationOpen: true });
    });
  }
  useEffect(() => {
    generation.current++;
    reload();
    return () => {
      generation.current++;
      running.current = false;
    };
  }, [project, environment, serviceReference, containerId]);
  const currentCredential = credentials.find((c) => c.id === selected);
  async function executePending(request, valid) {
    assertSaveScope(request.scope, {
      project,
      environment,
      serviceReference,
      containerId,
    });
    let result;
    try {
      result = await call(request.action, request.values);
    } catch (e) {
      if (valid() && saveProvenUncommitted(e)) {
        setPending(null);
        setPassword("");
      }
      throw e;
    }
    if (!valid()) return;
    setPending(null);
    setPassword("");
    setUsername("");
    const saved = { action: request.action, result };
    setSavedReload(saved);
    setNotice(
      "Save confirmed. Current state must be reloaded before continuing.",
    );
    await reloadSaved(saved, valid);
  }
  async function reloadSaved(saved, valid) {
    let current = saved.result;
    if (saved.action === "database-binding-save") {
      const response = await call("database-binding-get", {
        binding_id: saved.result.binding.id,
      });
      if (!valid()) return;
      current = {
        ...saved.result,
        binding: currentSavedBinding(response, {
          id: saved.result.binding.id,
          project,
          environment,
          serviceReference,
        }),
      };
    }
    if (saved.action !== "database-binding-save") {
      const response = await call("database-credential-get", {
        credential_id: saved.result.credential.id,
      });
      if (!valid()) return;
      const credential = currentSavedCredential(response, {
        id: saved.result.credential.id,
        project,
        environment,
      });
      setCredentials((old) => mergeCredential(old, credential));
      setSelected(credential.id);
      if (credential.enabled) {
        // Select a newly saved credential unless doing so would replace a
        // different credential deliberately chosen in an unsaved binding draft.
        if (!edited || !secret || secret === credential.id) {
          setSecret(credential.id);
          setEdited(true);
        }
      } else if (secret === credential.id) {
        setSecret("");
        setEdited(true);
      }
      current = { ...current, credential, keepConfigurationOpen: true };
      if (binding) {
        const refreshed = await call("database-binding-get", {
          binding_id: binding.id,
        });
        if (!valid()) return;
        current.binding = currentSavedBinding(refreshed, {
          id: binding.id,
          project,
          environment,
          serviceReference,
        });
      }
    }
    if (!valid()) return;
    setSavedReload(null);
    if (saved.action === "database-binding-save") setEdited(false);
    setNotice(
      current.credential && !current.credential.enabled
        ? "Credential is revoked. Choose an enabled credential and save a new binding revision before verification."
        : "Save confirmed and current metadata reloaded. Database identity still requires verification.",
    );
    onChanged?.(current);
  }
  function saveCredential() {
    run(async (valid) => {
      if (!username || !password)
        throw new Error(
          "Enter the dedicated inspection username and password.",
        );
      if (selected && !currentCredential)
        throw new Error("Reload the selected credential before rotating it.");
      const request = {
        action: "database-credential-save",
        scope: { project, environment, serviceReference, containerId },
        values: {
          values: { username, password },
          expected_revision: currentCredential?.revision || 0,
          ...(currentCredential ? { credential_id: currentCredential.id } : {}),
          request_id: crypto.randomUUID(),
        },
      };
      setPending(request);
      await executePending(request, valid);
    });
  }
  function revoke() {
    run(async (valid) => {
      if (!currentCredential) throw new Error("Select a credential first.");
      const result = await call("database-credential-revoke", {
        credential_id: currentCredential.id,
        expected_revision: currentCredential.revision,
      });
      if (!valid()) return;
      setPassword("");
      setUsername("");
      const saved = { action: "database-credential-revoke", result };
      setSavedReload(saved);
      await reloadSaved(saved, valid);
    });
  }
  function saveBinding() {
    run(async (valid) => {
      const revisions = bindingDraftRevisions(draftBase, binding || null);
      if (!candidate)
        throw new Error(
          "Load registered database ownership before saving a binding.",
        );
      const credential = credentials.find((c) => c.id === secret && c.enabled);
      if (!credential)
        throw new Error("Choose an enabled inspection credential.");
      const { allowed_schemas: allowed, row_columns } = databaseRowPolicy(
        schemas,
        policies,
      );
      const values = {
        engine: candidate.engine,
        adapter_version: 1,
        ownership: {
          kind: "adopted-compose",
          reference: serviceReference,
          revision: candidate.recipe_revision,
        },
        database_name: candidate.database,
        database_port: candidate.port,
        secret_id: credential.id,
        secret_revision: credential.revision,
        allowed_schemas: allowed,
        policy_revision: revisions.policy_revision,
        row_columns,
      };
      const request = {
        action: "database-binding-save",
        scope: { project, environment, serviceReference, containerId },
        values: {
          values,
          expected_revision: revisions.expected_revision,
          ...(binding ? { binding_id: binding.id } : {}),
          request_id: crypto.randomUUID(),
        },
      };
      setPending(request);
      await executePending(request, valid);
    });
  }
  const locked = busy || Boolean(pending || savedReload);
  return (
    <Stack spacing={2}>
      <Typography variant="h6">Database configuration</Typography>
      <Alert severity="info">
        Settings and credentials apply only to {project} / {environment}. Use a
        dedicated read-only inspection account. This form never creates database
        users or grants.
      </Alert>
      {draftConflict && (
        <Alert severity="warning">
          This binding changed while the draft was open. Your draft is
          preserved, but saving is blocked. Discard it and reload the current
          definition before editing again.
        </Alert>
      )}
      {error && <Alert severity="error">{error}</Alert>}
      {notice && <Alert severity="success">{notice}</Alert>}
      {busy && (
        <Typography role="status">
          Saving or loading database configuration…
        </Typography>
      )}
      {savedReload && (
        <Alert severity="warning">
          Save confirmed; current state could not yet be reloaded. Retry the
          reload below without submitting another save.
        </Alert>
      )}
      {savedReload && (
        <Button
          disabled={busy}
          onClick={() => run((valid) => reloadSaved(savedReload, valid))}
        >
          Reload confirmed save
        </Button>
      )}
      {pending && (
        <Alert severity="warning">
          The save outcome is not confirmed. Reconcile using the exact original
          request ID; authorization or conflict errors do not prove that nothing
          was saved. Keep this view open to preserve recovery.
        </Alert>
      )}
      {pending && (
        <Button
          disabled={busy}
          onClick={() => run((valid) => executePending(pending, valid))}
        >
          Reconcile saved request
        </Button>
      )}
      {(edited || username || password || draftConflict) &&
        !pending &&
        !savedReload && (
          <Button
            disabled={busy}
            onClick={() => {
              setDraftBase(binding || null);
              setUsername("");
              setPassword("");
              setSelected("");
              setSchemas(
                binding?.definition.allowed_schemas.join("\n") || "public",
              );
              setSecret(binding?.definition.secret_id || "");
              setPolicies(
                Object.entries(binding?.definition.row_columns || {}).flatMap(
                  ([schema, tables]) =>
                    Object.entries(tables).map(([table, columns]) => ({
                      schema,
                      table,
                      columns: columns.join(", "),
                    })),
                ),
              );
              setEdited(false);
            }}
          >
            {draftConflict
              ? "Discard draft and load current binding"
              : "Discard unsaved changes"}
          </Button>
        )}
      <Button disabled={busy} onClick={reload}>
        Reload credentials and ownership
      </Button>
      {cursor && (
        <Button
          disabled={busy}
          onClick={() => run((valid) => reloadCredentials(valid, cursor))}
        >
          Load more credentials
        </Button>
      )}
      {loaded && credentials.length === 0 && (
        <Typography>
          No inspection credentials are configured in this environment.
        </Typography>
      )}
      <Typography fontWeight={600}>
        Inspection credential
        <Hint label="Credential storage">
          Passwords are encrypted by the worker. Existing passwords are never
          returned. Rotation requires entering the complete replacement
          credential.
        </Hint>
      </Typography>
      <Field
        select
        label="Create or rotate credential"
        value={selected}
        disabled={locked}
        onChange={(e) => {
          setSelected(e.target.value);
          setUsername("");
          setPassword("");
        }}
      >
        <MenuItem value="">Create new credential</MenuItem>
        {credentials.map((c) => (
          <MenuItem key={c.id} value={c.id}>
            Credential {c.id.slice(0, 8)} · revision {c.revision} ·{" "}
            {c.enabled ? "enabled" : "revoked"}
          </MenuItem>
        ))}
      </Field>
      <Field
        label="Database username"
        value={username}
        disabled={locked}
        autoComplete="off"
        onChange={(e) => setUsername(e.target.value)}
        inputProps={{ maxLength: 128 }}
      />
      <Field
        label="Database password"
        type="password"
        value={password}
        disabled={locked}
        autoComplete="new-password"
        onChange={(e) => setPassword(e.target.value)}
        inputProps={{ maxLength: 4096 }}
      />
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap" }}>
        <Button
          variant="contained"
          disabled={locked || !username || !password}
          onClick={saveCredential}
        >
          {selected ? "Rotate credential" : "Save credential"}
        </Button>
        <Button
          color="error"
          disabled={locked || !currentCredential?.enabled}
          onClick={revoke}
        >
          Revoke selected credential
        </Button>
      </Stack>
      <Typography fontWeight={600}>
        {binding ? "Update selected binding" : "Create database binding"}
      </Typography>
      {!candidate ? (
        <Alert severity="warning">
          Registered live database ownership is required. Reload ownership to
          retry; no database name, port, or ownership revision is guessed.
        </Alert>
      ) : (
        <Typography variant="body2">
          {candidate.database} · {candidate.engine} · port {candidate.port} ·
          registered service {candidate.reference}
        </Typography>
      )}
      <Field
        select
        label="Binding inspection credential"
        value={secret}
        disabled={locked}
        onChange={(e) => {
          setEdited(true);
          setSecret(e.target.value);
        }}
      >
        <MenuItem value="">Choose an enabled credential</MenuItem>
        {credentials
          .filter((c) => c.enabled)
          .map((c) => (
            <MenuItem key={c.id} value={c.id}>
              Credential {c.id.slice(0, 8)} · revision {c.revision}
            </MenuItem>
          ))}
      </Field>
      <Field
        label="Permitted schemas (one per line)"
        multiline
        minRows={2}
        value={schemas}
        disabled={locked}
        onChange={(e) => {
          setEdited(true);
          setSchemas(e.target.value);
        }}
      />
      <Typography variant="body2">
        Without row policies, this binding permits metadata inspection only. Add
        explicit permitted columns to read table rows.
      </Typography>
      {policies.map((policy, index) => (
        <Stack
          key={index}
          spacing={1}
          sx={{
            border: "1px solid",
            borderColor: "divider",
            borderRadius: 1,
            p: 2,
          }}
        >
          {["schema", "table", "columns"].map((field) => (
            <Field
              key={field}
              label={
                field === "columns"
                  ? "Permitted columns (comma separated)"
                  : field === "schema"
                    ? "Policy schema"
                    : "Policy table"
              }
              value={policy[field]}
              disabled={locked}
              onChange={(e) => {
                setEdited(true);
                setPolicies((old) =>
                  old.map((p, i) =>
                    i === index ? { ...p, [field]: e.target.value } : p,
                  ),
                );
              }}
            />
          ))}
          <Button
            disabled={locked}
            onClick={() => {
              setEdited(true);
              setPolicies((old) => old.filter((_, i) => i !== index));
            }}
          >
            Remove row policy
          </Button>
        </Stack>
      ))}
      <Button
        disabled={locked || policies.length >= 100}
        onClick={() => {
          setEdited(true);
          setPolicies((old) => [
            ...old,
            { schema: "", table: "", columns: "" },
          ]);
        }}
      >
        Add row policy
      </Button>
      <Button
        variant="contained"
        disabled={locked || draftConflict || !candidate || !secret}
        onClick={saveBinding}
      >
        {binding ? "Save binding revision" : "Save new binding"}
      </Button>
    </Stack>
  );
}
