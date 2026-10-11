import test from "node:test";
import assert from "node:assert/strict";
import {
  workloadViewKey,
  workloadResponse,
  retryStorageKey,
  restoreWorkloadRetry,
  retryOperation,
  workloadFailure,
} from "./workloadConsole.js";
const target = {
  project: "demo",
  environment: "development",
  service_id: "service",
  container_id: "old",
  definition_digest: "digest",
  adapter_revision: 1,
};
const request = {
  ...target,
  request_id: "original",
  connection_id: "pgsql",
  queue: "default",
  failed_job_id: "failure",
  failure_revision: "revision",
};
const snapshot = () => ({
  workload: {
    ...target,
    role: "queue",
    received_at: Date.now() / 1000,
    capabilities: { read: true, retry: false },
    observation: {
      protocol: 1,
      status: "ok",
      observed_at: new Date().toISOString(),
      scope: {
        project: "demo",
        environment: "development",
        service_id: "service",
        definition_digest: "digest",
      },
      result: { queues: [], workers: [] },
    },
  },
});
test("read verifies both outer target and inner scope before showing empty or populated result", () => {
  assert.deepEqual(
    workloadResponse(snapshot(), target, "queue").observation.result.queues,
    [],
  );
  for (const layer of ["outer", "inner"]) {
    const s = snapshot();
    if (layer === "outer") s.workload.container_id = "replacement";
    else s.workload.observation.scope.environment = "production";
    assert.throws(
      () => workloadResponse(s, target, "queue"),
      /identity-changed/,
    );
  }
  const s = snapshot();
  s.workload.observation.status = "unsupported";
  assert.throws(() => workloadResponse(s, target, "queue"), /unsupported/);
});
test("retry restore is owner isolated and retains original container after replacement", () => {
  const key = retryStorageKey("owner", target);
  assert.notEqual(key, retryStorageKey("another", target));
  assert.equal(
    restoreWorkloadRetry({ getItem: () => JSON.stringify(request) }, key, {
      ...target,
      container_id: "new",
    }).container_id,
    "old",
  );
  assert.throws(() =>
    restoreWorkloadRetry({ getItem: () => JSON.stringify(request) }, key, {
      ...target,
      environment: "production",
    }),
  );
});
test("operation result must match every original pin and never presents requeue as completion", () => {
  const { request_id, ...pins } = request;
  const valid = {
    operation: {
      request_id,
      target: pins,
      status: "requeued",
      business_execution: "not-observed",
    },
  };
  assert.equal(
    retryOperation(valid, request).business_execution,
    "not-observed",
  );
  assert.throws(() =>
    retryOperation(
      {
        operation: {
          ...valid.operation,
          target: { ...pins, container_id: "new" },
        },
      },
      request,
    ),
  );
  assert.throws(() =>
    retryOperation(
      { operation: { ...valid.operation, status: "completed" } },
      request,
    ),
  );
});
test("safe error state does not print raw adapter exceptions", () => {
  assert.equal(
    workloadFailure({ message: "secret payload traceback" }),
    "unavailable",
  );
  assert.equal(workloadFailure({ code: "forbidden" }), "denied");
});

test('replacement resets browsing view but preserves actor-scoped original operation storage',()=>{const next={...target,container_id:'replacement',definition_digest:'newdigest'};assert.notEqual(workloadViewKey('owner',target),workloadViewKey('owner',next));assert.equal(retryStorageKey('owner',target),retryStorageKey('owner',next));});
