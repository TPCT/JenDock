export const MCP_SCOPES = [
  "projects:read",
  "projects:create",
  "services:read",
  "services:create",
  "services:start",
  "operations:read",
  "domains:read",
  "domains:create",
  "domains:adopt",
];
export const freshMcpDraft = () => ({
  label: "",
  scopes: ["projects:read", "services:read", "operations:read"],
  projects: [],
  environments: [],
  lifetime: 7,
  newProject: "",
  repositoryId: "",
});
export function mcpCredentialValues(draft, projects, now = Date.now()) {
  if (!draft.label.trim() || draft.label.trim().length > 80)
    throw new Error("Enter a name of 1–80 characters.");
  if (!draft.scopes.length || draft.scopes.some((s) => !MCP_SCOPES.includes(s)))
    throw new Error("Choose supported permissions.");
  if (![1, 7, 30, 90].includes(Number(draft.lifetime)))
    throw new Error("Choose an available expiration period.");
  const selected = [...new Set(draft.projects)];
  const repositories = selected.map((id) => {
    const p = projects[id];
    if (!p || !Number.isSafeInteger(p.repository_id) || p.repository_id <= 0)
      throw new Error(
        "Each selected project needs a verified numeric repository ID.",
      );
    return p.repository_id;
  });
  if (draft.newProject || draft.repositoryId) {
    if (!draft.scopes.includes("projects:create"))
      throw new Error("New project access requires projects:create.");
    if (
      !/^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(draft.newProject) ||
      projects[draft.newProject]
    )
      throw new Error(
        "Enter a unique new project slug using lowercase letters, numbers and hyphens.",
      );
    if (
      !/^[1-9]\d*$/.test(draft.repositoryId) ||
      !Number.isSafeInteger(Number(draft.repositoryId))
    )
      throw new Error("Enter a positive numeric GitHub repository ID.");
    selected.push(draft.newProject);
    repositories.push(Number(draft.repositoryId));
  }
  if (!selected.length) throw new Error("Choose at least one project.");
  const environments = draft.environments
    .map((v) => JSON.parse(v))
    .filter(([p]) => selected.includes(p));
  if (environments.some(([, e]) => !["development", "production"].includes(e)))
    throw new Error("Choose supported environments.");
  if (
    draft.scopes.some((s) => s.startsWith("services:") || s.startsWith("domains:")) &&
    !environments.length
  )
    throw new Error("Choose exact environments for service or domain access.");
  return {
    label: draft.label.trim(),
    scopes: [...draft.scopes],
    allowed_projects: selected,
    allowed_repository_ids: [...new Set(repositories)],
    allowed_environments: environments,
    expires_at: Math.floor(now / 1000) + Number(draft.lifetime) * 86400,
  };
}
