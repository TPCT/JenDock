export function verifyDatabaseScope(
  result,
  { project, environment, binding, containerId, candidateFingerprint },
) {
  const scope = result?.scope;
  if (
    !scope ||
    scope.project !== project ||
    scope.environment !== environment ||
    scope.binding_id !== binding?.id ||
    scope.binding_revision !== binding?.revision ||
    scope.definition_digest !== binding?.definition_digest ||
    scope.container_id !== containerId ||
    !/^[a-f0-9]{64}$/.test(scope.candidate_fingerprint || "") ||
    (candidateFingerprint &&
      scope.candidate_fingerprint !== candidateFingerprint)
  ) {
    const error = new Error(
      "Database response identity changed. Reload and verify this binding again.",
    );
    error.code = "identity_changed";
    throw error;
  }
  return result;
}

export function invalidateDatabaseEvidence(error, preserveAcceptance = false) {
  return (
    [401, 403, 409].includes(error?.status) ||
    error?.code === "identity_changed" ||
    (!error?.status && !preserveAcceptance)
  );
}
