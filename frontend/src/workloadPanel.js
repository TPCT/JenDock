// Client presentation and recovery only; the worker remains the authority.
const retryFields = [
  "request_id",
  "project",
  "environment",
  "service_id",
  "container_id",
  "definition_digest",
  "adapter_revision",
  "connection_id",
  "queue",
  "failed_job_id",
  "failure_revision",
];
export function retryRequest(value) {
  if (
    !value ||
    Object.keys(value).some((key) => !retryFields.includes(key)) ||
    !["development", "production"].includes(value.environment) ||
    !Number.isInteger(value.adapter_revision) ||
    value.adapter_revision < 1 ||
    retryFields
      .filter((key) => key !== "adapter_revision")
      .some(
        (key) =>
          typeof value[key] !== "string" ||
          !value[key] ||
          value[key].length > 512,
      )
  )
    throw new Error(
      "The failed job target is incomplete. Reload its current observation.",
    );
  return Object.fromEntries(retryFields.map((key) => [key, value[key]]));
}
export function saveRetryRequest(storage, key, value) {
  const request = retryRequest(value);
  storage.setItem(key, JSON.stringify(request));
  return request;
}
export function restoreRetryRequest(storage, key, scope) {
  const value = storage.getItem(key);
  if (!value) return null;
  const request = retryRequest(JSON.parse(value));
  for (const name of ["project", "environment", "service_id"])
    if (request[name] !== scope[name])
      throw new Error("The saved retry belongs to another workload.");
  // Keep the originally captured container and failure revisions. Recovery must
  // never silently retarget an old operation to a replacement container.
  return request;
}
export function observedAge(observedAt, now = Date.now() / 1000) {
  if (typeof observedAt === "string") observedAt = Date.parse(observedAt) / 1000;
  if (!Number.isFinite(observedAt) || observedAt > now + 5) return null;
  return Math.max(0, now - observedAt);
}
export function workloadStateMessage(state) {
  return (
    {
      loading: "Loading workload observations…",
      empty: "No records were returned.",
      unsupported: "This workload adapter is not supported.",
      "not-installed": "Workload instrumentation is not installed.",
      denied: "You do not have access to this workload.",
      unavailable: "Workload observations are unavailable. Retry loading.",
      stopped:
        "This container is stopped. Reading this panel will not start it.",
      stale: "The workload observation is stale. Reload before taking action.",
      "identity-changed": "The container identity changed. Reload its details.",
      "recovery-required":
        "The previous retry outcome is unresolved. Check its receipt before retrying another job.",
    }[state] || "Workload status is unavailable."
  );
}
