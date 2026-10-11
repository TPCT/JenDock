export const migrationPrefix = "database-migration-";
export function scopedMigrationBinding(value, project, environment) {
  if (value === null) return null;
  if (
    !value ||
    value.project !== project ||
    value.environment !== environment ||
    value.definition?.project !== project ||
    value.definition?.environment !== environment ||
    !Number.isInteger(value.revision) ||
    value.revision < 1 ||
    !value.id ||
    !value.definition_digest
  )
    throw new Error(
      "Migration configuration scope changed. Reload this environment.",
    );
  return value;
}
export function endpointKey(definition) {
  if (!definition) return "";
  return JSON.stringify([
    definition.engine,
    definition.database_name,
    definition.database_port,
    definition.ownership?.kind,
    definition.ownership?.reference,
    definition.ownership?.revision,
  ]);
}
export function migrationDraft(binding) {
  return {
    endpoint: endpointKey(binding?.definition),
    framework: binding?.definition.framework || "laravel",
    paths:
      binding?.definition.migration_paths.join("\n") || "database/migrations",
    credential: binding?.definition.secret_id || "",
  };
}
export function migrationValues(draft, endpoints, credentials) {
  const endpoint = endpoints.find(
    (value) => endpointKey(value.definition) === draft.endpoint,
  );
  const credential = credentials.find((value) => value.id === draft.credential);
  if (!endpoint || endpoint.definition.engine !== "postgresql")
    throw new Error("Choose a PostgreSQL database for this environment.");
  if (!credential?.enabled)
    throw new Error("Choose an enabled migration account.");
  if (!["laravel", "alembic"].includes(draft.framework))
    throw new Error("Choose Laravel or Alembic.");
  const paths = draft.paths
    .split("\n")
    .map((v) => v.trim())
    .filter(Boolean);
  if (
    !paths.length ||
    paths.length > 8 ||
    new Set(paths).size !== paths.length ||
    paths.some(
      (p) =>
        p.length > 240 ||
        !/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)*$/.test(p) ||
        p.split("/").some((part) => [".", "..", ".git"].includes(part)),
    ) ||
    paths.some((a) => paths.some((b) => a !== b && a.startsWith(b + "/")))
  )
    throw new Error(
      "Enter distinct repository directories, one per line, without parent paths.",
    );
  const d = endpoint.definition;
  return {
    engine: d.engine,
    adapter_version: 1,
    ownership: { ...d.ownership },
    database_name: d.database_name,
    database_port: d.database_port,
    secret_id: credential.id,
    secret_revision: credential.revision,
    framework: draft.framework,
    grammar:
      draft.framework === "laravel" ? "laravel12-stock-postgresql-v1" : null,
    migration_paths: paths,
    ledger_schema: "public",
  };
}
export function sameMigrationRevision(first, second) {
  return (
    (!first && !second) ||
    !!(
      first &&
      second &&
      first.id === second.id &&
      first.revision === second.revision &&
      first.definition_digest === second.definition_digest
    )
  );
}
