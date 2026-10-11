import test from "node:test";
import assert from "node:assert/strict";
import {
  fileBreadcrumbs,
  verifyFileResponse,
  fileSize,
} from "./containerFiles.js";
test("breadcrumbs remain relative to configured root", () => {
  assert.deepEqual(
    fileBreadcrumbs("src/views").map((item) => item.path),
    ["", "src", "src/views"],
  );
  for (const path of [
    "/etc/passwd",
    "../private",
    "src/../private",
    "src/./file",
    "bad\0name",
  ])
    assert.throws(() => fileBreadcrumbs(path));
});
test("responses must match exact container, root and requested file", () => {
  const response = {
    container_id: "full-id",
    root: "/app",
    path: "src/index.js",
    text: "const a = 1;",
  };
  assert.equal(
    verifyFileResponse(response, "full-id", "src/index.js", "/app", true),
    response,
  );
  for (const changes of [
    { container_id: "replacement" },
    { root: "/other" },
    { path: "other.js" },
  ])
    assert.throws(() =>
      verifyFileResponse(
        { ...response, ...changes },
        "full-id",
        "src/index.js",
        "/app",
        true,
      ),
    );
});
test("preview bound counts UTF-8 bytes and rejects missing text", () => {
  const response = { container_id: "id", root: "/app", path: "file" };
  assert.throws(() => verifyFileResponse(response, "id", "file", "/app", true));
  assert.throws(() =>
    verifyFileResponse(
      { ...response, text: "é".repeat(32769) },
      "id",
      "file",
      "/app",
      true,
    ),
  );
  assert.doesNotThrow(() =>
    verifyFileResponse(
      { ...response, text: "a".repeat(65536) },
      "id",
      "file",
      "/app",
      true,
    ),
  );
});
test("unsupported capability remains explicit without a fictional root", () => {
  const response = {
    container_id: "id",
    path: "",
    root: null,
    capability: { supported: false, reason: "Unavailable" },
    entries: [],
  };
  assert.equal(verifyFileResponse(response, "id", ""), response);
  assert.equal(fileSize(null), "Size unavailable");
  assert.equal(fileSize(0), "0 B");
});
