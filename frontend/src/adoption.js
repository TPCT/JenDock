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
