export function databaseRowPolicy(schemas, policies) {
  const allowed = schemas
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!allowed.length || new Set(allowed).size !== allowed.length)
    throw new Error("Enter unique permitted schema names, one per line.");
  const row_columns = Object.create(null);
  for (const policy of policies) {
    const columns = policy.columns
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean);
    if (
      !allowed.includes(policy.schema) ||
      !policy.table.trim() ||
      !columns.length ||
      columns.length > 20 ||
      new Set(columns).size !== columns.length
    )
      throw new Error(
        "Each row policy needs a permitted schema, table, and up to 20 unique columns.",
      );
    row_columns[policy.schema] ||= Object.create(null);
    if (row_columns[policy.schema][policy.table])
      throw new Error("Each table may have only one row policy.");
    row_columns[policy.schema][policy.table] = columns;
  }

  return { allowed_schemas: allowed, row_columns };
}

export function currentSavedBinding(
  response,
  { id, project, environment, serviceReference },
) {
  const value = response?.binding;
  if (
    !value ||
    value.id !== id ||
    value.project !== project ||
    value.environment !== environment ||
    value.definition?.project !== project ||
    value.definition?.environment !== environment ||
    value.definition?.ownership?.kind !== "adopted-compose" ||
    value.definition.ownership.reference !== serviceReference ||
    !Number.isInteger(value.revision) ||
    value.revision < 1
  )
    throw new Error(
      "Saved binding scope changed. Reload the current environment before continuing.",
    );
  return value;
}

export function currentSavedCredential(response, { id, project, environment }) {
  const value = response?.credential;
  if (
    !value ||
    value.id !== id ||
    value.project !== project ||
    value.environment !== environment ||
    !Number.isInteger(value.revision) ||
    value.revision < 1 ||
    typeof value.enabled !== "boolean"
  )
    throw new Error(
      "Saved credential scope changed. Reload current credentials before continuing.",
    );
  return value;
}
export function mergeCredential(previous, current) {
  return previous.some((value) => value.id === current.id)
    ? previous.map((value) => (value.id === current.id ? current : value))
    : [...previous, current];
}

export function bindingDraftConflict(base, current) {
  if (!base || !current) return Boolean(base) !== Boolean(current);
  return (
    base.id !== current.id ||
    base.revision !== current.revision ||
    base.definition_digest !== current.definition_digest
  );
}
export function bindingDraftRevisions(base, current) {
  if (bindingDraftConflict(base, current))
    throw new Error(
      "Binding changed while this draft was open. Discard the draft and reload before saving.",
    );
  return {
    expected_revision: base?.revision || 0,
    policy_revision: (base?.definition.policy_revision || 0) + 1,
  };
}

export function assertSaveScope(saved, current) {
  if (
    !saved ||
    ["project", "environment", "serviceReference", "containerId"].some(
      (key) => saved[key] !== current[key],
    )
  )
    throw new Error(
      "The pending save belongs to a different database context. Return to its original container to reconcile it.",
    );
}
export function saveProvenUncommitted(error) {
  // HTTP status cannot prove absence of an earlier committed replay record.
  return error?.outcome === "not_committed";
}

export const supportedDatabaseEngines = ["postgresql", "mysql", "mariadb"];
export function candidateDialects(candidate) {
  // Adoption records mysql for the endpoint family, including Laravel clients
  // connected to MariaDB. Only authenticated helper evidence proves the dialect.
  if (["mysql", "mysqlfamily"].includes(candidate?.engine)) return ["mysql", "mariadb"];
  return supportedDatabaseEngines.includes(candidate?.engine)
    ? [candidate.engine]
    : [];
}
export function databaseDraftDefaults(binding, candidate) {
  if (binding)
    return {
      engine: binding.definition.engine,
      schemas: binding.definition.allowed_schemas.join("\n"),
    };
  return {
    engine:
      ["mysql", "mysqlfamily"].includes(candidate?.engine)
        ? ""
        : candidateDialects(candidate)[0] || "",
    schemas:
      candidate?.engine === "postgresql" ? "public" : candidate?.database || "",
  };
}
export function validateDatabaseDialect(candidate, engine, schemas) {
  if (!candidateDialects(candidate).includes(engine))
    throw new Error(
      "Choose the database dialect that is actually running: MySQL or MariaDB. Verification will check your selection.",
    );
  if (
    engine !== "postgresql" &&
    (schemas.length !== 1 || schemas[0] !== candidate.database)
  )
    throw new Error(
      "MySQL and MariaDB inspection permits only the registered database as its schema.",
    );
  return engine;
}
