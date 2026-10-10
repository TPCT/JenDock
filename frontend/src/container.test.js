import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { build } from "esbuild";
// Exercise production contract helpers in Node; xterm itself needs a browser.
const bundle = await build({
  entryPoints: [new URL("./ContainerPage.jsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  format: "cjs",
  platform: "node",
  packages: "external",
  plugins: [
    {
      name: "terminal-view",
      setup(b) {
        b.onResolve({ filter: /\.\/TerminalPanel$/ }, () => ({
          path: "terminal",
          namespace: "test",
        }));
        b.onLoad({ filter: /.*/, namespace: "test" }, () => ({
          contents: "export default function TerminalPanel(){return null}",
        }));
      },
    },
  ],
});
const module = { exports: {} };
new Function("require", "module", "exports", bundle.outputFiles[0].text)(
  createRequire(import.meta.url),
  module,
  module.exports,
);
const {
  updateEnvironmentDraft,
  environmentProblem,
  containerObservation,
  containerOperationBody,
} = module.exports;
const data = () => ({
  snapshots: {
    containers: {
      updated: 1000,
      value: [{ Names: "app-1", ID: "abc123", State: "running" }],
    },
    worker: { value: { state: "running" } },
  },
});

test("environment revert only clears that change, preserves unrelated edits and new empty values", () => {
  const base = { API_KEY: "saved", EMPTY: "" };
  const draft = updateEnvironmentDraft(
    base,
    { OTHER: "retained" },
    "API_KEY",
    "new",
  );
  assert.deepEqual(draft, { OTHER: "retained", API_KEY: "new" });
  assert.deepEqual(updateEnvironmentDraft(base, draft, "API_KEY", "saved"), {
    OTHER: "retained",
  });
  assert.deepEqual(base, { API_KEY: "saved", EMPTY: "" });
  assert.deepEqual(updateEnvironmentDraft(base, {}, "NEW_EMPTY", ""), {
    NEW_EMPTY: "",
  });
});
test("prototype-like environment keys remain own data without prototype mutation", () => {
  const next = updateEnvironmentDraft({}, {}, "__proto__", "literal-value");
  assert.equal(Object.getPrototypeOf(next), Object.prototype);
  assert.equal(Object.hasOwn(next, "__proto__"), true);
  assert.equal(JSON.parse(JSON.stringify(next)).__proto__, "literal-value");
  assert.equal(environmentProblem(next), "");
});
test("environment validation matches backend key, count and value boundaries", () => {
  assert.equal(environmentProblem({ _KEY2: "", LONG: "a".repeat(32768) }), "");
  for (const value of [
    {},
    { "2KEY": "x" },
    { ["A".repeat(129)]: "x" },
    { KEY: "a".repeat(32769) },
    { KEY: "a\0b" },
    { KEY: null },
    Object.fromEntries(
      Array.from({ length: 201 }, (_, n) => [`KEY_${n}`, "x"]),
    ),
  ])
    assert.notEqual(environmentProblem(value), "");
});
test("fresh observations require worker health, timestamp and successful fetch", () => {
  assert.equal(
    containerObservation(data(), "app-1", "abc123full", false, 1089).stale,
    false,
  );
  assert.equal(
    containerObservation(data(), "app-1", "abc123full", false, 1091).stale,
    true,
  );
  assert.equal(
    containerObservation(data(), "app-1", "abc123full", true, 1001).stale,
    true,
  );
  const missingTimestamp = data();
  delete missingTimestamp.snapshots.containers.updated;
  assert.equal(
    containerObservation(missingTimestamp, "app-1", "abc123full", false, 1001)
      .stale,
    true,
  );
  const stopped = data();
  stopped.snapshots.worker.value.state = "stopped";
  assert.equal(
    containerObservation(stopped, "app-1", "abc123full", false, 1001).stale,
    true,
  );
});
test("same-name replacement differs from abbreviated identity and missing container", () => {
  assert.equal(
    containerObservation(data(), "app-1", "abc123full", false, 1001).replaced,
    false,
  );
  assert.equal(
    containerObservation(data(), "app-1", "old123full", false, 1001).replaced,
    true,
  );
  assert.equal(
    containerObservation(data(), "missing", "abc123full", false, 1001).observed,
    undefined,
  );
});
test("ambiguous retry retains captured identity, revision, changes and UUID", () => {
  const op = {
    kind: "environment-save",
    containerId: "full-original-id",
    revision: "original-revision",
    requestId: "fixed-uuid",
    changes: { SECRET: "edited-value" },
  };
  const first = containerOperationBody(op, "project", "app-1");
  const retry = containerOperationBody(
    { ...op, submitted: true },
    "project",
    "app-1",
  );
  assert.deepEqual(retry, first);
  assert.equal(retry.container_id, "full-original-id");
  assert.equal(retry.revision, "original-revision");
  assert.equal(retry.request_id, "fixed-uuid");
  assert.equal(retry.action, "environment-save");
  const restart = containerOperationBody(
    { ...op, kind: "restart", changes: undefined },
    "project",
    "app-1",
  );
  assert.equal(restart.action, "container-restart");
  assert.equal(Object.hasOwn(restart, "changes"), false);
});

const terminalBundle = await build({
  entryPoints: [new URL("./TerminalPanel.jsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  format: "cjs",
  platform: "node",
  packages: "external",
  plugins: [
    {
      name: "xterm-browser-only",
      setup(b) {
        b.onResolve({ filter: /^@xterm\// }, (args) => ({
          path: args.path,
          namespace: "test",
        }));
        b.onLoad({ filter: /.*/, namespace: "test" }, () => ({
          contents: "export class Terminal {} export class FitAddon {}",
        }));
      },
    },
  ],
});
const terminalModule = { exports: {} };
new Function(
  "require",
  "module",
  "exports",
  terminalBundle.outputFiles[0].text,
)(createRequire(import.meta.url), terminalModule, terminalModule.exports);
test("terminal polling preserves Unicode characters across the input boundary", () => {
  const input = "x".repeat(999) + "😀" + "done";
  const first = terminalModule.exports.terminalInputChunk(input);
  assert.equal(first, "x".repeat(999));
  const second = terminalModule.exports.terminalInputChunk(
    input.slice(first.length),
  );
  assert.equal(first + second, input);
  assert.equal(
    terminalModule.exports.terminalInputChunk("a".repeat(2000)).length,
    1000,
  );
});
