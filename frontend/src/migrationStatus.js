const states = {
  "not-configured": ["Not configured", "info"],
  captured: ["Waiting for build", "info"],
  "source-ready": ["Checking pending migrations", "info"],
  planned: ["Plan ready", "info"],
  applying: ["Applying migrations", "info"],
  "recovery-required": ["Reconciling database state", "warning"],
  verified: ["Verified", "success"],
  failed: ["Migration stage failed", "error"],
};
const terminalRuns = new Set([
  "failed",
  "cancelled",
  "canceled",
  "superseded",
  "validated",
  "succeeded",
]);
const beforeExecution = new Set(["captured", "source-ready", "planned"]);
export function migrationSummary(record, runStatus) {
  if (!record) return null;
  const files = Array.isArray(record.files)
    ? record.files.filter((value) => typeof value === "string")
    : [];
  if (terminalRuns.has(runStatus) && beforeExecution.has(record.state)) {
    return {
      label: "Not run · stopped before execution",
      severity: "info",
      files,
    };
  }
  const [label, severity] = states[record.state] || [
    "Status unavailable",
    "warning",
  ];
  return {
    label:
      record.state === "verified" && files.length === 0
        ? "No pending migrations"
        : label,
    severity,
    files,
  };
}
