import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Breadcrumbs,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  InputAdornment,
  Stack,
  Typography,
} from "@mui/material";
import { FileDirectoryIcon, FileIcon } from "@primer/octicons-react";
import { Refresh, Chevron } from "./icons";
import Field from "./Field";
import Hint from "./Hint";
import {
  fileBreadcrumbs,
  fileSize,
  verifyFileResponse,
} from "./containerFiles.js";
const storageLabel = (value) =>
  ({
    image: "Image",
    "writable-layer": "Container writable layer",
    mount: "Mounted storage",
    unknown: "Storage origin unverified",
  })[value] || "Storage origin unverified";
export default function ContainerFiles({
  api,
  project,
  name,
  containerId,
  enabled,
  reason,
}) {
  const [directoryPath, setDirectoryPath] = useState("");
  const [path, setPath] = useState(""),
    [listing, setListing] = useState(null),
    [preview, setPreview] = useState(null),
    [selected, setSelected] = useState(null),
    [busy, setBusy] = useState(false),
    [previewBusy, setPreviewBusy] = useState(false),
    [error, setError] = useState(""),
    [previewError, setPreviewError] = useState(""),
    [reload, setReload] = useState(0),
    [cursor, setCursor] = useState(null);
  const apiRef = useRef(api);
  apiRef.current = api;
  const generation = useRef(0),
    previewGeneration = useRef(0),
    rootRef = useRef(null);
  useEffect(() => {
    rootRef.current = null;
    setPath("");
    setDirectoryPath("");
    setCursor(null);
    setListing(null);
    setPreview(null);
    setSelected(null);
  }, [project, name, containerId]);
  useEffect(() => {
    const request = ++generation.current;
    ++previewGeneration.current;
    setPreview(null);
    setSelected(null);
    setPreviewError("");
    setPreviewBusy(false);
    setListing(null);
    setError("");
    setBusy(false);
    if (!enabled || !containerId) return;
    setBusy(true);
    apiRef
      .current("action", {
        action: "container-files-list",
        project,
        container: name,
        container_id: containerId,
        path,
        limit: 100,
        ...(cursor !== null ? { cursor } : {}),
      })
      .then((result) => {
        if (request !== generation.current) return;
        verifyFileResponse(result, containerId, path, rootRef.current);
        if (result.capability?.supported === false) {
          setListing(result);
          return;
        }
        rootRef.current = result.root;
        setListing(result);
      })
      .catch((failure) => {
        if (request === generation.current)
          setError(
            failure.message ||
              "Files could not be loaded. Reload to try again.",
          );
      })
      .finally(() => {
        if (request === generation.current) setBusy(false);
      });
    return () => {
      ++generation.current;
      ++previewGeneration.current;
    };
  }, [project, name, containerId, enabled, path, cursor, reload]);
  const navigate = (next) => {
    try {
      fileBreadcrumbs(next);
      setDirectoryPath(next);
      setPath(next);
      setCursor(null);
    } catch (failure) {
      setError(failure.message);
    }
  };
  const openPreview = async (entry) => {
    if (!enabled || busy || !entry.previewable) return;
    const request = ++previewGeneration.current;
    setSelected(entry);
    setPreview(null);
    setPreviewError("");
    setPreviewBusy(true);
    try {
      const result = await apiRef.current("action", {
        action: "container-files-preview",
        project,
        container: name,
        container_id: containerId,
        path: entry.path,
      });
      if (request !== previewGeneration.current) return;
      verifyFileResponse(
        result,
        containerId,
        entry.path,
        rootRef.current,
        true,
      );
      setPreview(result);
    } catch (failure) {
      if (request === previewGeneration.current)
        setPreviewError(failure.message || "This file cannot be previewed.");
    } finally {
      if (request === previewGeneration.current) setPreviewBusy(false);
    }
  };
  const closePreview = () => {
    ++previewGeneration.current;
    setSelected(null);
    setPreview(null);
    setPreviewError("");
    setPreviewBusy(false);
  };
  const crumbs = fileBreadcrumbs(path),
    supported = listing?.capability?.supported !== false;
  return (
    <Stack spacing={2}>
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        gap={1}
      >
        <Typography variant="h6" component="h2">
          Application files
          <Hint label="About application files">
            Browse only the configured application root. Text previews are
            limited to 64 KiB. Secret paths, symlinks and unsupported file types
            cannot be opened. Storage origin is shown only when verified.
          </Hint>
        </Typography>
        <Button
          startIcon={<Refresh />}
          disabled={!enabled || busy}
          onClick={() => {
            setCursor(null);
            setReload((v) => v + 1);
          }}
        >
          Reload
        </Button>
      </Stack>
      {!enabled && (
        <Alert severity="warning">
          {reason ||
            "File access is unavailable. Reload container details to verify its identity."}
        </Alert>
      )}
      {error && <Alert severity="error">{error}</Alert>}
      <Box
        component="form"
        onSubmit={(event) => {
          event.preventDefault();
          if (enabled && !busy) navigate(directoryPath);
        }}
      >
        <Field
          label="Directory path"
          value={directoryPath}
          fullWidth
          disabled={!enabled || busy}
          onChange={(event) => setDirectoryPath(event.target.value)}
          inputProps={{
            maxLength: 1024,
            autoCapitalize: "none",
            spellCheck: false,
          }}
          InputProps={{
            endAdornment: (
              <InputAdornment position="end">
                <IconButton
                  type="submit"
                  aria-label="Open directory"
                  disabled={!enabled || busy}
                >
                  <Chevron />
                </IconButton>
              </InputAdornment>
            ),
          }}
        />
      </Box>
      {enabled && (
        <Breadcrumbs
          aria-label="Application file location"
          sx={{ overflowWrap: "anywhere" }}
        >
          {crumbs.map((crumb, index) =>
            index === crumbs.length - 1 ? (
              <Typography key={crumb.path} fontSize={14} aria-current="page">
                {crumb.label}
              </Typography>
            ) : (
              <Button
                key={crumb.path}
                size="small"
                disabled={busy}
                onClick={() => navigate(crumb.path)}
              >
                {crumb.label}
              </Button>
            ),
          )}
        </Breadcrumbs>
      )}
      {busy && <Typography role="status">Loading directory…</Typography>}
      {!supported && (
        <Alert severity="info">
          {listing.capability.reason ||
            "File browsing is not supported for this container."}
        </Alert>
      )}
      {listing?.root && (
        <Typography
          fontSize={12}
          color="text.secondary"
          sx={{ overflowWrap: "anywhere" }}
        >
          Application root: <Box component="code">{listing.root}</Box>
          {Number.isFinite(listing.observed)
            ? ` · Observed ${new Date(listing.observed * 1000).toLocaleString()}`
            : ""}
        </Typography>
      )}
      {supported && listing?.entries && (
        <>
          <Box
            sx={{
              border: 1,
              borderColor: "divider",
              borderRadius: 1,
              minWidth: 0,
            }}
          >
            {listing.entries.length === 0 && (
              <Typography color="text.secondary" sx={{ p: 2 }}>
                No visible files in this directory. Protected entries may be
                omitted.
              </Typography>
            )}
            {listing.entries.map((entry) => {
              const directory = entry.type === "directory",
                allowed = directory ? !entry.blocked_reason : entry.previewable;
              return (
                <Stack
                  key={entry.path}
                  direction="row"
                  gap={1.5}
                  alignItems="center"
                  sx={{
                    p: 1.5,
                    borderBottom: 1,
                    borderColor: "divider",
                    "&:last-child": { borderBottom: 0 },
                    minWidth: 0,
                  }}
                >
                  <Box
                    component={directory ? FileDirectoryIcon : FileIcon}
                    size={16}
                    sx={{ flexShrink: 0, color: "text.secondary" }}
                  />
                  <Box sx={{ minWidth: 0, flex: 1 }}>
                    <Button
                      disabled={!enabled || busy || !allowed}
                      onClick={() =>
                        directory ? navigate(entry.path) : openPreview(entry)
                      }
                      sx={{
                        p: 0,
                        minWidth: 0,
                        textAlign: "left",
                        justifyContent: "flex-start",
                        overflowWrap: "anywhere",
                        maxWidth: "100%",
                      }}
                    >
                      {entry.name}
                    </Button>
                    <Typography
                      fontSize={12}
                      color="text.secondary"
                      sx={{ overflowWrap: "anywhere" }}
                    >
                      {entry.blocked_reason ||
                        (!directory && !entry.previewable
                          ? "Preview unavailable"
                          : storageLabel(entry.storage))}
                    </Typography>
                  </Box>
                  {!directory && (
                    <Typography
                      fontSize={12}
                      color="text.secondary"
                      sx={{ flexShrink: 0 }}
                    >
                      {fileSize(entry.size)}
                    </Typography>
                  )}
                </Stack>
              );
            })}
          </Box>
          {(cursor !== null || listing.next_cursor != null) && (
            <Stack
              direction="row"
              justifyContent="space-between"
              alignItems="center"
              gap={1}
            >
              <Button
                disabled={busy || cursor === null}
                onClick={() => setCursor(null)}
              >
                First page
              </Button>
              <Typography fontSize={12} color="text.secondary">
                Up to 100 entries per page
              </Typography>
              <Button
                disabled={busy || listing.next_cursor == null}
                onClick={() => setCursor(listing.next_cursor)}
              >
                Next page
              </Button>
            </Stack>
          )}
        </>
      )}
      <Dialog
        open={!!selected}
        onClose={closePreview}
        fullWidth
        maxWidth="md"
        aria-labelledby="file-preview-title"
      >
        <DialogTitle id="file-preview-title" sx={{ overflowWrap: "anywhere" }}>
          Preview · {selected?.name}
        </DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2}>
            {previewBusy && (
              <Typography role="status">Loading text preview…</Typography>
            )}
            {previewError && <Alert severity="error">{previewError}</Alert>}
            {preview && (
              <>
                <Typography
                  fontSize={12}
                  color="text.secondary"
                  sx={{ overflowWrap: "anywhere" }}
                >
                  {preview.path} · {fileSize(preview.size)} ·{" "}
                  {storageLabel(preview.storage)}
                </Typography>
                {preview.truncated && (
                  <Alert severity="info">
                    This is a bounded excerpt; the file contains additional
                    content.
                  </Alert>
                )}
                <Box
                  component="pre"
                  tabIndex={0}
                  aria-label="File text preview"
                  sx={{
                    m: 0,
                    p: 2,
                    bgcolor: "action.hover",
                    fontSize: 13,
                    lineHeight: 1.6,
                    whiteSpace: "pre-wrap",
                    overflowWrap: "anywhere",
                    maxHeight: "60vh",
                    overflow: "auto",
                  }}
                >
                  {preview.text || "(Empty file)"}
                </Box>
              </>
            )}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={closePreview}>Close</Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
