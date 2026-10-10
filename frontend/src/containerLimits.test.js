import test from "node:test";
import assert from "node:assert/strict";
import { limitProblems, limitRequest } from "./containerLimits.js";

test("container limits enforce RAM and CPU boundaries including blank and non-finite input", () => {
  for (const draft of [
    { memory_mib: "", cpu: "1" },
    { memory_mib: "15.9", cpu: "1" },
    { memory_mib: "1048577", cpu: "1" },
    { memory_mib: "16", cpu: "0.09" },
    { memory_mib: "16", cpu: "1025" },
    { memory_mib: "Infinity", cpu: "1" },
    { memory_mib: "16", cpu: "NaN" },
    { memory_mib: null, cpu: 1 },
    { memory_mib: 16, cpu: true },
  ]) {
    assert.ok(
      Object.values(limitProblems(draft)).some(Boolean),
      JSON.stringify(draft),
    );
  }
  for (const draft of [
    { memory_mib: "16", cpu: "0.1" },
    { memory_mib: "1048576", cpu: "1024" },
    { memory_mib: "256.5", cpu: "0.25" },
  ])
    assert.deepEqual(limitProblems(draft), { memory_mib: "", cpu: "" });
});

test("an ambiguous retry keeps immutable container identity, revision, limits and request ID", () => {
  const draft = { memory_mib: "256", cpu: "0.5" };
  const target = {
    project: "project",
    name: "container",
    reviewed: { container_id: "original-id", revision: "original-revision" },
    draft,
  };
  const captured = limitRequest(null, target, "stable-request-id");
  draft.memory_mib = "512";
  const replay = limitRequest(
    captured,
    {
      ...target,
      reviewed: {
        container_id: "replacement-id",
        revision: "replacement-revision",
      },
    },
    "new-request-id",
  );
  assert.strictEqual(replay, captured);
  assert.equal(replay.container_id, "original-id");
  assert.equal(replay.revision, "original-revision");
  assert.equal(replay.request_id, "stable-request-id");
  assert.deepEqual(replay.limits, { memory_mib: 256, cpu: 0.5 });
  assert.equal("environment" in replay, false);
});

test("fresh reviewed request uses newly inspected identity while invalid draft creates no request", () => {
  const target = {
    project: "project",
    name: "container",
    reviewed: { container_id: "new-id", revision: "new-revision" },
    draft: { memory_mib: "128", cpu: "0.25" },
  };
  assert.equal(
    limitRequest(null, target, "new-operation").container_id,
    "new-id",
  );
  assert.throws(
    () =>
      limitRequest(
        null,
        { ...target, draft: { memory_mib: "", cpu: "1" } },
        "invalid-operation",
      ),
    /Invalid container limits/,
  );
});

test("configured budgets capture reviewed revision and ambiguous retry cannot switch it", () => {
  const target = {
    project: "app",
    name: "web",
    draft: { memory_mib: "128", cpu: "0.5" },
    reviewed: {
      container_id: "id",
      revision: "compose-revision",
      resource_policy: { configured: true, revision: 4 },
    },
  };
  const payload = limitRequest(null, target, "request");
  assert.equal(payload.policy_revision, 4);
  target.reviewed.resource_policy.revision = 5;
  assert.strictEqual(limitRequest(payload, target, "other"), payload);
  assert.equal(payload.policy_revision, 4);
  assert.equal(
    limitRequest(null, target, "new-reviewed-request").policy_revision,
    5,
  );
  target.reviewed.resource_policy = { configured: false, revision: 0 };
  assert.equal(
    "policy_revision" in limitRequest(null, target, "no-policy"),
    false,
  );
});

test("configured budget with missing or invalid revision never creates a request", () => {
  for (const revision of [undefined, null, -1, 1.5, true, "2"]) {
    assert.throws(
      () =>
        limitRequest(
          null,
          {
            project: "app",
            name: "web",
            draft: { memory_mib: "128", cpu: "0.5" },
            reviewed: {
              container_id: "id",
              revision: "compose",
              resource_policy: { configured: true, revision },
            },
          },
          "request",
        ),
      /budget revision/,
    );
  }
});
