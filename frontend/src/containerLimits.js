export const limitFields = [
  ["memory_mib", "RAM (MiB)", 16, 1048576],
  ["cpu", "CPU cores", 0.1, 1024],
];

export function limitProblems(draft) {
  return Object.fromEntries(
    limitFields.map(([key, label, min, max]) => {
      const raw = draft?.[key];
      const value = Number(raw);
      const invalid =
        (typeof raw !== "string" && typeof raw !== "number") ||
        String(raw).trim() === "" ||
        !Number.isFinite(value) ||
        value < min ||
        value > max;
      return [
        key,
        invalid
          ? `${label}: enter a value from ${min.toLocaleString("en-US")} to ${max.toLocaleString("en-US")}.`
          : "",
      ];
    }),
  );
}

// Once sent, retries retain the exact target and limits even if live observations change.
export function limitRequest(
  previous,
  { project, name, reviewed, draft },
  requestId,
) {
  if (previous) return previous;
  if (Object.values(limitProblems(draft)).some(Boolean))
    throw new Error("Invalid container limits.");
  const policy = reviewed.resource_policy;
  if (
    policy?.configured &&
    (!Number.isInteger(policy.revision) || policy.revision < 0)
  )
    throw new Error(
      "The resource budget revision is unavailable. Inspect the current limits before applying.",
    );
  return {
    action: "container-limits-save",
    ...(policy?.configured ? { policy_revision: policy.revision } : {}),
    project,
    container: name,
    container_id: reviewed.container_id,
    revision: reviewed.revision,
    request_id: requestId,
    limits: Object.fromEntries(
      limitFields.map(([key]) => [key, Number(draft[key])]),
    ),
  };
}
