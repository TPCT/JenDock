import { limitProblems } from "./containerLimits.js";
export const serviceTerminal = (status) =>
  ["stopped", "running", "recovery-required", "failed", "cancelled"].includes(
    status,
  );
export function serviceProblems(draft) {
  return {
    ...limitProblems(draft),
    name: /^[a-z][a-z0-9-]{0,47}$/.test(draft.name || "")
      ? ""
      : "Use 1–48 lowercase letters, numbers or hyphens, starting with a letter.",
    artifact_id: draft.artifact_id ? "" : "Choose a verified build artifact.",
  };
}
export function serviceRequest(
  previous,
  project,
  environment,
  draft,
  catalog,
  requestId,
) {
  if (previous) return previous;
  if (Object.values(serviceProblems(draft)).some(Boolean))
    throw Error("Check the service name, artifact and limits.");
  const artifact = catalog.templates.find(
    (item) => item.artifact_id === draft.artifact_id,
  );
  if (!artifact || !artifact.desired_states?.includes(draft.desired_state))
    throw Error(
      "This artifact or desired state is no longer available. Reload the available artifacts.",
    );
  if (
    !Number.isInteger(catalog.environment_revision) ||
    catalog.environment_revision < 1 ||
    !Number.isInteger(catalog.policy_revision) ||
    catalog.policy_revision < 0
  )
    throw Error(
      "Environment or budget revision is unavailable. Reload before creating a container.",
    );
  return {
    action: "service-create",
    project,
    environment,
    request_id: requestId,
    values: {
      name: draft.name,
      artifact_id: artifact.artifact_id,
      template_id: artifact.id,
      template_revision: artifact.revision,
      environment_revision: catalog.environment_revision,
      policy_revision: catalog.policy_revision,
      resources: {
        memory_mib: Number(draft.memory_mib),
        cpu: Number(draft.cpu),
      },
      desired_state: draft.desired_state,
    },
  };
}
export function serviceResultMatches(result, request) {
  return !!(
    result?.service &&
    result?.operation?.operation_id &&
    result.service.project === request.project &&
    result.service.environment === request.environment &&
    result.service.name === request.values.name &&
    result.service.operation_id === result.operation.operation_id &&
    [
      "queued",
      "provisioning",
      "running",
      "stopped",
      "recovery-required",
      "failed",
      "cancelled",
    ].includes(result.operation.status)
  );
}
function safeRequest(request, project, environment) {
  if (!request) return null;
  const v = request.values;
  if (
    request.action !== "service-create" ||
    request.project !== project ||
    request.environment !== environment ||
    !/^[A-Za-z0-9-]{16,80}$/.test(request.request_id || "") ||
    !v ||
    !/^[a-z][a-z0-9-]{0,47}$/.test(v.name || "") ||
    !["artifact_id", "template_id"].every(
      (key) =>
        typeof v[key] === "string" && v[key].length > 0 && v[key].length < 256,
    ) ||
    !["environment_revision", "template_revision"].every(
      (key) => Number.isInteger(v[key]) && v[key] >= 1,
    ) ||
    !Number.isInteger(v.policy_revision) ||
    v.policy_revision < 0 ||
    !["stopped", "running"].includes(v.desired_state) ||
    Object.values(limitProblems(v.resources)).some(Boolean)
  )
    throw Error("Invalid saved request");
  return {
    action: "service-create",
    project,
    environment,
    request_id: request.request_id,
    values: {
      name: v.name,
      artifact_id: v.artifact_id,
      template_id: v.template_id,
      template_revision: v.template_revision,
      environment_revision: v.environment_revision,
      policy_revision: v.policy_revision,
      resources: { memory_mib: v.resources.memory_mib, cpu: v.resources.cpu },
      desired_state: v.desired_state,
    },
  };
}
const prefix = "monitor-service-creation:";
const cacheKey = (owner, project, environment) =>
  prefix + [owner, project, environment].map(encodeURIComponent).join(":");
export function saveServiceDraft(
  storage,
  owner,
  project,
  environment,
  draft,
  request,
) {
  if (!owner) return false;
  try {
    storage.setItem(
      cacheKey(owner, project, environment),
      JSON.stringify({
        version: 1,
        owner,
        project,
        environment,
        draft: {
          name: draft.name,
          artifact_id: draft.artifact_id,
          memory_mib: draft.memory_mib,
          cpu: draft.cpu,
          desired_state: draft.desired_state,
        },
        request: safeRequest(request, project, environment),
      }),
    );
    return true;
  } catch {
    return false;
  }
}
export function restoreServiceDraft(storage, owner, project, environment) {
  try {
    const raw = storage.getItem(cacheKey(owner, project, environment));
    if (!raw || raw.length > 12000) return null;
    const value = JSON.parse(raw),
      d = value.draft;
    if (
      value.version !== 1 ||
      value.owner !== owner ||
      value.project !== project ||
      value.environment !== environment ||
      !d ||
      !["name", "artifact_id", "memory_mib", "cpu", "desired_state"].every(
        (k) => typeof d[k] === "string" && d[k].length < 256,
      ) ||
      !["stopped", "running"].includes(d.desired_state)
    )
      return null;
    const request = safeRequest(value.request, project, environment);
    if (request && JSON.stringify(request) !== JSON.stringify(value.request))
      return null;
    return {
      draft: Object.fromEntries(
        ["name", "artifact_id", "memory_mib", "cpu", "desired_state"].map(
          (k) => [k, d[k]],
        ),
      ),
      request,
    };
  } catch {
    return null;
  }
}
export function clearServiceDrafts(storage, owner) {
  try {
    for (let index = storage.length - 1; index >= 0; index--) {
      const key = storage.key(index);
      if (
        key?.startsWith(
          owner ? prefix + encodeURIComponent(owner) + ":" : prefix,
        )
      )
        storage.removeItem(key);
    }
  } catch {}
}
