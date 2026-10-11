import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Autocomplete,
  Box,
  Button,
  Checkbox,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControlLabel,
  MenuItem,
  Stack,
  Typography,
} from "@mui/material";
import Field from "./Field";
import Hint from "./Hint";
import { MCP_SCOPES, freshMcpDraft, mcpCredentialValues } from "./mcpAccess";
const date = (v) => (v ? new Date(v * 1000).toLocaleString() : "Never");
export default function McpAccess({ api, onDirty, onPending }) {
  const [data, setData] = useState(null),
    [projects, setProjects] = useState({}),
    [draft, setDraft] = useState(freshMcpDraft),
    [open, setOpen] = useState(false),
    [token, setToken] = useState(""),
    [revoke, setRevoke] = useState(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [uncertain, setUncertain] = useState(false);
  const lock = useRef(false),
    alive = useRef(true),
    apiRef = useRef(api);
  apiRef.current = api;
  const load = async () => {
    const [access, registry] = await Promise.all([
      apiRef.current("action", { action: "mcp-access-list" }),
      apiRef.current("action", { action: "registry-list" }),
    ]);
    if (
      !Array.isArray(access.credentials) ||
      !access.endpoint ||
      !registry.projects
    )
      throw new Error("Access details are incomplete. Reload to try again.");
    if (alive.current) {
      setData(access);
      setProjects(registry.projects);
    }
  };
  const run = async (callback) => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      await callback();
    } catch (e) {
      if (alive.current)
        setError(e.message || "Request failed. Reload before retrying.");
    } finally {
      lock.current = false;
      if (alive.current) setBusy(false);
    }
  };
  useEffect(() => {
    alive.current = true;
    run(load);
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    onDirty(open);
    return () => onDirty(false);
  }, [open, onDirty]);
  useEffect(() => {
    onPending(busy);
    return () => onPending(false);
  }, [busy, onPending]);
  const change = (key, value) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const toggle = (key, value) => {
    const removing = draft[key].includes(value);
    setDraft((current) => ({
      ...current,
      [key]: removing
        ? current[key].filter((v) => v !== value)
        : [...current[key], value],
      ...(key === "scopes" && value === "projects:create" && removing
        ? {
            newProject: "",
            repositoryId: "",
            environments: current.environments.filter(
              (v) => JSON.parse(v)[0] !== current.newProject,
            ),
          }
        : {}),
      ...(key === "projects" && removing
        ? {
            environments: current.environments.filter(
              (v) => JSON.parse(v)[0] !== value,
            ),
          }
        : {}),
    }));
  };
  const close = () => {
    if (busy) return;
    setToken("");
    setOpen(false);
    setError("");
    setDraft(freshMcpDraft());
  };
  const create = (event) => {
    event.preventDefault();
    let values;
    try {
      values = mcpCredentialValues(draft, projects);
    } catch (e) {
      setError(e.message);
      return;
    }
    run(async () => {
      setUncertain(true);
      const result = await api("action", {
        action: "mcp-access-create",
        values,
      });
      if (!result.credential?.id || !result.token)
        throw new Error(
          "Creation outcome is unknown. Reload access and revoke any new credential before creating another. Tokens cannot be recovered.",
        );
      if (!alive.current) return;
      setToken(result.token);
      setUncertain(false);
      setData((current) => ({
        ...current,
        credentials: [result.credential, ...current.credentials],
      }));
    });
  };
  const targets = [
    ...draft.projects,
    ...(draft.newProject ? [draft.newProject] : []),
  ];
  const checks = (title, items, key) => (
    <Box component="fieldset" sx={{ border: 0, p: 0, m: 0 }}>
      <Typography component="legend" fontWeight={600} fontSize={13}>
        {title}
      </Typography>
      {items.map(([value, label, disabled]) => (
        <FormControlLabel
          key={value}
          sx={{ display: "flex", overflowWrap: "anywhere" }}
          control={
            <Checkbox
              checked={draft[key].includes(value)}
              disabled={busy || uncertain || disabled}
              onChange={() => toggle(key, value)}
            />
          }
          label={label}
        />
      ))}
    </Box>
  );
  return (
    <Box
      sx={{
        border: 1,
        borderColor: "divider",
        borderRadius: 1,
        p: 2,
        minWidth: 0,
      }}
    >
      <Stack spacing={2}>
        <Stack
          direction="row"
          justifyContent="space-between"
          gap={1}
          alignItems="center"
        >
          <Typography variant="h6" component="h2">
            MCP access
            <Hint label="About MCP access">
              Create a separate expiring credential for each client. Permissions
              and targets are fixed at creation. Revoke access when it is no
              longer needed.
            </Hint>
          </Typography>
          <Button
            disabled={busy || !data}
            onClick={() => {
              setDraft(freshMcpDraft());
              setError("");
              setUncertain(false);
              setOpen(true);
            }}
          >
            Create credential
          </Button>
        </Stack>
        {error && !open && !revoke && <Alert severity="error">{error}</Alert>}
        {message && (
          <Alert severity="success" role="status">
            {message}
          </Alert>
        )}
        {data && (
          <Typography fontSize={14} sx={{ overflowWrap: "anywhere" }}>
            Server URL: <Box component="code">{data.endpoint}</Box>
          </Typography>
        )}
        {!data && !error && (
          <Typography role="status">Loading MCP access…</Typography>
        )}
        {data?.credentials.length === 0 && (
          <Typography color="text.secondary">
            No credentials. Create one to connect an MCP client with specific
            permissions.
          </Typography>
        )}
        {data?.credentials.map((item) => (
          <Box
            key={item.id}
            sx={{ borderTop: 1, borderColor: "divider", pt: 1.5 }}
          >
            <Stack
              direction="row"
              justifyContent="space-between"
              alignItems="center"
              gap={1}
            >
              <Typography fontWeight={600} sx={{ overflowWrap: "anywhere" }}>
                {item.label}
              </Typography>
              <Button
                color="error"
                disabled={busy || !!item.revoked_at}
                onClick={() => {
                  setRevoke(item);
                  setError("");
                }}
              >
                {item.revoked_at ? "Revoked" : "Revoke"}
              </Button>
            </Stack>
            <Typography fontSize={13} color="text.secondary">
              {item.revoked_at
                ? `Revoked ${date(item.revoked_at)}`
                : `${item.expires_at * 1000 <= Date.now() ? "Expired" : "Expires"} ${date(item.expires_at)}`}{" "}
              · Last used: {date(item.last_used_at)}
            </Typography>
            <Typography fontSize={13} sx={{ overflowWrap: "anywhere" }}>
              Permissions: {item.scopes.join(", ")}
              <br />
              Projects: {item.allowed_projects.join(", ")}
              <br />
              Repository IDs: {item.allowed_repository_ids.join(", ")}
              <br />
              Environments:{" "}
              {item.allowed_environments
                .map(([p, e]) => `${p} / ${e}`)
                .join(", ") || "None"}
            </Typography>
          </Box>
        ))}
        <Box>
          <Button disabled={busy} onClick={() => run(load)}>
            {busy ? "Loading…" : "Reload MCP access"}
          </Button>
        </Box>
      </Stack>
      <Dialog
        open={open}
        onClose={close}
        fullWidth
        maxWidth="sm"
        aria-labelledby="mcp-create-title"
        slotProps={{ paper: { component: "form", onSubmit: create } }}
      >
        <DialogTitle id="mcp-create-title">
          {token ? "Save your MCP credential" : "Create MCP credential"}
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            {error && <Alert severity="error">{error}</Alert>}
            {token ? (
              <>
                <Alert severity="warning">
                  This token is shown once. Save it in your MCP client's secure
                  credential settings before closing. It cannot be recovered.
                </Alert>
                <Field
                  label="Server URL"
                  value={data.endpoint}
                  fullWidth
                  slotProps={{ input: { readOnly: true } }}
                />
                <Field
                  label="Bearer token"
                  value={token}
                  multiline
                  fullWidth
                  slotProps={{ input: { readOnly: true } }}
                />
                <Button
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(token);
                      setMessage("Token copied. Store it securely.");
                    } catch {
                      setError(
                        "Clipboard unavailable. Select and copy the token from the field.",
                      );
                    }
                  }}
                >
                  Copy token
                </Button>
              </>
            ) : (
              <>
                {uncertain && (
                  <Alert severity="warning">
                    A creation request was sent. Close and reload before trying
                    again. If a credential was created without receiving its
                    token, revoke it and create a replacement.
                  </Alert>
                )}
                <Field
                  label="Credential name"
                  required
                  autoFocus
                  fullWidth
                  value={draft.label}
                  disabled={busy || uncertain}
                  onChange={(e) => change("label", e.target.value)}
                  inputProps={{ maxLength: 80 }}
                />
                {checks(
                  "Permissions",
                  MCP_SCOPES.map((s) => [
                    s,
                    {
                      "projects:read": "Read projects",
                      "projects:create": "Create projects",
                      "services:read": "Read services",
                      "services:create": "Create services",
                      "services:start": "Start services",
                      "operations:read": "Read operations",
                      "domains:read": "Read domains",
                      "domains:create": "Create and recover domains",
                      "domains:adopt": "Adopt domain DNS records",
                      "databases:read": "Read accepted database metadata",
                      "databases:data:read":
                        "Read permitted database rows (requires metadata access)",
                    }[s],
                  ]),
                  "scopes",
                )}
                <Autocomplete
                  multiple
                  disableCloseOnSelect
                  options={Object.values(projects)}
                  value={draft.projects
                    .map((id) => projects[id])
                    .filter(Boolean)}
                  disabled={busy || uncertain}
                  getOptionLabel={(project) => project.label || project.id}
                  getOptionDisabled={(project) =>
                    !Number.isSafeInteger(project.repository_id) ||
                    project.repository_id <= 0
                  }
                  isOptionEqualToValue={(option, value) =>
                    option.id === value.id
                  }
                  filterOptions={(options, { inputValue }) =>
                    options.filter((project) =>
                      [
                        project.id,
                        project.label,
                        project.repository,
                        String(project.repository_id || ""),
                      ]
                        .join(" ")
                        .toLowerCase()
                        .includes(inputValue.toLowerCase()),
                    )
                  }
                  onChange={(_, selected) => {
                    const ids = selected.map((project) => project.id);
                    setDraft((current) => ({
                      ...current,
                      projects: ids,
                      environments: current.environments.filter((value) => {
                        const id = JSON.parse(value)[0];
                        return ids.includes(id) || id === current.newProject;
                      }),
                    }));
                  }}
                  renderOption={(props, project, { selected }) => {
                    const { key, ...optionProps } = props;
                    return (
                      <li key={key} {...optionProps}>
                        <Checkbox
                          checked={selected}
                          tabIndex={-1}
                          sx={{ mr: 1 }}
                        />
                        <Box sx={{ minWidth: 0, overflowWrap: "anywhere" }}>
                          <Typography fontSize={14}>
                            {project.label || project.id}
                          </Typography>
                          <Typography fontSize={12} color="text.secondary">
                            {project.repository}{" "}
                            {project.repository_id
                              ? `(#${project.repository_id})`
                              : "(repository ID not configured)"}
                          </Typography>
                        </Box>
                      </li>
                    );
                  }}
                  renderInput={(params) => (
                    <Field
                      {...params}
                      label="Existing projects and repositories"
                      placeholder={
                        draft.projects.length
                          ? undefined
                          : "Search projects or repositories"
                      }
                      fullWidth
                      helperText="Access is limited to the selected projects and their repository IDs."
                    />
                  )}
                />
                {!Object.keys(projects).length && (
                  <Typography color="text.secondary">
                    No projects are registered.
                  </Typography>
                )}
                {draft.scopes.includes("projects:create") && (
                  <>
                    <Field
                      label="New project slug (optional)"
                      fullWidth
                      disabled={busy || uncertain}
                      value={draft.newProject}
                      onChange={(e) => change("newProject", e.target.value)}
                      helperText="Grant creation access only to this exact project slug."
                    />
                    <Field
                      label="New project's GitHub repository ID"
                      fullWidth
                      disabled={busy || uncertain}
                      value={draft.repositoryId}
                      onChange={(e) => change("repositoryId", e.target.value)}
                      helperText="Use the numeric repository ID, not its name or URL."
                    />
                  </>
                )}
                {checks(
                  "Allowed environments",
                  targets.flatMap((p) =>
                    ["development", "production"].map((e) => [
                      JSON.stringify([p, e]),
                      `${p} / ${e}`,
                    ]),
                  ),
                  "environments",
                )}
                {!targets.length && (
                  <Typography color="text.secondary">
                    Choose a project to select its environments.
                  </Typography>
                )}
                <Field
                  select
                  label="Expires after"
                  value={draft.lifetime}
                  fullWidth
                  disabled={busy || uncertain}
                  onChange={(e) => change("lifetime", e.target.value)}
                >
                  {[1, 7, 30, 90].map((days) => (
                    <MenuItem key={days} value={days}>
                      {days} {days === 1 ? "day" : "days"}
                    </MenuItem>
                  ))}
                </Field>
              </>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={close}>
            {token ? "I saved the token — close" : "Cancel"}
          </Button>
          {!token && (
            <Button
              type="submit"
              variant="contained"
              disabled={busy || uncertain}
            >
              {busy ? "Creating…" : "Create credential"}
            </Button>
          )}
        </DialogActions>
      </Dialog>
      <Dialog
        open={!!revoke}
        onClose={() => !busy && setRevoke(null)}
        fullWidth
        maxWidth="xs"
        aria-labelledby="mcp-revoke-title"
      >
        <DialogTitle id="mcp-revoke-title">Revoke MCP credential</DialogTitle>
        <DialogContent dividers>
          {error && <Alert severity="error">{error}</Alert>}
          <Typography>
            Revoke “{revoke?.label}”? Clients using this token will lose access.
            This cannot be undone.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button disabled={busy} onClick={() => setRevoke(null)}>
            Cancel
          </Button>
          <Button
            color="error"
            variant="contained"
            disabled={busy}
            onClick={() =>
              run(async () => {
                const result = await api("action", {
                  action: "mcp-access-revoke",
                  credential_id: revoke.id,
                });
                if (!result.credential?.revoked_at)
                  throw new Error(
                    "Revocation could not be verified. Reload access before retrying.",
                  );
                if (!alive.current) return;
                setData((current) => ({
                  ...current,
                  credentials: current.credentials.map((item) =>
                    item.id === result.credential.id ? result.credential : item,
                  ),
                }));
                setRevoke(null);
                setMessage("Credential revoked.");
              })
            }
          >
            {busy ? "Revoking…" : "Revoke credential"}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
