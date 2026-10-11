import test from "node:test";
import assert from "node:assert/strict";
import {
  storeCreation,
  restoreCreation,
  clearCreation,
} from "./projectCreationStorage.js";
import { creationRequest } from "./projectCreation.js";
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
const draft = () => {
  const value = {
    id: "example",
    label: "Example",
    repository: "Owner/Repo",
    customId: false,
  };
  return {
    ...value,
    request: creationRequest(null, value, "request-original-12345"),
    outcome: "uncertain",
    created: null,
  };
};
test("reload restores original request and isolates authenticated usernames without storing errors or credentials", () => {
  const cache = storage(),
    value = draft();
  value.error = "raw provider diagnostic";
  value.token = "provider-secret";
  value.request.values.token = "nested-secret";
  storeCreation(cache, "alice", value, 100);
  const raw = cache.getItem(cache.key(0));
  for (const excluded of [
    "raw provider",
    "provider-secret",
    "nested-secret",
    "token",
  ])
    assert.equal(raw.includes(excluded), false);
  assert.equal(restoreCreation(cache, "bob", 101), null);
  const restored = restoreCreation(cache, "alice", 101);
  assert.equal(restored.request.request_id, "request-original-12345");
  assert.strictEqual(
    creationRequest(restored.request, restored, "different-key"),
    restored.request,
  );
  // Unresolved creation keys do not expire while their browser session is retained.
  assert.equal(
    restoreCreation(cache, "alice", 100 + 30 * 86400000).request.request_id,
    "request-original-12345",
  );
});
test("corrupt, stale schema, wrong owner, oversized and inconsistent request caches are ignored", () => {
  const cache = storage();
  storeCreation(cache, "alice", draft(), 100);
  const key = cache.key(0),
    valid = JSON.parse(cache.getItem(key));
  for (const value of [
    "{broken",
    JSON.stringify({ ...valid, version: 0 }),
    JSON.stringify({ ...valid, owner: "bob" }),
    JSON.stringify({ ...valid, updated: 999 }),
    "x".repeat(16001),
    JSON.stringify({ ...valid, draft: { ...valid.draft, id: "changed" } }),
    JSON.stringify({ ...valid, draft: { ...valid.draft, request: null } }),
  ]) {
    cache.setItem(key, value);
    assert.equal(restoreCreation(cache, "alice", 101), null);
  }
});
test("signout and expired boot session clear only creation caches; storage denial is harmless", () => {
  const cache = storage();
  storeCreation(cache, "alice", draft(), 100);
  storeCreation(cache, "bob", draft(), 100);
  cache.setItem("theme", "dark");
  clearCreation(cache, "alice");
  assert.equal(restoreCreation(cache, "alice", 101), null);
  assert.ok(restoreCreation(cache, "bob", 101));
  clearCreation(cache, null);
  assert.equal(cache.length, 1);
  assert.equal(cache.getItem("theme"), "dark");
  assert.doesNotThrow(() => storeCreation(null, "alice", draft()));
  assert.equal(restoreCreation(null, "alice"), null);
  assert.doesNotThrow(() => clearCreation(null, "alice"));
});
