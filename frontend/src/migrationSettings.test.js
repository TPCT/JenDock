import test from "node:test";
import assert from "node:assert/strict";
import {
  endpointKey,
  migrationDraft,
  migrationValues,
  sameMigrationRevision,
  scopedMigrationBinding,
} from "./migrationSettings.js";
const definition = {
  project: "app",
  environment: "development",
  engine: "postgresql",
  database_name: "app",
  database_port: 5432,
  ownership: { kind: "adopted-compose", reference: "db", revision: 1 },
  secret_id: "migrator",
  framework: "laravel",
  migration_paths: ["database/migrations"],
};
const binding = {
  id: "binding",
  project: "app",
  environment: "development",
  revision: 1,
  definition_digest: "hash",
  definition,
};
const credentials = [{ id: "migrator", enabled: true, revision: 2 }];
test("migration configuration copies endpoint identity and uses separate current account revision", () => {
  const values = migrationValues(
    migrationDraft(binding),
    [binding],
    credentials,
  );
  assert.equal(values.secret_revision, 2);
  assert.equal(values.grammar, "laravel12-stock-postgresql-v1");
  assert.equal(values.ledger_schema, "public");
  assert.deepEqual(values.ownership, definition.ownership);
  assert.notEqual(values.ownership, definition.ownership);
  assert.equal(
    migrationValues(
      {
        ...migrationDraft(binding),
        framework: "alembic",
        paths: "migrations/versions",
      },
      [binding],
      credentials,
    ).grammar,
    null,
  );
});
test("missing disabled accounts and wrong endpoints cannot create misleading configuration", () => {
  assert.throws(
    () => migrationValues(migrationDraft(binding), [binding], []),
    /enabled/,
  );
  assert.throws(
    () =>
      migrationValues(
        migrationDraft(binding),
        [binding],
        [{ ...credentials[0], enabled: false }],
      ),
    /enabled/,
  );
  assert.throws(
    () =>
      migrationValues(
        { ...migrationDraft(binding), endpoint: "wrong" },
        [binding],
        credentials,
      ),
    /PostgreSQL/,
  );
});
test("directory validation rejects traversal overlap duplicate and empty paths", () => {
  for (const paths of [
    "",
    "../db",
    ".git",
    "migrations\nmigrations",
    "migrations\nmigrations/versions",
    "/migrations",
  ]) {
    assert.throws(
      () =>
        migrationValues(
          { ...migrationDraft(binding), paths },
          [binding],
          credentials,
        ),
      /directories/,
    );
  }
});
test("metadata scope and revision checks reject stale or cross-environment saves", () => {
  assert.equal(scopedMigrationBinding(binding, "app", "development"), binding);
  assert.equal(scopedMigrationBinding(null, "app", "development"), null);
  assert.throws(
    () => scopedMigrationBinding(binding, "app", "production"),
    /scope/,
  );
  assert.equal(
    sameMigrationRevision(binding, { ...binding, revision: 2 }),
    false,
  );
  assert.equal(
    sameMigrationRevision(binding, { ...binding, definition_digest: "other" }),
    false,
  );
  assert.equal(
    sameMigrationRevision(binding, { ...binding, status: "credential-stale" }),
    true,
  );
  assert.equal(sameMigrationRevision(null, binding), false);
  assert.equal(
    endpointKey(definition),
    endpointKey({ ...definition, secret_id: "reader" }),
  );
});
