export const activeStates = [
  "queued",
  "validation-queued",
  "building",
  "testing",
  "deploying",
  "recovering",
];
export const retryStates = [
  "failed",
  "interrupted",
  "rolled-back",
  "cancelled",
  "needs-review",
  "superseded",
  "awaiting-validation",
  "auto-deploy-disabled",
  "validated",
];
export const stateNames = {
  "validation-queued": "Validation queued",
  "awaiting-validation": "Not started",
  "auto-deploy-disabled": "Auto-deploy off",
  "needs-review": "Migration review",
  "rolled-back": "Rolled back",
  succeeded: "Deployed",
  validated: "Build verified",
};
export const statusLabel = (s) =>
  stateNames[s] || String(s || "Unknown").replace(/^./, (c) => c.toUpperCase());
export const date = (v) =>
  v
    ? new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(v * 1000))
    : "Not observed";
export const servicesFor = (data, p) =>
  (data?.snapshots?.containers?.value || []).filter(
    (c) =>
      (p?.container_prefixes || []).some((prefix) =>
        c.Names.startsWith(prefix),
      ) ||
      (p?.image_prefixes || []).some((prefix) => c.Image.startsWith(prefix)),
  );
export const stale = (data, failed = false) =>
  failed ||
  (!data?.preview &&
    (!data?.snapshots?.containers ||
      Date.now() / 1000 - data.snapshots.containers.updated > 90 ||
      data.snapshots.worker?.value.state !== "running"));
export function branchState(data, id) {
  const o = data?.snapshots?.["branches:" + id];
  return {
    branches: o?.value?.branches || {},
    error: o?.value?.error,
    updated: o?.updated,
    fresh: !!o && !o.value?.error && Date.now() / 1000 - o.updated <= 600,
  };
}
export function buildPayload({ project, branch, sha, mode, retry, requestId }) {
  return {
    action: retry ? "retry" : "build",
    project,
    branch,
    sha,
    mode,
    job_id: retry?.id,
    request_id: requestId,
  };
}
