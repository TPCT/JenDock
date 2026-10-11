import React, { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Typography,
} from "@mui/material";
import { Visibility } from "./icons";
import { migrationSummary } from "./migrationStatus.js";

export default function MigrationStatus({ record, runStatus }) {
  const [open, setOpen] = useState(false);
  const summary = migrationSummary(record, runStatus);
  if (!summary) return null;
  return (
    <Box sx={{ mt: "var(--space-panel)" }}>
      <Alert
        severity={summary.severity}
        role="status"
        sx={{ "& .MuiAlert-message": { width: "100%" } }}
      >
        <Stack
          direction="row"
          alignItems="center"
          justifyContent="space-between"
          flexWrap="wrap"
          gap={1}
        >
          <span>
            <strong>Database migrations</strong> · {summary.label}
          </span>
          {summary.files.length > 0 && (
            <Button
              size="small"
              startIcon={<Visibility />}
              onClick={() => setOpen(true)}
            >
              Files ({summary.files.length})
            </Button>
          )}
        </Stack>
      </Alert>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        fullWidth
        maxWidth="sm"
        aria-labelledby="migration-files-title"
      >
        <DialogTitle id="migration-files-title">Migration files</DialogTitle>
        <DialogContent dividers>
          <Typography color="text.secondary" variant="body2" sx={{ mb: 2 }}>
            Files in this run’s recorded migration plan.
          </Typography>
          <Stack component="ul" gap={1} sx={{ m: 0, pl: 2 }}>
            {summary.files.map((file) => (
              <Typography
                component="li"
                key={file}
                sx={{
                  fontFamily: "monospace",
                  fontSize: 13,
                  overflowWrap: "anywhere",
                }}
              >
                {file}
              </Typography>
            ))}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setOpen(false)}>Close</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
