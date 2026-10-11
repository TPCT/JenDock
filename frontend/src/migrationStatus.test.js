import test from "node:test";
import assert from "node:assert/strict";
import { migrationSummary } from "./migrationStatus.js";
test("missing migration observations never imply success", () => {
  assert.equal(migrationSummary(undefined), null);
  assert.equal(
    migrationSummary({ state: "unexpected", files: [] }).label,
    "Status unavailable",
  );
});
test("only verified observations report completion, including no-op plans", () => {
  assert.equal(
    migrationSummary({ state: "verified", files: [] }).label,
    "No pending migrations",
  );
  assert.equal(
    migrationSummary({ state: "verified", files: ["one.php"] }).label,
    "Verified",
  );
  assert.equal(
    migrationSummary({ state: "planned", files: [] }).label,
    "Plan ready",
  );
});
test("uncertain execution does not offer approval or unsafe retry", () => {
  assert.equal(
    migrationSummary({ state: "recovery-required", files: ["one.php"] }).label,
    "Reconciling database state",
  );
  assert.equal(
    migrationSummary({ state: "failed", files: ["one.php"] }).severity,
    "error",
  );
});
test("terminal runs do not advertise pre-execution work as active", () => {
  for (const status of [
    "failed",
    "cancelled",
    "canceled",
    "superseded",
    "validated",
    "succeeded",
  ]) {
    for (const state of ["captured", "source-ready", "planned"]) {
      const summary = migrationSummary({ state, files: ["new.php"] }, status);
      assert.equal(summary.label, "Not run · stopped before execution");
      assert.equal(summary.severity, "info");
      assert.deepEqual(summary.files, ["new.php"]);
    }
  }
  assert.equal(
    migrationSummary({ state: "planned", files: [] }, "deploying").label,
    "Plan ready",
  );
});
test("terminal parent does not conceal uncertain execution or erase verified database facts", () => {
  assert.equal(
    migrationSummary({ state: "recovery-required", files: [] }, "failed").label,
    "Reconciling database state",
  );
  assert.equal(
    migrationSummary({ state: "applying", files: [] }, "failed").label,
    "Applying migrations",
  );
  assert.equal(
    migrationSummary({ state: "verified", files: ["new.php"] }, "failed").label,
    "Verified",
  );
  assert.equal(
    migrationSummary({ state: "verified", files: [] }, "failed").label,
    "No pending migrations",
  );
  assert.equal(
    migrationSummary({ state: "failed", files: ["new.php"] }, "failed")
      .severity,
    "error",
  );
  assert.equal(
    migrationSummary({ state: "not-configured", files: [] }, "failed").label,
    "Not configured",
  );
});
