export function creationRequest(previous, draft, requestId, rejected = false) {
  const values = {
    repository: draft.repository.trim(),
    label: draft.label.trim(),
  };
  const input = {
    action: "registry-create",
    project: draft.id,
    values,
    integration_ref: "github",
  };
  if (previous) {
    const { request_id, ...captured } = previous;
    if (JSON.stringify(captured) === JSON.stringify(input)) return previous;
    if (!rejected)
      throw Error(
        "Check the original creation request before changing its details.",
      );
  }
  return { ...input, request_id: requestId };
}

export function verifiedCreation(result, request) {
  const project = result?.project,
    operation = result?.operation;
  const environments = project?.environments;
  if (
    project?.id !== request.project ||
    typeof project.repository !== "string" ||
    project.repository.toLowerCase() !==
      request.values.repository.toLowerCase() ||
    !Number.isSafeInteger(project.repository_id) ||
    project.repository_id <= 0 ||
    operation?.status !== "succeeded" ||
    operation.resource_id !== request.project ||
    typeof operation.operation_id !== "string" ||
    !operation.operation_id ||
    !environments ||
    Object.keys(environments).sort().join(",") !== "development,production" ||
    Object.entries(environments).some(
      ([name, env]) =>
        env.name !== name ||
        env.state !== "not-provisioned" ||
        env.recipe_revision !== null,
    )
  )
    throw Error(
      "The creation response could not be verified. Check the original request to retrieve its saved result.",
    );
  return project;
}
