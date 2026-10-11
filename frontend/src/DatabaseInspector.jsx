import { supportedDatabaseEngines } from "./databaseConfiguration.js";
import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  MenuItem,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import Field from "./Field";
import DatabaseConfiguration from "./DatabaseConfiguration";
import {
  verifyDatabaseScope,
  invalidateDatabaseEvidence,
} from "./databaseInspector.js";

/** Existing-binding workflow; all SQL and connection policy remain worker-owned. */
export default function DatabaseInspector({
  api,
  project,
  environment,
  serviceReference,
  containerId,
  open,
  onClose,
  onPending,
  onDirty,
}) {
  const [configOpen, setConfigOpen] = useState(false),
    [configBusy, setConfigBusy] = useState(false),
    [configDirty, setConfigDirty] = useState(false);
  const [catalogLoaded, setCatalogLoaded] = useState(false),
    [schemasLoaded, setSchemasLoaded] = useState(false),
    [tablesLoaded, setTablesLoaded] = useState(false);
  const [bindings, setBindings] = useState([]),
    [catalogCursor, setCatalogCursor] = useState(null);
  const [binding, setBinding] = useState(null),
    [preview, setPreview] = useState(null),
    [accepted, setAccepted] = useState(false);
  const [schemas, setSchemas] = useState([]),
    [schema, setSchema] = useState(""),
    [tables, setTables] = useState([]),
    [table, setTable] = useState("");
  const [tableCursor, setTableCursor] = useState(null),
    [schemaCursor, setSchemaCursor] = useState(null),
    [detail, setDetail] = useState(null),
    [rows, setRows] = useState(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const generation = useRef(0),
    active = useRef(false),
    acceptance = useRef(null),
    apiRef = useRef(api);
  apiRef.current = api;
  const scopeReady = Boolean(
    project &&
    ["development", "production"].includes(environment) &&
    serviceReference &&
    containerId,
  );
  const call = (action, values = {}) =>
    apiRef.current("action", { action, project, environment, ...values });
  async function run(work, preserveAcceptance = false) {
    if (active.current) return;
    const current = generation.current;
    active.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const valid = () => current === generation.current;
    try {
      await work(valid);
    } catch (e) {
      if (valid()) {
        setError(
          e?.message ||
            "Database request failed. Reload or verify this binding again.",
        );
        setRows(null);
        setDetail(null);
        setSchemasLoaded(false);
        setTablesLoaded(false);
        if (invalidateDatabaseEvidence(e, preserveAcceptance)) {
          setAccepted(false);
          setSchemas([]);
          setTables([]);
          setSchema("");
          setTable("");
          setPreview(null);
          acceptance.current = null;
        }
      }
    } finally {
      if (valid()) {
        active.current = false;
        setBusy(false);
      }
    }
  }
  const target = () => ({
    binding_id: binding.id,
    expected_revision: binding.revision,
  });
  const read = async (request) =>
    verifyDatabaseScope(await call("database-read", { ...target(), request }), {
      project,
      environment,
      binding,
      containerId,
      candidateFingerprint: preview?.candidate?.fingerprint,
    });
  useEffect(() => {
    onPending?.(busy || configBusy);
    return () => onPending?.(false);
  }, [busy, configBusy, onPending]);
  useEffect(() => {
    onDirty?.(Boolean((preview && !accepted) || configDirty));
    return () => onDirty?.(false);
  }, [preview, accepted, configDirty, onDirty]);
  function clearSelection() {
    setPreview(null);
    setSchemasLoaded(false);
    setTablesLoaded(false);
    setAccepted(false);
    acceptance.current = null;
    setSchemas([]);
    setSchema("");
    setTables([]);
    setTable("");
    setDetail(null);
    setRows(null);
    setTableCursor(null);
    setSchemaCursor(null);
  }
  function catalog(cursor = null) {
    setCatalogLoaded(false);
    return run(async (valid) => {
      const result = await call("database-bindings", {
        limit: 25,
        ...(cursor ? { cursor } : {}),
      });
      if (!valid()) return;
      if (!Array.isArray(result.bindings))
        throw new Error("The binding catalog returned an invalid response.");
      const matches = result.bindings.filter(
        (b) =>
          b.project === project &&
          b.environment === environment &&
          b.definition?.ownership?.kind === "adopted-compose" &&
          b.definition.ownership.reference === serviceReference,
      );
      setBindings((previous) => (cursor ? [...previous, ...matches] : matches));
      setCatalogCursor(result.next_cursor || null);
      setCatalogLoaded(true);
    });
  }
  useEffect(() => {
    generation.current += 1;
    active.current = false;
    setBusy(false);
    setError("");
    setNotice("");
    setBindings([]);
    setCatalogLoaded(false);
    setBinding(null);
    setCatalogCursor(null);
    clearSelection();
    if (open && scopeReady) catalog();
    return () => {
      generation.current += 1;
      active.current = false;
    };
  }, [open, project, environment, serviceReference, containerId]);
  function choose(id) {
    setBinding(bindings.find((b) => b.id === id) || null);
    clearSelection();
    setError("");
    setNotice("");
  }
  function verify() {
    clearSelection();
    run(async (valid) => {
      const result = await call("database-preview", target());
      if (!valid()) return;
      if (
        result.binding_id !== binding.id ||
        result.binding_revision !== binding.revision ||
        result.candidate?.container_id !== containerId ||
        result.candidate?.project !== project ||
        result.candidate?.environment !== environment ||
        result.candidate?.reference !== serviceReference
      )
        throw new Error(
          "The database identity no longer matches this container. Reload its details before continuing.",
        );
      setPreview(result);
      acceptance.current = {
        preview_id: result.preview_id,
        request_id: crypto.randomUUID(),
      };
    });
  }
  function accept() {
    run(async (valid) => {
      const pending = acceptance.current;
      if (!pending)
        throw new Error("Verify the binding before accepting its identity.");
      const receipt = await call("database-accept", pending);
      if (!valid()) return;
      if (
        receipt.binding_id !== binding.id ||
        receipt.binding_revision !== binding.revision ||
        receipt.preview_id !== pending.preview_id
      )
        throw new Error("The acceptance receipt did not match this binding.");
      setAccepted(true);
      setNotice(
        "Identity accepted. Each read rechecks the container, storage, credential, and database identity.",
      );
      const result = await read({ action: "schemas", limit: 100 });
      if (!valid()) return;
      setSchemas(result.data);
      setSchemasLoaded(true);
      setSchemaCursor(result.next_cursor || null);
    }, true);
  }
  function loadSchemas() {
    setSchemasLoaded(false);
    run(async (valid) => {
      const result = await read({
        action: "schemas",
        limit: 100,
        ...(schemaCursor ? { cursor: schemaCursor } : {}),
      });
      if (valid()) {
        setSchemas((old) =>
          schemaCursor ? [...old, ...result.data] : result.data,
        );
        setSchemaCursor(result.next_cursor || null);
        setSchemasLoaded(true);
      }
    });
  }
  function selectSchema(value) {
    setSchema(value);
    setTable("");
    setTables([]);
    setTablesLoaded(false);
    setDetail(null);
    setRows(null);
    setTableCursor(null);
    if (value) loadTables(value);
  }
  function loadTables(value = schema, cursor = null) {
    setTablesLoaded(false);
    setDetail(null);
    setRows(null);
    if (!cursor) {
      setTables([]);
      setTable("");
      setTableCursor(null);
    }
    run(async (valid) => {
      const result = await read({
        action: "tables",
        schema: value,
        limit: 100,
        ...(cursor ? { cursor } : {}),
      });
      if (valid()) {
        setTables((old) => (cursor ? [...old, ...result.data] : result.data));
        setTableCursor(result.next_cursor || null);
        setTablesLoaded(true);
      }
    });
  }
  function selectTable(value) {
    setTable(value);
    setRows(null);
    setDetail(null);
    if (value)
      run(async (valid) => {
        const result = await read({ action: "table", schema, table: value });
        if (valid()) setDetail(result);
      });
  }
  function loadRows(cursor = null) {
    run(async (valid) => {
      const result = await read({
        action: "rows",
        schema,
        table,
        limit: 25,
        ...(cursor ? { cursor } : {}),
      });
      if (valid()) setRows(result);
    });
  }
  function disable() {
    run(async (valid) => {
      const result = await call("database-binding-disable", target());
      if (!valid()) return;
      setBinding(result.binding);
      setBindings((old) =>
        old.map((b) => (b.id === result.binding.id ? result.binding : b)),
      );
      clearSelection();
      setNotice("Binding disabled. Saved credentials have not been revoked.");
    });
  }
  const columns = detail?.data?.columns || [];
  return (
    <Dialog
      open={Boolean(open)}
      onClose={busy || configBusy || configDirty ? undefined : onClose}
      fullWidth
      maxWidth="lg"
      aria-labelledby="database-inspector-title"
    >
      <DialogTitle id="database-inspector-title">
        Database inspector
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2} sx={{ minWidth: 0 }}>
          <Typography variant="body2" color="text.secondary">
            {project} / {environment} / {serviceReference}
          </Typography>
          {!scopeReady && (
            <Alert severity="warning">
              Select a registered environment and a current database container
              to inspect its data.
            </Alert>
          )}
          {error && (
            <Alert severity="error" role="alert">
              {error}
            </Alert>
          )}
          {notice && (
            <Alert severity="success" role="status">
              {notice}
            </Alert>
          )}
          <Stack
            direction="row"
            spacing={1}
            useFlexGap
            sx={{ flexWrap: "wrap" }}
          >
            <Button
              disabled={busy || configBusy || configDirty}
              onClick={() => setConfigOpen((value) => !value)}
            >
              {configOpen
                ? "Return to inspection"
                : "Configure database access"}
            </Button>
            {configOpen && binding && (
              <Button
                disabled={busy || configBusy || configDirty}
                onClick={() => {
                  setBinding(null);
                  clearSelection();
                }}
              >
                Configure a new binding
              </Button>
            )}
          </Stack>
          {configOpen && (
            <DatabaseConfiguration
              key={`${project}:${environment}:${binding?.id || "new"}`}
              api={api}
              project={project}
              environment={environment}
              serviceReference={serviceReference}
              containerId={containerId}
              binding={binding}
              onPending={setConfigBusy}
              onDirty={setConfigDirty}
              onChanged={(result) => {
                clearSelection();
                if (result?.binding) {
                  setBinding(result.binding);
                  if (!result.keepConfigurationOpen) setConfigOpen(false);
                }
                catalog();
              }}
            />
          )}
          {busy && (
            <Typography role="status" variant="body2">
              Checking database…
            </Typography>
          )}
          <Box
            component="fieldset"
            disabled={configOpen}
            sx={{
              border: 0,
              p: 0,
              m: 0,
              minWidth: 0,
              display: configOpen ? "none" : "block",
            }}
          >
            <Stack spacing={2}>
              <Stack
                direction="row"
                spacing={1}
                useFlexGap
                sx={{ flexWrap: "wrap" }}
              >
                <Button
                  disabled={busy || !scopeReady}
                  onClick={() => {
                    setBinding(null);
                    clearSelection();
                    catalog();
                  }}
                >
                  Reload bindings
                </Button>
                {catalogCursor && (
                  <Button
                    disabled={busy}
                    onClick={() => catalog(catalogCursor)}
                  >
                    Load more bindings
                  </Button>
                )}
              </Stack>
              {!busy &&
                catalogLoaded &&
                scopeReady &&
                bindings.length === 0 && (
                  <Alert severity="info">
                    No saved database binding for this service is listed
                    {catalogCursor ? " on this page" : ""}.{" "}
                    {catalogCursor
                      ? "Load more bindings to continue searching."
                      : "An authorized administrator must configure a database binding and a dedicated inspection credential before this view can verify or read data."}
                  </Alert>
                )}
              {bindings.length > 0 && (
                <Field
                  select
                  fullWidth
                  label="Database binding"
                  value={binding?.id || ""}
                  disabled={busy}
                  onChange={(e) => choose(e.target.value)}
                >
                  <MenuItem value="">Select a binding</MenuItem>
                  {bindings.map((b) => (
                    <MenuItem key={b.id} value={b.id}>
                      {b.definition.database_name} · {b.definition.engine} ·
                      revision {b.revision} · {b.status}
                    </MenuItem>
                  ))}
                </Field>
              )}
              {binding && (
                <>
                  {!supportedDatabaseEngines.includes(
                    binding.definition.engine,
                  ) && (
                    <Alert severity="warning">
                      This saved database engine is not supported by the current
                      adapters. PostgreSQL, MySQL, and MariaDB are supported.
                    </Alert>
                  )}
                  {binding.acceptance && (
                    <Typography variant="body2" color="text.secondary">
                      Previously accepted{" "}
                      {new Date(
                        binding.acceptance.accepted_at * 1000,
                      ).toLocaleString()}
                      . Fresh runtime verification is still required.
                    </Typography>
                  )}
                  <Typography variant="body2">
                    Configuration status: {binding.status}. Saving configuration
                    does not verify database identity.
                  </Typography>
                  <Stack
                    direction="row"
                    spacing={1}
                    useFlexGap
                    sx={{ flexWrap: "wrap" }}
                  >
                    <Button
                      variant="contained"
                      disabled={
                        busy ||
                        binding.status !== "unverified" ||
                        !supportedDatabaseEngines.includes(
                          binding.definition.engine,
                        )
                      }
                      onClick={verify}
                    >
                      Verify current identity
                    </Button>
                    <Button
                      color="error"
                      disabled={busy || !binding.enabled}
                      onClick={disable}
                    >
                      Disable binding
                    </Button>
                  </Stack>
                  {preview && (
                    <Box
                      sx={{
                        border: "1px solid",
                        borderColor: "divider",
                        borderRadius: 1,
                        p: 2,
                      }}
                    >
                      <Stack spacing={1}>
                        <Typography fontWeight={600}>
                          Review observed database
                        </Typography>
                        <Typography variant="body2">
                          {preview.candidate.database} ·{" "}
                          {preview.candidate.engine} · port{" "}
                          {preview.candidate.port}
                        </Typography>
                        <Typography
                          variant="body2"
                          sx={{ overflowWrap: "anywhere" }}
                        >
                          Container: {preview.candidate.container_id}
                        </Typography>
                        <Typography variant="body2">
                          Container and storage identity were checked. Stable
                          cluster identity is not verified. Preview expires{" "}
                          {new Date(
                            preview.expires * 1000,
                          ).toLocaleTimeString()}
                          .
                        </Typography>
                        <Button
                          variant="contained"
                          disabled={busy || accepted}
                          onClick={accept}
                        >
                          {accepted
                            ? "Identity accepted"
                            : "Accept identity and browse"}
                        </Button>
                      </Stack>
                    </Box>
                  )}
                  {accepted && (
                    <>
                      <Field
                        select
                        fullWidth
                        label="Schema"
                        value={schema}
                        disabled={busy}
                        onChange={(e) => selectSchema(e.target.value)}
                      >
                        <MenuItem value="">Select a schema</MenuItem>
                        {schemas.map((s) => (
                          <MenuItem key={s} value={s}>
                            {s}
                          </MenuItem>
                        ))}
                      </Field>
                      {schemasLoaded && !schemas.length && !busy && (
                        <Typography variant="body2">
                          No permitted schemas were returned.
                        </Typography>
                      )}
                      <Button disabled={busy} onClick={loadSchemas}>
                        {schemaCursor ? "Load more schemas" : "Reload schemas"}
                      </Button>
                      {schema && (
                        <>
                          <Field
                            select
                            fullWidth
                            label="Table"
                            value={table}
                            disabled={busy}
                            onChange={(e) => selectTable(e.target.value)}
                          >
                            <MenuItem value="">Select a table</MenuItem>
                            {tables.map((t) => (
                              <MenuItem key={t.name} value={t.name}>
                                {t.name} ({t.kind})
                              </MenuItem>
                            ))}
                          </Field>
                          {tablesLoaded && !tables.length && !busy && (
                            <Typography variant="body2">
                              No permitted tables were returned for this schema.
                            </Typography>
                          )}
                          <Button
                            disabled={busy}
                            onClick={() => loadTables(schema)}
                          >
                            Reload tables
                          </Button>
                          {tableCursor && (
                            <Button
                              disabled={busy}
                              onClick={() => loadTables(schema, tableCursor)}
                            >
                              Load more tables
                            </Button>
                          )}
                        </>
                      )}
                      {detail && (
                        <>
                          <Typography fontWeight={600}>
                            Columns and relationships
                          </Typography>
                          <TableContainer
                            sx={{ maxWidth: "100%", overflowX: "auto" }}
                          >
                            <Table size="small" aria-label="Database columns">
                              <TableHead>
                                <TableRow>
                                  <TableCell>Column</TableCell>
                                  <TableCell>Type</TableCell>
                                  <TableCell>Nullable</TableCell>
                                </TableRow>
                              </TableHead>
                              <TableBody>
                                {columns.map((c) => (
                                  <TableRow key={c.name}>
                                    <TableCell>{c.name}</TableCell>
                                    <TableCell>{c.type}</TableCell>
                                    <TableCell>
                                      {c.nullable ? "Yes" : "No"}
                                    </TableCell>
                                  </TableRow>
                                ))}
                              </TableBody>
                            </Table>
                          </TableContainer>
                          {(detail.data.keys || []).map((k) => (
                            <Typography key={k.name} variant="body2">
                              {k.name} ·{" "}
                              {k.kind === "p"
                                ? "Primary key"
                                : k.kind === "f"
                                  ? "Foreign key"
                                  : "Unique key"}{" "}
                              · {k.columns.join(", ")}
                              {k.reference &&
                                (k.reference.restricted
                                  ? " → restricted reference"
                                  : ` → ${k.reference.schema}.${k.reference.table} (${k.reference.columns.join(", ")})`)}
                            </Typography>
                          ))}
                          {(detail.data.indexes || []).map((i) => (
                            <Typography key={i.name} variant="body2">
                              Index {i.name} · {i.columns.join(", ")} ·{" "}
                              {i.unique ? "unique" : "non-unique"} ·{" "}
                              {i.valid ? "valid" : "invalid"}
                            </Typography>
                          ))}
                          <Stack direction="row" spacing={1}>
                            <Button disabled={busy} onClick={() => loadRows()}>
                              Read first page
                            </Button>
                            {rows?.next_cursor && (
                              <Button
                                disabled={busy}
                                onClick={() => loadRows(rows.next_cursor)}
                              >
                                Next page
                              </Button>
                            )}
                          </Stack>
                          {rows && (
                            <>
                              <Typography variant="body2">
                                Observed{" "}
                                {new Date(
                                  rows.observed_at * 1000,
                                ).toLocaleString()}
                                . {rows.data.rows.length} rows on this page.
                              </Typography>
                              {rows.data.rows.length === 0 ? (
                                <Alert severity="info">
                                  No rows were returned on this page.
                                </Alert>
                              ) : (
                                <TableContainer
                                  sx={{ maxWidth: "100%", overflowX: "auto" }}
                                >
                                  <Table
                                    size="small"
                                    aria-label="Database rows"
                                  >
                                    <TableHead>
                                      <TableRow>
                                        {rows.data.columns.map((c) => (
                                          <TableCell key={c}>{c}</TableCell>
                                        ))}
                                      </TableRow>
                                    </TableHead>
                                    <TableBody>
                                      {rows.data.rows.map((r, index) => (
                                        <TableRow key={index}>
                                          {r.map((cell, n) => (
                                            <TableCell
                                              key={n}
                                              sx={{
                                                maxWidth: 360,
                                                overflowWrap: "anywhere",
                                                whiteSpace: "pre-wrap",
                                              }}
                                            >
                                              {cell.value === null ? (
                                                <Typography
                                                  component="span"
                                                  color="text.secondary"
                                                >
                                                  NULL
                                                </Typography>
                                              ) : cell.value === "" ? (
                                                <Typography
                                                  component="span"
                                                  color="text.secondary"
                                                >
                                                  Empty string
                                                </Typography>
                                              ) : (
                                                cell.value
                                              )}
                                              {cell.truncated && (
                                                <Typography
                                                  component="span"
                                                  color="text.secondary"
                                                >
                                                  {" "}
                                                  (truncated)
                                                </Typography>
                                              )}
                                            </TableCell>
                                          ))}
                                        </TableRow>
                                      ))}
                                    </TableBody>
                                  </Table>
                                </TableContainer>
                              )}
                            </>
                          )}
                        </>
                      )}
                    </>
                  )}
                </>
              )}
            </Stack>
          </Box>
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button disabled={busy || configBusy || configDirty} onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
