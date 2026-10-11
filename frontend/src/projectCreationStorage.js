const PREFIX = "monitor-project-creation:";
const ownerValid = (owner) =>
  typeof owner === "string" && owner.length > 0 && owner.length <= 128;
const key = (owner) => PREFIX + encodeURIComponent(owner);
const string = (value, max) =>
  typeof value === "string" &&
  value.length <= max &&
  !/[\x00-\x1f]/.test(value);
function sanitize(value) {
  if (
    !value ||
    typeof value !== "object" ||
    !string(value.label, 120) ||
    !string(value.repository, 201) ||
    !string(value.id, 64)
  )
    return null;
  const outcome = ["", "uncertain", "rejected", "succeeded"].includes(
    value.outcome,
  )
    ? value.outcome
    : null;
  if (outcome === null) return null;
  let request = null;
  if (value.request) {
    const source = value.request;
    if (
      source.action !== "registry-create" ||
      typeof source.project !== "string" ||
      typeof source.request_id !== "string" ||
      typeof source.values?.repository !== "string" ||
      !/^[a-z0-9][a-z0-9-]{0,63}$/.test(source.project || "") ||
      !/^[A-Za-z0-9-]{16,80}$/.test(source.request_id || "") ||
      source.integration_ref !== "github" ||
      !string(source.values?.label, 120) ||
      !/^[A-Za-z0-9][A-Za-z0-9-]{0,99}\/[A-Za-z0-9_.-]{1,100}$/.test(
        source.values?.repository || "",
      )
    )
      return null;
    request = {
      action: "registry-create",
      project: source.project,
      values: {
        repository: source.values.repository,
        label: source.values.label,
      },
      integration_ref: "github",
      request_id: source.request_id,
    };
  }
  if ((outcome === "uncertain" || outcome === "succeeded") && !request)
    return null;
  if (
    outcome === "uncertain" &&
    (value.id !== request.project ||
      value.label.trim() !== request.values.label ||
      value.repository.trim() !== request.values.repository)
  )
    return null;
  let created = null;
  if (value.created) {
    const record = value.created;
    if (
      !request ||
      record.id !== request.project ||
      !Number.isSafeInteger(record.repository_id) ||
      record.repository_id <= 0 ||
      typeof record.repository !== "string" ||
      record.repository.toLowerCase() !==
        request.values.repository.toLowerCase() ||
      !record.environments ||
      Object.keys(record.environments).sort().join(",") !==
        "development,production"
    )
      return null;
    const environments = {};
    for (const name of ["development", "production"]) {
      const env = record.environments[name];
      if (
        env?.name !== name ||
        env.state !== "not-provisioned" ||
        env.recipe_revision !== null
      )
        return null;
      environments[name] = {
        name,
        state: "not-provisioned",
        recipe_revision: null,
      };
    }
    created = {
      id: record.id,
      repository: record.repository,
      repository_id: record.repository_id,
      environments,
    };
  }
  if (outcome === "succeeded" && !created) return null;
  return {
    label: value.label,
    repository: value.repository,
    id: value.id,
    customId: value.customId === true,
    request,
    outcome,
    created,
  };
}
export function storeCreation(storage, owner, draft, now = Date.now()) {
  if (!ownerValid(owner)) return;
  try {
    const safe = sanitize(draft);
    if (!safe) return;
    storage.setItem(
      key(owner),
      JSON.stringify({ version: 1, owner, updated: now, draft: safe }),
    );
  } catch {
    /* Storage denial must not turn a successful API call into a failure. */
  }
}
export function restoreCreation(storage, owner, now = Date.now()) {
  if (!ownerValid(owner)) return null;
  try {
    const raw = storage.getItem(key(owner));
    if (!raw || raw.length > 16000) return null;
    const entry = JSON.parse(raw);
    if (
      entry.version !== 1 ||
      entry.owner !== owner ||
      !Number.isFinite(entry.updated) ||
      entry.updated > now
    )
      return null;
    return sanitize(entry.draft);
  } catch {
    return null;
  }
}
export function clearCreation(storage, owner) {
  try {
    if (ownerValid(owner)) storage.removeItem(key(owner));
    else
      for (let i = storage.length - 1; i >= 0; i--) {
        const item = storage.key(i);
        if (item?.startsWith(PREFIX)) storage.removeItem(item);
      }
  } catch {
    /* Sign-out must still finish when storage is unavailable. */
  }
}

export function creationSessionStorage() {
  try {
    return globalThis.sessionStorage;
  } catch {
    return null;
  }
}
