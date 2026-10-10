import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { build } from "esbuild";
import { englishMessage } from "./legacyMessages.js";
const bundle = await build({
  entryPoints: [new URL("./RunDetail.jsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  format: "cjs",
  platform: "node",
  packages: "external",
  loader: { ".css": "empty" },
});
const module = { exports: {} };
new Function("require", "module", "exports", bundle.outputFiles[0].text)(
  createRequire(import.meta.url),
  module,
  module.exports,
);
const { recordedRunStages } = module.exports;
test("recorded stages filter unrelated runs and group observations without inventing success", () => {
  const rows = recordedRunStages(
    { id: 4, status: "failed", updated: 40, detail: "Execution failed" },
    [
      {
        id: 3,
        job_id: 4,
        created: 30,
        status: "testing",
        detail: "Tests starting",
      },
      { id: 2, job_id: 4, created: 20, status: "building", detail: "Image B" },
      { id: 8, job_id: 99, created: 10, status: "succeeded" },
      { id: 1, job_id: 4, created: 10, status: "building", detail: "Image A" },
    ],
  );
  assert.deepEqual(
    rows.map((row) => row.status),
    ["building", "testing", "failed"],
  );
  assert.deepEqual(
    rows[0].events.map((event) => event.detail),
    ["Image A", "Image B"],
  );
  assert.equal(rows.at(-1).source, "job");
  assert.equal(rows.at(-1).events[0].detail, "Execution failed");
});
test("missing historical event history remains a labeled job record", () => {
  const rows = recordedRunStages(
    { id: 10, status: "awaiting-validation", updated: 100 },
    [],
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, "awaiting-validation");
  assert.equal(rows[0].source, "job");
});
test("historical failure diagnostics retain their meaning in English", () => {
  const translated = englishMessage(
    "جارٍ اختبار الصور دون أسرار أو قواعد بيانات الإنتاج. — فشلت خطوة التنفيذ (exit 1). راجع تشخيص العامل محليًا.",
  );
  assert.match(
    translated,
    /Testing images without production secrets or databases/,
  );
  assert.match(translated, /Execution step failed \(exit 1\)/);
  assert.match(translated, /Review the protected worker diagnostics/);
  assert.doesNotMatch(translated, /[\u0600-\u06ff]/);
});
