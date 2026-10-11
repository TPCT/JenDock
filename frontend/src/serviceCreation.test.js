import test from "node:test";
import assert from "node:assert/strict";
import {
  serviceProblems,
  serviceResultMatches,
  serviceRequest,
  serviceTerminal,
  saveServiceDraft,
  restoreServiceDraft,
  clearServiceDrafts,
} from "./serviceCreation.js";
const draft = () => ({
  name: "web",
  artifact_id: "artifact-123",
  memory_mib: "128",
  cpu: "0.25",
  desired_state: "stopped",
});
const catalog = () => ({
  environment_revision: 2,
  policy_revision: 4,
  templates: [
    {
      artifact_id: "artifact-123",
      id: "image-app-v1",
      revision: 1,
      desired_states: ["stopped", "running"],
    },
  ],
});
const storage = () => {
  const map = new Map();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value),
    removeItem: (key) => map.delete(key),
    key: (index) => [...map.keys()][index],
    get length() {
      return map.size;
    },
  };
};
test("creation pins selected artifact and reviewed revisions without arbitrary image or commands", () => {
  const request = serviceRequest(
    null,
    "demo",
    "production",
    draft(),
    catalog(),
    "original-request-123",
  );
  assert.deepEqual(request.values, {
    name: "web",
    artifact_id: "artifact-123",
    template_id: "image-app-v1",
    template_revision: 1,
    environment_revision: 2,
    policy_revision: 4,
    resources: { memory_mib: 128, cpu: 0.25 },
    desired_state: "stopped",
  });
  assert.equal(request.environment, "production");
  assert.equal(
    serviceRequest(request, "other", "development", {}, null, "replacement"),
    request,
  );
});
test("validates numeric limits, service names, artifact availability and reviewed versions", () => {
  for (const patch of [
    { name: "Bad Name" },
    { memory_mib: "" },
    { memory_mib: "15" },
    { cpu: "NaN" },
    { cpu: "0.01" },
    { artifact_id: "" },
  ])
    assert.ok(
      Object.values(serviceProblems({ ...draft(), ...patch })).some(Boolean),
    );
  for (const patch of [
    { environment_revision: null },
    { policy_revision: -1 },
    { templates: [] },
  ])
    assert.throws(() =>
      serviceRequest(
        null,
        "demo",
        "development",
        draft(),
        { ...catalog(), ...patch },
        "request-123456789",
      ),
    );
  assert.throws(() =>
    serviceRequest(
      null,
      "demo",
      "development",
      { ...draft(), desired_state: "running" },
      {
        ...catalog(),
        templates: [{ ...catalog().templates[0], desired_states: ["stopped"] }],
      },
      "request-123456789",
    ),
  );
});
test("interrupted request restores exactly after reload and isolates environment and user", () => {
  const cache = storage(),
    value = draft(),
    request = serviceRequest(
      null,
      "demo",
      "development",
      value,
      catalog(),
      "original-request-123",
    );
  saveServiceDraft(cache, "alice", "demo", "development", value, request);
  assert.deepEqual(restoreServiceDraft(cache, "alice", "demo", "development"), {
    draft: value,
    request,
  });
  assert.equal(restoreServiceDraft(cache, "alice", "demo", "production"), null);
  assert.equal(restoreServiceDraft(cache, "bob", "demo", "development"), null);
  clearServiceDrafts(cache, "bob");
  assert.equal(cache.length, 1);
  clearServiceDrafts(cache, "alice");
  assert.equal(cache.length, 0);
});
test("cache stores only nonsecret allowlisted fields and rejects corrupted or tampered requests", () => {
  const cache = storage(),
    value = { ...draft(), credential: "secret" },
    request = serviceRequest(
      null,
      "demo",
      "development",
      value,
      catalog(),
      "original-request-123",
    );
  request.values.token = "secret";
  saveServiceDraft(cache, "alice", "demo", "development", value, request);
  assert.ok(!cache.getItem(cache.key(0)).includes("secret"));
  const saved = JSON.parse(cache.getItem(cache.key(0)));
  saved.request.values.resources.cpu = "oops";
  cache.setItem(cache.key(0), JSON.stringify(saved));
  assert.equal(
    restoreServiceDraft(cache, "alice", "demo", "development"),
    null,
  );
  cache.setItem(cache.key(0), "not-json");
  assert.equal(
    restoreServiceDraft(cache, "alice", "demo", "development"),
    null,
  );
  clearServiceDrafts(cache, null);
  assert.equal(cache.length, 0);
});
test("queued and provisioning remain pending while recovery is terminal without success", () => {
  assert.equal(serviceTerminal("queued"), false);
  assert.equal(serviceTerminal("provisioning"), false);
  for (const status of ["running", "stopped", "recovery-required", "failed"])
    assert.equal(serviceTerminal(status), true);
});

test("creation accepts only the matching durable operation and environment, never an unrecognized success claim", () => {
  const request = serviceRequest(
    null,
    "demo",
    "production",
    draft(),
    catalog(),
    "original-request-123",
  );
  const result = {
    service: {
      project: "demo",
      environment: "production",
      name: "web",
      operation_id: "operation",
    },
    operation: { operation_id: "operation", status: "queued" },
  };
  assert.equal(serviceResultMatches(result, request), true);
  for (const patch of [
    { environment: "development" },
    { project: "other" },
    { name: "other" },
    { operation_id: "other" },
  ])
    assert.equal(
      serviceResultMatches(
        { ...result, service: { ...result.service, ...patch } },
        request,
      ),
      false,
    );
  assert.equal(
    serviceResultMatches(
      { ...result, operation: { ...result.operation, status: "succeeded" } },
      request,
    ),
    false,
  );
});

test("creation recovery reports storage failures before a request can dispatch", () => {
  const storage = {
    setItem() {
      throw Error("quota exceeded");
    },
  };
  assert.equal(
    saveServiceDraft(storage, "admin", "app", "development", draft(), null),
    false,
  );
  assert.equal(
    saveServiceDraft({ setItem() {} }, "", "app", "development", draft(), null),
    false,
  );
  assert.equal(
    saveServiceDraft(
      { setItem() {} },
      "admin",
      "app",
      "development",
      draft(),
      null,
    ),
    true,
  );
});
