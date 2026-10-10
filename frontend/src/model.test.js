import test from "node:test";
import assert from "node:assert/strict";
import { servicesFor, branchState, buildPayload, stale } from "./model.js";
test("group services by configured prefix, not arbitrary project substring", () => {
  const data = {
    snapshots: {
      containers: {
        value: [
          { Names: "aic-app-1", Image: "aic:1" },
          { Names: "other-aic-app", Image: "other:1" },
        ],
      },
    },
  };
  assert.equal(servicesFor(data, { container_prefixes: ["aic-"] }).length, 1);
});
test("stale branches cannot authorize a new build", () => {
  const o = {
    snapshots: {
      "branches:a": {
        updated: Date.now() / 1000 - 601,
        value: { branches: { main: "a".repeat(40) } },
      },
    },
  };
  assert.equal(branchState(o, "a").fresh, false);
});
test("retry preserves validation mode and idempotency key", () => {
  const p = buildPayload({
    project: "a",
    branch: "main",
    sha: "a".repeat(40),
    mode: "validate",
    retry: { id: 7 },
    requestId: "stable-id",
  });
  assert.equal(p.action, "retry");
  assert.equal(p.mode, "validate");
  assert.equal(p.request_id, "stable-id");
  assert.equal(p.job_id, 7);
});
test("connection failure makes even recent observed containers unavailable", () =>
  assert.equal(stale({ preview: true }, true), true));

test("environment build payload captures reviewed target revisions without mutating its source", () => {
  const target = {
    environment: "production",
    project_revision: 3,
    environment_revision: 7,
    recipe_revision: 9,
  };
  const payload = buildPayload({
    project: "app",
    branch: "production",
    sha: "a".repeat(40),
    mode: "deploy",
    requestId: "stable-request",
    target,
  });
  target.environment_revision = 8;
  assert.equal(payload.environment, "production");
  assert.equal(payload.project_revision, 3);
  assert.equal(payload.environment_revision, 7);
  assert.equal(payload.recipe_revision, 9);
});

test("retry cannot move a pinned run to another environment or a legacy target", () => {
  const retry = { id: 42, target: { environment: "development" } };
  const base = {
    project: "app",
    branch: "development",
    sha: "b".repeat(40),
    mode: "validate",
    requestId: "stable-request",
    retry,
  };
  for (const target of [
    {
      environment: "production",
      project_revision: 1,
      environment_revision: 1,
      recipe_revision: 1,
    },
    { environment: "legacy-unclassified" },
    undefined,
  ])
    assert.throws(
      () => buildPayload({ ...base, target }),
      /original environment/,
    );
  assert.equal(
    buildPayload({
      ...base,
      target: {
        environment: "development",
        project_revision: 2,
        environment_revision: 3,
        recipe_revision: 4,
      },
    }).mode,
    "validate",
  );
});

test("unreviewed environment revisions are rejected and explicit legacy carries no inferred environment fields", () => {
  const base = {
    project: "app",
    branch: "main",
    sha: "c".repeat(40),
    mode: "deploy",
    requestId: "stable-request",
  };
  for (const revision of [undefined, null, 0, 1.5, true])
    assert.throws(
      () =>
        buildPayload({
          ...base,
          target: {
            environment: "production",
            project_revision: 1,
            environment_revision: revision,
            recipe_revision: 2,
          },
        }),
      /Refresh/,
    );
  const legacy = buildPayload({
    ...base,
    target: { environment: "legacy-unclassified" },
  });
  assert.equal("environment" in legacy, false);
  assert.equal("environment_revision" in legacy, false);
});
