export function selectionPayload(selection = {}) {
  const result = {};
  if (selection.recipe_revision != null) result.recipe_revision = selection.recipe_revision;
  if (selection.services != null) result.services = [...selection.services].sort();
  return result;
}
export function reviewedSelection(preview) {
  return selectionPayload({ recipe_revision: preview.selected_recipe_revision, services: preview.selected_services });
}
export function matchesReview(preview, project, environment, selection) {
  return preview?.project === project && preview?.environment === environment &&
    JSON.stringify(reviewedSelection(preview)) === JSON.stringify(selectionPayload(selection));
}
export function linkedEnvironment(result, project, environment, fingerprint) {
  const target = result?.project?.environments?.[environment];
  return result?.project?.id === project && result?.receipt?.project === project &&
    result?.receipt?.environment === environment && /^[a-f0-9]{64}$/.test(fingerprint || '') &&
    result?.receipt?.fingerprint === fingerprint && Number.isInteger(target?.recipe_revision) &&
    ['active', 'stopped', 'degraded'].includes(target?.state);
}

// Keep only the request identity, never database/container inspection details.
export function adoptionRecoveryKey(owner, project, environment) {
  if (!owner || !project || !['development', 'production'].includes(environment))
    throw new Error('A signed-in environment is required for recovery.');
  return `adoption-request:${JSON.stringify([owner, project, environment])}`;
}
export function adoptionCheckpoint(preview) {
  return { project: preview.project, environment: preview.environment,
    fingerprint: preview.fingerprint, ...reviewedSelection(preview) };
}
export function restoreAdoption(storage, key, project, environment) {
  const raw = storage.getItem(key);
  if (raw == null) return null;
  const value = JSON.parse(raw);
  if (value.project !== project || value.environment !== environment ||
      !/^[a-f0-9]{64}$/.test(value.fingerprint || '') ||
      (value.recipe_revision != null && (!Number.isInteger(value.recipe_revision) || value.recipe_revision < 1)) ||
      (value.services != null && (!Array.isArray(value.services) || !value.services.length ||
        value.services.some(item => typeof item !== 'string' || !item.length) || new Set(value.services).size !== value.services.length)))
    throw new Error('Saved linking request is invalid.');
  return { project, environment, fingerprint: value.fingerprint,
    ...selectionPayload(value) };
}
