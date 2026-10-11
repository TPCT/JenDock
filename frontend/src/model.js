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
export function buildPayload({
  project,
  branch,
  sha,
  mode,
  retry,
  requestId,
  target,
}) {
  const scoped =
    target && ["development", "production"].includes(target.environment);
  if (target && !scoped && target.environment !== "legacy-unclassified")
    throw Error("Choose an explicit build target.");
  const revisionKey = target?.source_revision != null ? 'source_revision' : 'recipe_revision';
  if (
    scoped &&
    ["project_revision", "environment_revision", revisionKey].some(
      (key) => !Number.isInteger(target[key]) || target[key] < 1,
    )
  )
    throw Error("Refresh the environment target before building.");
  if (
    retry?.target?.environment &&
    target?.environment !== retry.target.environment
  )
    throw Error("A retry must retain the original environment.");
  return {
    action: retry ? "retry" : "build",
    project,
    branch,
    sha,
    mode,
    job_id: retry?.id,
    request_id: requestId,
    ...(scoped
      ? {
          environment: target.environment,
          project_revision: target.project_revision,
          environment_revision: target.environment_revision,
          [revisionKey]: target[revisionKey],
        }
      : {}),
  };
}

export function workspaceEnvironment(value) {
  return value === 'development' || value === 'production' ? value : null;
}
export function containerRoute(project, name, environment, tab) {
  const query = new URLSearchParams();
  if (workspaceEnvironment(environment)) query.set('environment', environment);
  if (['details', 'variables', 'terminal', 'logs'].includes(tab)) query.set('tab', tab);
  return `container/${encodeURIComponent(project)}/${encodeURIComponent(name)}${query.size ? '?' + query : ''}`;
}
export function projectReturnRoute(project, environment) {
  return `project/${encodeURIComponent(project)}${workspaceEnvironment(environment) ? '?environment=' + environment + '&tab=containers' : ''}`;
}

export function projectTabIndex(tab) {
  return Math.max(0, ['overview', 'containers', 'deployments', 'settings', 'domains'].indexOf(tab));
}
