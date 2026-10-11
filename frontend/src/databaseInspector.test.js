import test from "node:test";
import assert from "node:assert/strict";
import {
  verifyDatabaseScope,
  invalidateDatabaseEvidence,
} from "./databaseInspector.js";
const expected = {
  project: "app",
  environment: "development",
  binding: { id: "binding", revision: 2, definition_digest: "a".repeat(64) },
  containerId: "b".repeat(64),
  candidateFingerprint: "c".repeat(64),
};
const result = {
  scope: {
    project: "app",
    environment: "development",
    binding_id: "binding",
    binding_revision: 2,
    definition_digest: "a".repeat(64),
    container_id: "b".repeat(64),
    candidate_fingerprint: "c".repeat(64),
  },
};
test("database evidence requires exact selected identity", () => {
  assert.equal(verifyDatabaseScope(result, expected), result);
  for (const key of Object.keys(result.scope)) {
    assert.throws(() =>
      verifyDatabaseScope(
        { scope: { ...result.scope, [key]: "wrong" } },
        expected,
      ),
    );
  }
  assert.throws(() => verifyDatabaseScope({}, expected));
});

test("failed reads invalidate evidence but ambiguous acceptance retains its replay identity", () => {
  for (const status of [401, 403, 409])
    assert.equal(invalidateDatabaseEvidence({ status }, true), true);
  assert.equal(
    invalidateDatabaseEvidence({ code: "identity_changed" }, true),
    true,
  );
  assert.equal(invalidateDatabaseEvidence(new Error("network"), true), false);
  assert.equal(invalidateDatabaseEvidence({ status: 503 }, true), false);
  assert.equal(
    invalidateDatabaseEvidence(new Error("unexpected response")),
    true,
  );
});
