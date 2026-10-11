import TextField from "./Field.jsx";
import {
  Dns as DnsOutlined,
  Refresh,
  PlayArrow,
  StopCircleOutlined,
  Visibility as VisibilityOutlined,
  VisibilityOff as VisibilityOffOutlined,
  Search,
  Add,
} from "./icons.jsx";
import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Avatar,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  InputAdornment,
  Stack,
  Tab,
  Tabs,
  Tooltip,
  Typography,
} from "@mui/material";
import TerminalPanel from "./TerminalPanel";
import ContainerLimits from "./ContainerLimits";
import ContainerLogs from "./ContainerLogs";

const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const showDate = (value) =>
  value &&
  !String(value).startsWith("0001-") &&
  Number.isFinite(new Date(value).getTime())
    ? new Date(value).toLocaleString()
    : "Not recorded";
export function updateEnvironmentDraft(environment, previous, key, value) {
  const next = { ...previous };
  if (has(environment, key) && environment[key] === value) delete next[key];
  else
    Object.defineProperty(next, key, {
      value,
      writable: true,
      enumerable: true,
      configurable: true,
    });
  return next;
}
export function environmentProblem(changes) {
  const entries = Object.entries(changes);
  if (!entries.length || entries.length > 200)
    return "Provide between 1 and 200 environment changes.";
  if (
    entries.some(
      ([key, value]) =>
        !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key) ||
        typeof value !== "string" ||
        value.includes("\x00") ||
        value.length > 32768,
    )
  )
    return "An environment key or value is invalid.";
  return "";
}
export function containerObservation(
  data,
  name,
  containerId,
  failed = false,
  now = Date.now() / 1000,
) {
  const snapshot = data?.snapshots?.containers;
  const observed = (snapshot?.value || []).find(
    (container) => container.Names === name,
  );
  const stale =
    failed ||
    !snapshot ||
    !Number.isFinite(snapshot.updated) ||
    now - snapshot.updated > 90 ||
    data?.snapshots?.worker?.value?.state !== "running";
  const replaced = !!(
    containerId &&
    observed?.ID &&
    !containerId.startsWith(observed.ID)
  );
  return { observed, stale, replaced };
}
export function containerOperationBody(operation, project, name) {
  return {
    action:
      operation.kind === "environment-save"
        ? operation.kind
        : `container-${operation.kind}`,
    project,
    container: name,
    container_id: operation.containerId,
    revision: operation.revision,
    request_id: operation.requestId,
    ...(operation.changes ? { changes: operation.changes } : {}),
  };
}
const actionTitle = (operation) =>
  operation === "environment-save"
    ? "Apply environment changes"
    : `${operation[0].toUpperCase()}${operation.slice(1)} container`;

export default function ContainerPage(props) {
  return (
    <ContainerWorkspace key={`${props.project}/${props.name}`} {...props} />
  );
}

function ContainerWorkspace({
  project,
  name,
  data,
  api,
  refresh,
  notify,
  setDirty,
  observationFailed = false,
  initialTab = null,
  setOperationPending,
}) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tab, setTab] = useState(initialTab === "terminal" ? 2 : 0);
  useEffect(() => {
    setTab(initialTab === "terminal" ? 2 : 0);
  }, [initialTab]);
  const [changes, setChanges] = useState({});
  const [revealed, setRevealed] = useState({});
  const [search, setSearch] = useState("");
  const [newKey, setNewKey] = useState("");
  const [keyError, setKeyError] = useState("");
  const [operation, setOperation] = useState(null);
  const [pending, setPending] = useState(false);
  const [limitsDirty, setLimitsDirty] = useState(false);
  const [limitsPending, setLimitsPending] = useState(false);
  const [operationError, setOperationError] = useState("");
  const [discardOpen, setDiscardOpen] = useState(false);
  const [needsInspection, setNeedsInspection] = useState(false);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const mounted = useRef(true);
  const apiRef = useRef(api);
  apiRef.current = api;
  const dirtyRef = useRef(setDirty);
  dirtyRef.current = setDirty;
  const pendingRef = useRef(setOperationPending);
  pendingRef.current = setOperationPending;
  const count = Object.keys(changes).length;
  const { observed, stale, replaced } = containerObservation(
    data,
    name,
    detail?.container_id,
    observationFailed,
  );
  const unavailable =
    stale || !observed?.ID || replaced || needsInspection || refreshRequired;
  const state = unavailable ? "Unavailable" : observed?.State || detail?.state;
  const reason = replaced
    ? "This container was replaced. Refresh its details to inspect the new version."
    : needsInspection
      ? "Check the current details before starting another operation."
      : refreshRequired
        ? "The operation completed. Refresh the details to inspect the updated container."
        : stale
          ? "Current service observations are unavailable or stale. Mutations and terminal connections are disabled."
          : !observed?.ID
            ? "This container is no longer present in the latest observation."
            : "";
  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const result = await apiRef.current("action", {
        action: "container-details",
        project,
        container: name,
      });
      if (mounted.current) {
        setDetail(result);
        setRevealed({});
        setNeedsInspection(false);
        setRefreshRequired(false);
      }
      return result;
    } catch (failure) {
      if (mounted.current) setError(failure.message);
    } finally {
      if (mounted.current) setLoading(false);
    }
  };
  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      dirtyRef.current?.(false);
      pendingRef.current?.(false);
    };
  }, [project, name]);
  useEffect(() => {
    dirtyRef.current?.(count > 0 || pending || limitsDirty);
  }, [count, pending, limitsDirty]);
  useEffect(() => {
    pendingRef.current?.(pending || limitsPending);
  }, [pending, limitsPending]);
  useEffect(() => {
    const protect = (event) => {
      if (count || pending || limitsDirty || limitsPending) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [count, pending, limitsDirty, limitsPending]);
  const reload = async () => {
    if (count || pending || limitsDirty || limitsPending) return;
    setOperation(null);
    await load();
    try {
      await refresh?.();
    } catch {
      /* Parent owns observation error presentation. */
    }
  };
  const edit = (key, value) =>
    setChanges((previous) =>
      updateEnvironmentDraft(detail.environment, previous, key, value),
    );
  const addVariable = () => {
    const key = newKey.trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key)) {
      setKeyError(
        "Use letters, numbers and underscores, starting with a letter or underscore; maximum 128 characters.",
      );
      return;
    }
    if (has(detail.environment, key) || has(changes, key)) {
      setKeyError(
        "This variable already exists. Search for it to edit its value.",
      );
      return;
    }
    if (count >= 200) {
      setKeyError(
        "Apply or discard the current 200 changes before adding more.",
      );
      return;
    }
    setChanges((previous) => ({ ...previous, [key]: "" }));
    setNewKey("");
    setKeyError("");
    setSearch("");
  };
  const review = (kind) => {
    if (unavailable || loading || pending || limitsDirty || limitsPending)
      return;
    if (kind !== "environment-save" && count) {
      setTab(1);
      notify(
        "Review or discard environment changes before changing the container state.",
      );
      return;
    }
    if (kind === "environment-save" && environmentProblem(changes)) {
      notify(environmentProblem(changes));
      return;
    }
    setOperation({
      kind,
      requestId: crypto.randomUUID(),
      containerId: detail.container_id,
      revision: detail.revision,
      changes: kind === "environment-save" ? { ...changes } : undefined,
      submitted: false,
    });
    setOperationError("");
  };
  const apply = async () => {
    if (
      !operation ||
      pending ||
      operation.resolved ||
      (!operation.submitted && unavailable)
    )
      return;
    setPending(true);
    pendingRef.current?.(true);
    setOperationError("");
    setOperation((previous) => ({ ...previous, submitted: true }));
    try {
      const result = await apiRef.current(
        "action",
        containerOperationBody(operation, project, name),
      );
      if (!mounted.current) return;
      if (result.status !== "succeeded") {
        if (
          ["failed", "rolled-back", "recovery-failed"].includes(result.status)
        )
          setOperation((previous) => ({ ...previous, resolved: true }));
        setOperationError(
          result.message ||
            "The operation did not complete. Inspect the current state before retrying.",
        );
        setNeedsInspection(true);
        return;
      }
      setChanges({});
      setRevealed({});
      setOperation(null);
      setRefreshRequired(true);
      notify(result.message);
      await load();
      try {
        await refresh?.();
      } catch {
        /* Parent owns observation error presentation. */
      }
    } catch (failure) {
      if (mounted.current) {
        setOperationError(
          `${failure.message} The result could not be confirmed. Retrying uses the same operation ID: a recorded result is returned, or the original operation runs if it never started. You can also close and inspect the service.`,
        );
        setNeedsInspection(true);
      }
    } finally {
      if (mounted.current) {
        setPending(false);
        pendingRef.current?.(false);
      }
    }
  };
  const guard =
    loading ||
    !!error ||
    unavailable ||
    pending ||
    limitsPending ||
    limitsDirty;
  const environment = detail ? { ...detail.environment, ...changes } : {};
  const keys = Object.keys(environment)
    .sort()
    .filter((key) => key.toLowerCase().includes(search.toLowerCase()));
  const invalidValue = Object.values(changes).some(
    (value) => value.includes("\x00") || value.length > 32768,
  );
  return (
    <Box>
      <header className="project-heading container-heading">
        <Box sx={{ minWidth: 0 }}>
          <div className="project-identity">
            <Avatar
              variant="rounded"
              sx={{
                bgcolor: "action.hover",
                color: "primary.main",
                width: 44,
                height: 44,
              }}
            >
              <DnsOutlined />
            </Avatar>
            <Typography variant="h4" sx={{ overflowWrap: "anywhere" }}>
              {name}
            </Typography>
          </div>
          <Typography color="text.secondary" sx={{ mt: 1 }}>
            {data?.projects?.[project]?.label || project}
            {detail?.service ? ` · ${detail.service}` : ""}
          </Typography>
        </Box>
        <Stack
          direction="row"
          spacing={1}
          alignItems="center"
          useFlexGap
          flexWrap="wrap"
        >
          {detail && (
            <Chip
              size="small"
              label={state}
              color={state === "running" ? "success" : "default"}
              variant="outlined"
            />
          )}
          <Tooltip
            title={
              count
                ? "Review or discard your draft before refreshing details"
                : "Refresh container details"
            }
          >
            <span>
              <IconButton
                aria-label="Refresh container details"
                disabled={
                  loading ||
                  pending ||
                  limitsPending ||
                  limitsDirty ||
                  count > 0
                }
                onClick={reload}
              >
                <Refresh />
              </IconButton>
            </span>
          </Tooltip>
          {detail && (
            <>
              <Tooltip title="Start container">
                <span>
                  <IconButton
                    aria-label="Start container"
                    onClick={() => review("start")}
                    disabled={guard || state === "running"}
                  >
                    <PlayArrow />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Restart container">
                <span>
                  <IconButton
                    aria-label="Restart container"
                    onClick={() => review("restart")}
                    disabled={guard || state !== "running"}
                  >
                    <Refresh />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Stop container">
                <span>
                  <IconButton
                    aria-label="Stop container"
                    onClick={() => review("stop")}
                    disabled={guard || state !== "running"}
                  >
                    <StopCircleOutlined />
                  </IconButton>
                </span>
              </Tooltip>
            </>
          )}
        </Stack>
      </header>
      {error && (
        <Alert
          severity="error"
          sx={{ mb: 2 }}
          action={
            !count && (
              <Button color="inherit" onClick={reload} disabled={loading}>
                Try again
              </Button>
            )
          }
        >
          {error}
        </Alert>
      )}
      {loading && (
        <Stack
          direction="row"
          spacing={2}
          alignItems="center"
          sx={{ my: 3 }}
          role="status"
        >
          <CircularProgress size={20} />
          <Typography>Loading container configuration…</Typography>
        </Stack>
      )}
      {detail && (
        <>
          {unavailable && (
            <Alert severity="warning" sx={{ mb: 2 }}>
              {reason}
              {count > 0 &&
                " Your environment draft is preserved. Discard it before loading the new configuration."}
            </Alert>
          )}
          <Tabs
            value={tab}
            onChange={(_, next) => setTab(next)}
            className="project-tabs"
            variant="scrollable"
            scrollButtons="auto"
            aria-label="Container details"
          >
            {["Details & version", "Variables", "Terminal", "Logs"].map(
              (label, index) => (
                <Tab
                  key={label}
                  id={`service-tab-${index}`}
                  aria-controls={`service-panel-${index}`}
                  label={index === 1 && count ? `${label} (${count})` : label}
                />
              ),
            )}
          </Tabs>
          <Box sx={{ display: tab === 0 ? "block" : "none" }}>
            <ContainerLimits
              project={project}
              name={name}
              detail={detail}
              api={api}
              disabled={
                loading ||
                !!error ||
                unavailable ||
                pending ||
                count > 0 ||
                !!operation
              }
              disabledReason={
                count
                  ? "Apply or discard variable changes before editing limits."
                  : ""
              }
              onDirty={setLimitsDirty}
              onPending={setLimitsPending}
              onNeedsInspection={() => setNeedsInspection(true)}
              onApplied={async (message) => {
                notify(message || "Container limits applied and verified.");
                await load();
                try {
                  await refresh?.();
                } catch {
                  /* Parent presents observation failures. */
                }
              }}
              onInspect={async () => {
                const inspected = await load();
                try {
                  await refresh?.();
                } catch {
                  /* Parent presents observation failures. */
                }
                return inspected;
              }}
            />
          </Box>
          <Box
            role="tabpanel"
            id={`service-panel-${tab}`}
            aria-labelledby={`service-tab-${tab}`}
            sx={{ py: 1 }}
          >
            {tab === 3 && <ContainerLogs api={api} project={project} name={name} containerId={detail.container_id} enabled={!loading && !error && !unavailable && !pending && !operation}/>}
            {tab === 0 && (
              <>
                <Typography variant="h6" sx={{ mb: 1 }}>
                  Version & runtime
                </Typography>
                <Typography color="text.secondary" sx={{ mb: 3 }}>
                  Actions apply to this container. Stopping a database or cache
                  can affect dependent services.
                </Typography>
                <Box
                  component="dl"
                  sx={{
                    display: "grid",
                    gridTemplateColumns: {
                      xs: "1fr",
                      sm: "160px minmax(0, 1fr)",
                    },
                    gap: { xs: 0.75, sm: 2 },
                    m: 0,
                    "& dt": { color: "text.secondary", fontSize: 12 },
                    "& dd": {
                      m: 0,
                      mb: { xs: 1.5, sm: 0 },
                      overflowWrap: "anywhere",
                      fontFamily: "ui-monospace, monospace",
                      fontSize: 12,
                    },
                  }}
                >
                  {[
                    ["Status", state],
                    [
                      "Health / observation",
                      unavailable
                        ? "Current status unavailable"
                        : observed?.Status || detail.health,
                    ],
                    ["Image / version", detail.image],
                    ["Image ID", detail.image_id],
                    [
                      "Git commit",
                      detail.commit || "Not recorded in this image",
                    ],
                    ["Image created", showDate(detail.image_created)],
                    ["Started", showDate(detail.started_at)],
                    ["Restart count", detail.restart_count],
                    ["Container ID", detail.container_id],
                    ["Compose service", detail.service],
                    ["Configuration file", detail.configuration_file],
                    [
                      "Image digests",
                      detail.image_digests?.join("\n") || "Not recorded",
                    ],
                  ].map(([label, value]) => (
                    <React.Fragment key={label}>
                      <Typography component="dt">{label}</Typography>
                      <Box component="dd" sx={{ whiteSpace: "pre-wrap" }}>
                        {value ?? "Not recorded"}
                      </Box>
                    </React.Fragment>
                  ))}
                </Box>
                <Divider sx={{ my: 4 }} />
                <Typography variant="h6" sx={{ mb: 2 }}>
                  Persistent mounts
                </Typography>
                {detail.mounts?.length ? (
                  <Stack divider={<Divider />} spacing={2}>
                    {detail.mounts.map((mount, index) => (
                      <Stack
                        key={`${mount.destination}-${index}`}
                        direction={{ xs: "column", sm: "row" }}
                        spacing={1}
                        justifyContent="space-between"
                      >
                        <Typography
                          sx={{
                            fontFamily: "ui-monospace, monospace",
                            overflowWrap: "anywhere",
                          }}
                        >
                          {mount.destination}
                        </Typography>
                        <Stack direction="row" spacing={1}>
                          <Chip
                            size="small"
                            label={mount.type}
                            variant="outlined"
                          />
                          <Chip
                            size="small"
                            label={
                              mount.read_only ? "Read only" : "Read / write"
                            }
                            variant="outlined"
                          />
                        </Stack>
                      </Stack>
                    ))}
                  </Stack>
                ) : (
                  <Typography color="text.secondary">
                    No mounts are configured for this container.
                  </Typography>
                )}
                <Divider sx={{ my: 4 }} />
                <Typography variant="h6" sx={{ mb: 2 }}>
                  Ports
                </Typography>
                {Object.keys(detail.ports || {}).length ? (
                  <Stack spacing={1}>
                    {Object.entries(detail.ports).map(([port, bindings]) => (
                      <Stack
                        key={port}
                        direction={{ xs: "column", sm: "row" }}
                        spacing={2}
                      >
                        <Typography component="code">{port}</Typography>
                        <Typography
                          color="text.secondary"
                          sx={{ overflowWrap: "anywhere" }}
                        >
                          {bindings?.length
                            ? bindings
                                .map(
                                  (binding) =>
                                    `${binding.HostIp}:${binding.HostPort}`,
                                )
                                .join(", ")
                            : "Container network only"}
                        </Typography>
                      </Stack>
                    ))}
                  </Stack>
                ) : (
                  <Typography color="text.secondary">
                    No exposed ports are recorded.
                  </Typography>
                )}
              </>
            )}
            {tab === 1 && (
              <Box sx={{ maxWidth: 920 }}>
                <Typography color="text.secondary" sx={{ mb: 2 }}>
                  {detail.environment_scope}
                </Typography>
                <Alert severity="info" sx={{ mb: 3 }}>
                  Values are hidden by default. Applying changes updates Compose
                  and recreates this service. Changing a database password
                  variable does not rotate existing database accounts; update
                  database credentials and connected services together.
                </Alert>
                <Stack
                  direction={{ xs: "column", sm: "row" }}
                  spacing={2}
                  alignItems={{ sm: "center" }}
                  sx={{ mb: 3 }}
                >
                  <TextField
                    fullWidth
                    size="small"
                    label="Find an environment variable"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    slotProps={{
                      input: {
                        startAdornment: (
                          <InputAdornment position="start">
                            <Search fontSize="small" />
                          </InputAdornment>
                        ),
                      },
                    }}
                  />
                  <Typography
                    color={count ? "primary.main" : "text.secondary"}
                    sx={{ whiteSpace: "nowrap" }}
                    role="status"
                  >
                    {count
                      ? `${count} unsaved ${count === 1 ? "change" : "changes"}`
                      : "No unsaved changes"}
                  </Typography>
                </Stack>
                <Stack spacing={2}>
                  {keys.map((key) => (
                    <Stack
                      key={key}
                      direction="row"
                      spacing={1}
                      alignItems="center"
                    >
                      <TextField
                        fullWidth
                        label={key}
                        value={environment[key]}
                        type={
                          has(revealed, key) && revealed[key]
                            ? "text"
                            : "password"
                        }
                        autoComplete="off"
                        disabled={pending || limitsPending}
                        error={
                          has(changes, key) &&
                          (environment[key].includes("\x00") ||
                            environment[key].length > 32768)
                        }
                        helperText={
                          has(changes, key)
                            ? environment[key].includes("\x00") ||
                              environment[key].length > 32768
                              ? "Use at most 32,768 characters and no null characters."
                              : has(detail.environment, key)
                                ? "Modified"
                                : "New variable · an empty value is allowed"
                            : undefined
                        }
                        onChange={(event) => edit(key, event.target.value)}
                        slotProps={{
                          input: {
                            endAdornment: (
                              <InputAdornment position="end">
                                <Tooltip
                                  title={
                                    has(revealed, key) && revealed[key]
                                      ? `Hide ${key}`
                                      : `Reveal ${key}`
                                  }
                                >
                                  <IconButton
                                    aria-label={
                                      has(revealed, key) && revealed[key]
                                        ? `Hide ${key}`
                                        : `Reveal ${key}`
                                    }
                                    aria-pressed={
                                      has(revealed, key) && !!revealed[key]
                                    }
                                    onClick={() =>
                                      setRevealed((previous) => ({
                                        ...previous,
                                        [key]: !(
                                          has(previous, key) && previous[key]
                                        ),
                                      }))
                                    }
                                  >
                                    {has(revealed, key) && revealed[key] ? (
                                      <VisibilityOffOutlined />
                                    ) : (
                                      <VisibilityOutlined />
                                    )}
                                  </IconButton>
                                </Tooltip>
                              </InputAdornment>
                            ),
                          },
                        }}
                      />
                      {has(changes, key) && (
                        <Button
                          size="small"
                          color="inherit"
                          disabled={pending || limitsPending}
                          aria-label={`Revert ${key}`}
                          onClick={() =>
                            setChanges((previous) => {
                              const next = { ...previous };
                              delete next[key];
                              return next;
                            })
                          }
                        >
                          Revert
                        </Button>
                      )}
                    </Stack>
                  ))}
                  {!keys.length && (
                    <Typography color="text.secondary">
                      {search
                        ? "No variables match your search."
                        : "This container has no environment variables. Add a variable below."}
                    </Typography>
                  )}
                </Stack>
                <Stack
                  component="form"
                  direction={{ xs: "column", sm: "row" }}
                  spacing={2}
                  alignItems={{ sm: "flex-start" }}
                  sx={{ mt: 3 }}
                  onSubmit={(event) => {
                    event.preventDefault();
                    addVariable();
                  }}
                >
                  <TextField
                    label="New variable name"
                    placeholder="NEW_VARIABLE"
                    value={newKey}
                    onChange={(event) => {
                      setNewKey(event.target.value);
                      setKeyError("");
                    }}
                    error={!!keyError}
                    helperText={keyError}
                    disabled={pending || limitsPending}
                    sx={{ flex: 1 }}
                  />
                  <Button
                    type="submit"
                    variant="outlined"
                    startIcon={<Add />}
                    disabled={pending || !newKey.trim()}
                    sx={{ mt: { sm: "9px !important" } }}
                  >
                    Add variable
                  </Button>
                </Stack>
                <Divider sx={{ my: 3 }} />
                <Stack direction="row" useFlexGap flexWrap="wrap" spacing={2}>
                  <Button
                    variant="contained"
                    disabled={!count || count > 200 || invalidValue || guard}
                    onClick={() => review("environment-save")}
                  >
                    Review changes
                  </Button>
                  <Button
                    variant="outlined"
                    color="inherit"
                    disabled={!count || pending}
                    onClick={() => setDiscardOpen(true)}
                  >
                    Discard changes
                  </Button>
                </Stack>
              </Box>
            )}
            {tab === 2 && (
              <TerminalPanel
                key={detail.container_id}
                project={project}
                name={name}
                containerId={detail.container_id}
                available={!guard && state === "running" && !operation}
                reason={
                  reason ||
                  "Start this container from Details & version before connecting."
                }
                api={api}
              />
            )}
          </Box>
        </>
      )}
      <Dialog
        open={discardOpen}
        onClose={() => setDiscardOpen(false)}
        fullWidth
        maxWidth="xs"
        aria-labelledby="discard-environment-title"
      >
        <DialogTitle id="discard-environment-title">
          Discard environment changes?
        </DialogTitle>
        <DialogContent>
          <Typography>
            {count} unsaved {count === 1 ? "change will" : "changes will"} be
            removed from this draft. Saved configuration will remain unchanged.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button color="inherit" onClick={() => setDiscardOpen(false)}>
            Keep editing
          </Button>
          <Button
            variant="contained"
            onClick={() => {
              setChanges({});
              setRevealed({});
              setDiscardOpen(false);
            }}
          >
            Discard changes
          </Button>
        </DialogActions>
      </Dialog>
      <Dialog
        open={!!operation}
        onClose={() => {
          if (!pending) setOperation(null);
        }}
        fullWidth
        maxWidth="sm"
        aria-labelledby="container-operation-title"
      >
        <DialogTitle id="container-operation-title">
          {operation ? actionTitle(operation.kind) : ""}?
        </DialogTitle>
        <DialogContent>
          <Typography sx={{ fontWeight: 600, overflowWrap: "anywhere", mb: 2 }}>
            {name}
          </Typography>
          <Typography color="text.secondary" sx={{ mb: 2 }}>
            {operation?.kind === "environment-save"
              ? "This recreates the service and briefly interrupts it if running. The previous configuration is restored if runtime checks fail."
              : "This changes the live container state. Other services may depend on this container."}
          </Typography>
          {operation?.changes && (
            <>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Changed variables · values hidden
              </Typography>
              <Stack
                direction="row"
                spacing={1}
                useFlexGap
                flexWrap="wrap"
                sx={{ mb: 2 }}
              >
                {Object.keys(operation.changes).map((key) => (
                  <Chip size="small" variant="outlined" key={key} label={key} />
                ))}
              </Stack>
            </>
          )}
          {unavailable && !operation?.submitted && (
            <Alert severity="warning" sx={{ my: 2 }}>
              {reason}
            </Alert>
          )}
          {operationError && (
            <Alert severity="error" sx={{ my: 2 }}>
              {operationError}
            </Alert>
          )}
          {pending && (
            <Stack
              direction="row"
              spacing={2}
              alignItems="center"
              role="status"
            >
              <CircularProgress size={20} />
              <Typography>Applying and verifying runtime state…</Typography>
            </Stack>
          )}
        </DialogContent>
        <DialogActions sx={{ p: 3 }}>
          <Button
            color="inherit"
            disabled={pending || limitsPending}
            onClick={() => setOperation(null)}
          >
            {operation?.submitted ? "Close" : "Cancel"}
          </Button>
          <Button
            variant="contained"
            disabled={
              pending ||
              operation?.resolved ||
              (!operation?.submitted && unavailable)
            }
            onClick={apply}
          >
            {pending
              ? "Applying…"
              : operation?.resolved
                ? "Operation finished"
                : operation?.submitted
                  ? "Retry same operation"
                  : "Apply"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
