import test from "node:test";
import assert from "node:assert/strict";
import { appendVersions, validateVersionPage, versionTimestamp } from "./serviceVersions.js";
const artifact = (id = "v1") => ({ artifact_id: id, source_environment: "development", recorded_services: [] });
const page = () => ({ project: "demo", environment: "development", evidence: "recorded", versions: [artifact()], next_cursor: null });
test("rejects histories from a different project or environment", () => {
  assert.throws(() => validateVersionPage(page(), "other", "development"));
  assert.throws(() => validateVersionPage(page(), "demo", "production"));
  const mixed = page(); mixed.versions[0].source_environment = "production";
  assert.throws(() => validateVersionPage(mixed, "demo", "development"));
  assert.equal(validateVersionPage(page(), "demo", "development").versions.length, 1);
});
test("malformed records and pagination do not become a misleading empty state", () => {
  for (const changed of [{ versions: null }, { next_cursor: 2 }, { evidence: "live" }, { versions: [{ artifact_id: "v1", source_environment: "development" }] }]) {
    assert.throws(() => validateVersionPage({ ...page(), ...changed }, "demo", "development"));
  }
});
test("pagination deduplicates overlapping versions while retaining chronological order", () => {
  const existing = [artifact("new"), artifact("overlap")];
  assert.deepEqual(appendVersions(existing, [artifact("overlap"), artifact("older"), artifact("older")]).map(v => v.artifact_id), ["new", "overlap", "older"]);
  assert.equal(existing.length, 2);
});
test("missing or invalid publication timestamps remain explicitly unrecorded", () => {
  assert.equal(versionTimestamp(null), "Time not recorded");
  assert.equal(versionTimestamp("broken"), "Time not recorded");
  assert.match(versionTimestamp(1700000000), /2023/);
});
