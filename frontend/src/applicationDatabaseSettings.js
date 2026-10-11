import { endpointKey } from "./migrationSettings.js";
export { endpointKey };
export const applicationPrefix = "database-application-";
export function scopedApplicationBinding(value, project, environment, service) {
  if (
    !value ||
    value.project !== project ||
    value.environment !== environment ||
    value.service_id !== service ||
    value.definition?.service_id !== service ||
    value.definition?.project !== project ||
    value.definition?.environment !== environment ||
    !Number.isInteger(value.revision) ||
    value.revision < 1 ||
    !value.id ||
    !value.definition_digest
  )
    throw new Error(
      "Application database scope changed. Reload this container.",
    );
  return value;
}
export function applicationDraft(binding) {
  const mapping = binding?.definition.environment_mapping;
  return {
    endpoint: endpointKey(binding?.definition),
    credential: binding?.definition.secret_id || "",
    mode: mapping?.url ? "url" : "fields",
    mapping: mapping || {
      host: "DB_HOST",
      port: "DB_PORT",
      database: "DB_DATABASE",
      username: "DB_USERNAME",
      password: "DB_PASSWORD",
    },
  };
}
export function applicationValues(draft, endpoints, credentials, service) {
  const endpoint = endpoints.find(
    (row) => endpointKey(row.definition) === draft.endpoint,
  )?.definition;
  const account = credentials.find((row) => row.id === draft.credential);
  if (
    !endpoint ||
    !["postgresql", "mysql", "mariadb"].includes(endpoint.engine) ||
    endpoint.ownership?.kind !== "adopted-compose"
  )
    throw new Error("Choose a supported database in this environment.");
  if (!account?.enabled)
    throw new Error("Choose an enabled application account.");
  const keys =
    draft.mode === "url"
      ? ["url"]
      : ["host", "port", "database", "username", "password"];
  const environment_mapping = Object.fromEntries(
    keys.map((key) => [key, (draft.mapping[key] || "").trim()]),
  );
  const names = Object.values(environment_mapping);
  if (
    new Set(names).size !== names.length ||
    names.some(
      (name) =>
        !/^[A-Z][A-Z0-9_]{0,63}$/.test(name) ||
        /^(LD_|DYLD_)/.test(name) ||
        [
          "PATH",
          "HOME",
          "SHELL",
          "ENV",
          "BASH_ENV",
          "PYTHONPATH",
          "PYTHONHOME",
          "NODE_OPTIONS",
          "JAVA_TOOL_OPTIONS",
          "PHP_INI_SCAN_DIR",
          "PHPRC",
          "PERL5OPT",
          "RUBYOPT",
        ].includes(name),
    )
  )
    throw new Error(
      "Use distinct uppercase environment variable names for this connection.",
    );
  return {
    service_id: service,
    engine: endpoint.engine,
    ownership: { ...endpoint.ownership },
    database_name: endpoint.database_name,
    database_port: endpoint.database_port,
    secret_id: account.id,
    secret_revision: account.revision,
    environment_mapping,
  };
}
