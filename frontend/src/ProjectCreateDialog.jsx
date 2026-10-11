import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
  Link,
} from "@mui/material";
import Field from "./Field";
import Hint from "./Hint";
import { creationRequest, verifiedCreation } from "./projectCreation.js";

const slug = (value) =>
  value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/g, "");

export default function ProjectCreateDialog({
  open,
  onClose,
  api,
  onCreated,
  onIntegrations,
  draftStore,
  onDraftChange,
  onPending,
}) {
  const cached = draftStore?.current || {};
  const [label, setLabel] = useState(cached.label ?? "");
  const [repository, setRepository] = useState(cached.repository ?? "");
  const [id, setId] = useState(cached.id ?? "");
  const [customId, setCustomId] = useState(cached.customId ?? false);
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(cached.error ?? "");
  const [created, setCreated] = useState(cached.created || null);
  const createdId = created?.id;
  const [request, setRequest] = useState(cached.request || null);
  const [outcome, setOutcome] = useState(cached.outcome || "");
  const [errorCode, setErrorCode] = useState(cached.errorCode || "");
  const uncertain = outcome === "uncertain";
  useEffect(() => {
    const saved = {
      label,
      repository,
      id,
      customId,
      error,
      created,
      request,
      outcome,
      errorCode,
    };
    if (onDraftChange) onDraftChange(saved);
    else if (draftStore) draftStore.current = saved;
  }, [
    label,
    repository,
    id,
    customId,
    error,
    created,
    request,
    outcome,
    errorCode,
    draftStore,
    onDraftChange,
  ]);
  useEffect(() => {
    onPending?.(busy && !created);
    return () => onPending?.(false);
  }, [busy, created, onPending]);
  const lock = useRef(false);
  const nameInput = useRef(null);
  const repositoryInput = useRef(null);
  const idInput = useRef(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current && createdId) {
      setLabel("");
      setRepository("");
      setId("");
      setCustomId(false);
      setSubmitted(false);
      setError("");
      setCreated(null);
      setRequest(null);
      setOutcome("");
      setErrorCode("");
    }
    wasOpen.current = open;
  }, [open, createdId]);
  const errors = {
    label: !label.trim()
      ? "Enter a project name."
      : label.trim().length > 120 || /[\x00-\x1f]/.test(label)
        ? "Use a name of up to 120 printable characters."
        : "",
    repository: !repository.trim()
      ? "Enter a repository, such as owner/repo."
      : !/^[A-Za-z0-9][A-Za-z0-9-]{0,99}\/[A-Za-z0-9_.-]{1,100}$/.test(
            repository.trim(),
          )
        ? "Use owner/repo without a URL or spaces."
        : "",
    id: !/^[a-z0-9][a-z0-9-]{0,63}$/.test(id)
      ? "Use 1–64 lowercase letters, numbers or hyphens, starting with a letter or number."
      : "",
  };
  const close = () => {
    if (!lock.current) onClose();
  };
  const finish = async () => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      await onCreated(createdId);
      onClose();
    } catch {
      setError(
        "Project created, but it could not be opened. Retry opening the saved project.",
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  const submit = async (event) => {
    event.preventDefault();
    if (lock.current) return;
    if (createdId) {
      finish();
      return;
    }
    setSubmitted(true);
    const invalid = Object.keys(errors).find((key) => errors[key]);
    if (invalid) {
      ({ label: nameInput, repository: repositoryInput, id: idInput })[
        invalid
      ].current?.focus();
      return;
    }
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const payload = creationRequest(
        request,
        { id, label, repository },
        crypto.randomUUID(),
        outcome === "rejected",
      );
      setRequest(payload);
      setOutcome("uncertain");
      // Persist the immutable request before the first network attempt.
      onDraftChange?.({
        label,
        repository,
        id,
        customId,
        request: payload,
        outcome: "uncertain",
        created: null,
      });
      const result = await api("action", payload);
      setCreated(verifiedCreation(result, payload));
      setOutcome("succeeded");
      setErrorCode("");
    } catch (error) {
      const definite =
        Number.isInteger(error.status) &&
        error.status >= 400 &&
        error.status < 500 &&
        error.code !== "idempotency_conflict";
      setOutcome(definite ? "rejected" : "uncertain");
      setErrorCode(error.code || "");
      setError(
        error.message ||
          "The result could not be confirmed. Check the original request before changing its details.",
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onClose={close}
      fullWidth
      maxWidth="sm"
      aria-labelledby="project-create-title"
      slotProps={{
        paper: { component: "form", onSubmit: submit, noValidate: true },
      }}
    >
      <DialogTitle id="project-create-title">
        New project{" "}
        <Hint label="About project creation">
          Verifies repository access through the saved GitHub connection and
          creates Development and Production. Both environments remain inactive;
          no containers are provisioned.
        </Hint>
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={3}>
          {error && (
            <Alert severity="error" sx={{ overflowWrap: "anywhere" }}>
              {error}
            </Alert>
          )}
          {createdId && (
            <Alert severity="success">
              Project created. GitHub repository verified.
            </Alert>
          )}
          {created && (
            <Stack spacing={1}>
              <Link
                href={`https://github.com/${created.repository}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                {created.repository}
              </Link>
              <Typography color="text.secondary">
                Repository ID: {created.repository_id}
              </Typography>
              <Typography>Development · Not provisioned</Typography>
              <Typography>Production · Not provisioned</Typography>
            </Stack>
          )}
          {uncertain && (
            <Typography color="text.secondary">
              Your original request is retained. Check its result before editing
              or starting another project.
            </Typography>
          )}
          {!created && (
            <>
              <Field
                label="Project name"
                autoFocus
                fullWidth
                required
                value={label}
                inputRef={nameInput}
                disabled={busy || uncertain || !!createdId}
                error={submitted && !!errors.label}
                helperText={submitted ? errors.label || undefined : undefined}
                inputProps={{ maxLength: 120 }}
                onChange={(event) => {
                  setLabel(event.target.value);
                  if (!customId) setId(slug(event.target.value));
                }}
              />
              <Field
                label="Repository"
                placeholder="owner/repo"
                fullWidth
                required
                value={repository}
                inputRef={repositoryInput}
                disabled={busy || uncertain || !!createdId}
                error={submitted && !!errors.repository}
                helperText={
                  submitted ? errors.repository || undefined : undefined
                }
                inputProps={{
                  maxLength: 201,
                  autoCapitalize: "none",
                  spellCheck: false,
                }}
                onChange={(event) => setRepository(event.target.value)}
              />
              <Field
                label="Project ID"
                fullWidth
                required
                value={id}
                inputRef={idInput}
                disabled={busy || uncertain || !!createdId}
                error={submitted && !!errors.id}
                helperText={submitted ? errors.id || undefined : undefined}
                inputProps={{
                  maxLength: 64,
                  autoCapitalize: "none",
                  spellCheck: false,
                }}
                onChange={(event) => {
                  setCustomId(true);
                  setId(event.target.value);
                }}
              />
            </>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button disabled={busy} onClick={close}>
          {created ? "Close" : "Cancel"}
        </Button>
        {errorCode === "integration_unavailable" && onIntegrations && (
          <Button
            disabled={busy}
            onClick={() => {
              onClose();
              onIntegrations();
            }}
          >
            Connect GitHub
          </Button>
        )}
        <Button variant="contained" type="submit" disabled={busy}>
          {busy
            ? created
              ? "Opening…"
              : "Verifying and creating…"
            : createdId
              ? "Open project"
              : uncertain
                ? "Check original request"
                : "Create project"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
