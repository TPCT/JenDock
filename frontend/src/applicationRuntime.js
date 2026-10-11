export function attachmentPin(binding) {
  if (
    !binding?.enabled ||
    binding.status !== "configured" ||
    !binding.id ||
    !Number.isInteger(binding.revision) ||
    !binding.definition_digest
  )
    throw new Error(
      "Save an enabled connection with current credentials before applying it.",
    );
  return {
    binding_id: binding.id,
    revision: binding.revision,
    digest: binding.definition_digest,
  };
}
export function sameAttachment(a, b) {
  return (
    !!a &&
    !!b &&
    a.binding_id === b.binding_id &&
    a.revision === b.revision &&
    a.digest === b.digest
  );
}
export function restoreAttachmentOperation(value) {
  try {
    const op = JSON.parse(value);
    if (
      op?.kind !== "environment-save" ||
      !op.databaseAttachment ||
      typeof op.requestId !== "string" ||
      typeof op.containerId !== "string" ||
      typeof op.revision !== "string" ||
      !op.submitted ||
      Object.keys(op.changes || {}).length ||
      typeof op.databaseAttachment.binding_id !== "string" ||
      !Number.isInteger(op.databaseAttachment.revision) ||
      typeof op.databaseAttachment.digest !== "string"
    )
      return null;
    return {
      kind: "environment-save",
      requestId: op.requestId,
      containerId: op.containerId,
      revision: op.revision,
      databaseAttachment: {
        binding_id: op.databaseAttachment.binding_id,
        revision: op.databaseAttachment.revision,
        digest: op.databaseAttachment.digest,
      },
      changes: {},
      submitted: true,
    };
  } catch {
    return null;
  }
}
export function attachmentOperationRecord(rows, operation, project, container) {
  return (
    rows?.find(
      (row) =>
        row.request_id === operation?.requestId &&
        row.project === project &&
        row.container === container &&
        row.operation === "environment-save",
    ) || null
  );
}

export function persistAttachmentOperation(storage, key, operation) {
  const safe = restoreAttachmentOperation(
    JSON.stringify({ ...operation, submitted: true }),
  );
  if (!safe) throw new Error("Invalid application connection operation.");
  storage.setItem(key, JSON.stringify(safe));
  return safe;
}
