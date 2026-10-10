import test from "node:test";
import assert from "node:assert/strict";
import { createLogPolling } from "./logPolling.js";

function fixture() {
  const requests = [],
    results = [],
    errors = [],
    loading = [],
    timers = new Map();
  let sequence = 0;
  const poll = createLogPolling({
    fetchLogs: () =>
      new Promise((resolve, reject) => requests.push({ resolve, reject })),
    onResult: (value) => results.push(value),
    onError: (value) => errors.push(value),
    onLoading: (value) => loading.push(value),
    schedule: (callback) => {
      timers.set(++sequence, callback);
      return sequence;
    },
    cancel: (id) => timers.delete(id),
  });
  return { poll, requests, results, errors, loading, timers };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("pause keeps displayed logs, cancels timer and does not request again; resume fetches once", async () => {
  const f = fixture();
  f.requests[0].resolve("existing logs");
  await tick();
  assert.equal(f.timers.size, 1);
  f.poll.setLive(false);
  assert.equal(f.timers.size, 0);
  assert.equal(f.requests.length, 1);
  assert.deepEqual(f.results, ["existing logs"]);
  f.poll.setLive(true);
  f.poll.setLive(true);
  f.poll.reload();
  assert.equal(f.requests.length, 2);
  f.poll.stop();
});

test("pause during request retains its result without scheduling another; manual reload works while paused", async () => {
  const f = fixture();
  f.poll.setLive(false);
  f.requests[0].resolve("captured");
  await tick();
  assert.deepEqual(f.results, ["captured"]);
  assert.equal(f.timers.size, 0);
  f.poll.reload();
  assert.equal(f.requests.length, 2);
  f.requests[1].reject(new Error("Disconnected"));
  await tick();
  assert.deepEqual(f.results, ["captured"]);
  assert.equal(f.errors[0].message, "Disconnected");
  assert.equal(f.timers.size, 0);
  f.poll.stop();
});

test("stopping for identity change or unmount ignores old results and stops timers", async () => {
  const f = fixture();
  f.poll.stop();
  f.requests[0].resolve("old container");
  await tick();
  assert.deepEqual(f.results, []);
  assert.equal(f.timers.size, 0);
  f.poll.reload();
  assert.equal(f.requests.length, 1);
});
