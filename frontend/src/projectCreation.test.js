import test from "node:test";
import assert from "node:assert/strict";
import { creationRequest, verifiedCreation } from "./projectCreation.js";
const draft = { id: "example", label: " Example ", repository: " Owner/Repo " };

test("creation retries retain original key and immutable payload until definite rejection", () => {
  const original = creationRequest(null, draft, "first");
  assert.equal(original.values.repository, "Owner/Repo");
  assert.strictEqual(creationRequest(original, draft, "discarded"), original);
  assert.throws(
    () =>
      creationRequest(original, { ...draft, repository: "Owner/Other" }, "new"),
    /original creation/,
  );
  const changed = creationRequest(
    original,
    { ...draft, repository: "Owner/Other" },
    "new",
    true,
  );
  assert.equal(changed.request_id, "new");
  assert.equal(original.values.repository, "Owner/Repo");
  assert.strictEqual(
    creationRequest(original, draft, "still-discarded", true),
    original,
  );
});

test("creation acceptance requires verified canonical repository and exactly two inactive environments", () => {
  const request = creationRequest(null, draft, "request");
  const result = {
    project: {
      id: "example",
      repository: "owner/repo",
      repository_id: 123,
      environments: {
        development: {
          name: "development",
          state: "not-provisioned",
          recipe_revision: null,
        },
        production: {
          name: "production",
          state: "not-provisioned",
          recipe_revision: null,
        },
      },
    },
    operation: {
      operation_id: "operation",
      status: "succeeded",
      resource_id: "example",
    },
  };
  assert.strictEqual(verifiedCreation(result, request), result.project);
  for (const change of [
    (r) => delete r.project.repository_id,
    (r) => (r.project.repository = "owner/different"),
    (r) => (r.operation.status = "running"),
    (r) => (r.operation.resource_id = "other"),
    (r) => (r.project.environments.production.state = "active"),
    (r) => delete r.project.environments.production,
    (r) => (r.project.environments.staging = { name: "staging" }),
  ]) {
    const invalid = structuredClone(result);
    change(invalid);
    assert.throws(
      () => verifiedCreation(invalid, request),
      /could not be verified/,
    );
  }
});
