import test from "node:test";
import assert from "node:assert/strict";
import {
  retryRequest,
  saveRetryRequest,
  restoreRetryRequest,
  observedAge,
  workloadStateMessage,
} from "./workloadPanel.js";
const request = {
  request_id: "request",
  project: "example",
  environment: "development",
  service_id: "service",
  container_id: "original",
  definition_digest: "digest",
  adapter_revision: 1,
  connection_id: "database",
  queue: "default",
  failed_job_id: "failed",
  failure_revision: "revision",
};
test("single retry rejects payload and closed target violations", () => {
  assert.deepEqual(retryRequest(request), request);
  assert.throws(() => retryRequest({ ...request, payload: "not permitted" }));
  assert.throws(() => retryRequest({ ...request, environment: "other" }));
  assert.throws(() => retryRequest({ ...request, failed_job_id: "" }));
});
test("retry is synchronously retained before dispatch, restore never retargets replacement", () => {
  let saved;
  const storage = {
    setItem: (_, value) => {
      saved = value;
    },
    getItem: () => saved,
  };
  saveRetryRequest(storage, "key", request);
  assert.deepEqual(
    restoreRetryRequest(storage, "key", {
      project: "example",
      environment: "development",
      service_id: "service",
      container_id: "replacement",
    }),
    request,
  );
  assert.throws(() =>
    restoreRetryRequest(storage, "key", {
      project: "example",
      environment: "production",
      service_id: "service",
    }),
  );
  assert.throws(
    () =>
      saveRetryRequest(
        {
          setItem: () => {
            throw Error("full");
          },
        },
        "key",
        request,
      ),
    /full/,
  );
});
test("observation freshness does not invent timestamps or runtime health", () => {
  assert.equal(observedAge(95, 100), 5);
  assert.equal(observedAge("1970-01-01T00:01:35Z",100),5);
  assert.equal(observedAge(null, 100), null);
  assert.equal(observedAge(200, 100), null);
  assert.match(workloadStateMessage("stopped"), /will not start/);
  assert.match(workloadStateMessage("recovery-required"), /unresolved/);
});
