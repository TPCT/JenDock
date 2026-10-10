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
