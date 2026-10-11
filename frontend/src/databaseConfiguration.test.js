import test from "node:test";
import assert from "node:assert/strict";
import { databaseRowPolicy } from "./databaseConfiguration.js";
test("binding policy is metadata only until explicit columns are configured", () => {
  const metadata = databaseRowPolicy("public", []);
  assert.equal(JSON.stringify(metadata.row_columns), "{}");
  const rows = databaseRowPolicy("public", [
    { schema: "public", table: "people", columns: "id, name" },
  ]);
  assert.deepEqual(rows.row_columns.public.people, ["id", "name"]);
  for (const policies of [
    [{ schema: "private", table: "people", columns: "id" }],
    [{ schema: "public", table: "people", columns: "id,id" }],
    [{ schema: "public", table: "people", columns: "" }],
  ])
    assert.throws(() => databaseRowPolicy("public", policies));
});
test("literal schema and table names cannot mutate policy prototypes", () => {
  const policy = databaseRowPolicy("__proto__", [
    { schema: "__proto__", table: "constructor", columns: "id" },
  ]);
  assert.equal(
    JSON.stringify(policy.row_columns),
    '{"__proto__":{"constructor":["id"]}}',
  );
  assert.equal(Object.getPrototypeOf(policy.row_columns), null);
});

test("saved receipt selects reloaded current binding including revoked or disabled state", async () => {
  const { currentSavedBinding } = await import("./databaseConfiguration.js");
  const scope = {
    id: "saved",
    project: "app",
    environment: "development",
    serviceReference: "db",
  };
  const binding = {
    ...scope,
    revision: 4,
    enabled: false,
    status: "disabled",
    definition: {
      project: "app",
      environment: "development",
      ownership: { kind: "adopted-compose", reference: "db" },
    },
  };
  assert.equal(currentSavedBinding({ binding }, scope), binding);
  assert.equal(
    currentSavedBinding(
      { binding: { ...binding, status: "credential-stale" } },
      scope,
    ).status,
    "credential-stale",
  );
  for (const key of ["id", "project", "environment"])
    assert.throws(() =>
      currentSavedBinding({ binding: { ...binding, [key]: "foreign" } }, scope),
    );
  assert.throws(() =>
    currentSavedBinding(
      {
        binding: {
          ...binding,
          definition: {
            ...binding.definition,
            ownership: { kind: "adopted-compose", reference: "foreign" },
          },
        },
      },
      scope,
    ),
  );
});

test("saved credential reload merges later pages and replaces rotated revisions without duplication", async () => {
  const { currentSavedCredential, mergeCredential } =
    await import("./databaseConfiguration.js");
  const scope = { id: "new", project: "app", environment: "development" };
  const current = { ...scope, revision: 2, enabled: true };
  assert.equal(currentSavedCredential({ credential: current }, scope), current);
  const page = [{ id: "existing", revision: 1 }];
  const merged = mergeCredential(page, current);
  assert.equal(merged.length, 2);
  assert.equal(merged[0], page[0]);
  const revoked = { ...current, revision: 3, enabled: false };
  assert.equal(mergeCredential(merged, revoked).length, 2);
  assert.equal(mergeCredential(merged, revoked)[1], revoked);
  assert.throws(() =>
    currentSavedCredential(
      { credential: { ...current, environment: "production" } },
      scope,
    ),
  );
});

test("status refresh cannot advance the binding draft CAS baseline", async () => {
  const { bindingDraftConflict, bindingDraftRevisions } =
    await import("./databaseConfiguration.js");
  const base = {
    id: "binding",
    revision: 2,
    definition_digest: "old",
    definition: { policy_revision: 3 },
  };
  const refreshed = {
    ...base,
    revision: 3,
    definition_digest: "new",
    definition: { policy_revision: 4 },
  };
  assert.equal(bindingDraftConflict(base, refreshed), true);
  assert.throws(() => bindingDraftRevisions(base, refreshed));
  assert.deepEqual(
    bindingDraftRevisions(base, { ...base, status: "credential-stale" }),
    { expected_revision: 2, policy_revision: 4 },
  );
  assert.deepEqual(bindingDraftRevisions(refreshed, refreshed), {
    expected_revision: 3,
    policy_revision: 5,
  });
});

test("authorization and conflict errors retain replay identity and replay cannot cross scope", async () => {
  const { assertSaveScope, saveProvenUncommitted } =
    await import("./databaseConfiguration.js");
  for (const status of [400, 401, 403, 409, 503])
    assert.equal(saveProvenUncommitted({ status }), false);
  assert.equal(
    saveProvenUncommitted({ status: 409, outcome: "not_committed" }),
    true,
  );
  const scope = {
    project: "app",
    environment: "development",
    serviceReference: "db",
    containerId: "a",
  };
  assert.doesNotThrow(() => assertSaveScope(scope, { ...scope }));
  for (const key of Object.keys(scope))
    assert.throws(() => assertSaveScope(scope, { ...scope, [key]: "changed" }));
});
