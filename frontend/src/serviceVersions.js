export function validateVersionPage(result, project, environment) {
  if (result?.project !== project || result?.environment !== environment ||
      result?.evidence !== "recorded" || !Array.isArray(result?.versions) ||
      !(result.next_cursor === null || typeof result.next_cursor === "string")) {
    throw new Error("Version history did not match this environment. Reload to try again.");
  }
  if (result.versions.some(version => !version?.artifact_id ||
      version.source_environment !== environment || !Array.isArray(version.recorded_services))) {
    throw new Error("Version history contains incomplete records. Reload to try again.");
  }
  return result;
}

// A newly published version can shift pagination between requests. Keep each
// immutable artifact once without losing the server's newest-first ordering.
export function appendVersions(current, incoming) {
  const seen = new Set(current.map(version => version.artifact_id));
  return [...current, ...incoming.filter(version => {
    if (seen.has(version.artifact_id)) return false;
    seen.add(version.artifact_id);
    return true;
  })];
}

export function versionTimestamp(value) {
  if (!value) return "Time not recorded";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "Time not recorded" : date.toLocaleString("en", {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}
