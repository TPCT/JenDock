import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
} from "@mui/material";
import Field from "./Field";
import Hint from "./Hint";

const slug = (value) =>
  value
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/g, "");

export default function ProjectCreateDialog({ open, onClose, api, onCreated }) {
  const [label, setLabel] = useState("");
  const [repository, setRepository] = useState("");
  const [id, setId] = useState("");
  const [customId, setCustomId] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [createdId, setCreatedId] = useState(null);
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
      setCreatedId(null);
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
  const finish = () => {
    onCreated(createdId);
    onClose();
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
    let result;
    try {
      result = await api("action", {
        action: "registry-create",
        project: id,
        values: { repository: repository.trim(), label: label.trim() },
      });
      if (!result.project?.id || result.project.id !== id)
        throw new Error(
          "The response could not be verified. Check the project list before retrying.",
        );
      setCreatedId(result.project.id);
    } catch (error) {
      setError(
        error.message ||
          "The project could not be created. Your entries are retained.",
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
    if (result?.project?.id === id) {
      onCreated(id);
      onClose();
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
          Creates a project with Development and Production settings.
          Environments stay inactive until deployment is configured. Repository
          access is not verified by this step.
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
              Project created. Open it to continue setup.
            </Alert>
          )}
          <Field
            label="Project name"
            autoFocus
            fullWidth
            required
            value={label}
            inputRef={nameInput}
            disabled={busy || !!createdId}
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
            disabled={busy || !!createdId}
            error={submitted && !!errors.repository}
            helperText={submitted ? errors.repository || undefined : undefined}
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
            disabled={busy || !!createdId}
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
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button disabled={busy} onClick={close}>
          Cancel
        </Button>
        <Button variant="contained" type="submit" disabled={busy}>
          {busy ? "Creating…" : createdId ? "Open project" : "Create project"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
