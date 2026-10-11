import { retryRequest, observedAge } from "./workloadPanel.js";
export const targetKeys = [
  "project",
  "environment",
  "service_id",
  "container_id",
  "definition_digest",
  "adapter_revision",
];
export const sameTarget = (a, b) =>
  !!a && !!b && targetKeys.every((k) => a[k] === b[k]);
export function workloadResponse(response, target, role) {
  const w = response?.workload,
    o = w?.observation;
  if (!sameTarget(w, target) || w.role !== role || o?.protocol !== 1)
    throw Error("identity-changed");
  if (o.status !== "ok")
    throw Error(
      o.error?.code === "instrumentation_not_configured"
        ? "not-installed"
        : o.status === "unsupported"
          ? "unsupported"
          : "unavailable",
    );
  if (
    !["project", "environment", "service_id", "definition_digest"].every(
      (k) => o.scope?.[k] === target[k],
    )
  )
    throw Error("identity-changed");
  if (
    !o.result ||
    typeof o.result !== "object" ||
    observedAge(o.observed_at) === null ||
    !Number.isFinite(w.received_at)
  )
    throw Error("unavailable");
  return w;
}
export function retryStorageKey(owner, target) {
  if (!owner) throw Error("No authenticated owner");
  return (
    "workload-retry:" +
    [owner, target.project, target.environment, target.service_id]
      .map(encodeURIComponent)
      .join(":")
  );
}
export function restoreWorkloadRetry(storage, key, target) {
  const raw = storage.getItem(key);
  if (!raw) return null;
  const req = retryRequest(JSON.parse(raw));
  if (
    !["project", "environment", "service_id"].every((k) => req[k] === target[k])
  )
    throw Error("Saved retry belongs to another workload");
  return req;
}
export function retryOperation(response, request) {
  const op = response?.operation;
  if (
    !op ||
    op.request_id !== request.request_id ||
    ![
      "accepted",
      "dispatching",
      "recovery-required",
      "requeued",
      "rejected",
    ].includes(op.status) ||
    !Object.keys(request)
      .filter((k) => k !== "request_id")
      .every((k) => op.target?.[k] === request[k])
  )
    throw Error("The original retry identity could not be verified.");
  return op;
}
export function workloadFailure(error) {
  const code = error?.code || error?.message;
  return (
    {
      workload_identity_changed: "identity-changed",
      forbidden: "denied",
      invalid_credential: "denied",
      workload_stopped: "stopped",
      instrumentation_not_configured: "not-installed",
    }[code] ||
    ([
      "identity-changed",
      "stopped",
      "denied",
      "not-installed",
      "unsupported",
      "stale",
    ].includes(code)
      ? code
      : "unavailable")
  );
}

export function workloadViewKey(owner,target){return [owner,...targetKeys.map(k=>target[k])].map(encodeURIComponent).join(":");}
