import test from "node:test";
import assert from "node:assert/strict";
import {
  profileDraft,
  profileProblems,
  profilePayload,
} from "./buildProfile.js";
const values = () => ({
  dockerfile: "Dockerfile",
  context: ".",
  target: null,
  test_command: ["node", "test.js"],
  command: ["node", "server.js", "a b"],
  healthcheck: ["node", "health.js"],
  user: "1000:1000",
});
const reviewed = () => ({
  project_revision: 2,
  environment_revision: 3,
  profile_revision: 0,
});
test("source profile preserves argv boundaries without shell splitting or quotation parsing", () => {
  const draft = profileDraft(values());
  assert.equal(draft.command, "node\nserver.js\na b");
  const request = profilePayload("app", "production", reviewed(), draft);
  assert.deepEqual(request.values, values());
  assert.equal(request.environment, "production");
  assert.equal(request.profile_revision, 0);
  assert.equal("recipe_revision" in request, false);
  assert.equal("source_revision" in request, false);
});
test("repository paths reject escapes, absolute or non-normalized forms", () => {
  for (const path of [
    "../app",
    "/tmp/app",
    "./Dockerfile",
    "app/../file",
    "app//file",
    "app/.git/config",
    "C:foo",
    "foo\\bar",
    "a\nfile",
    "a/".repeat(121),
  ]) {
    assert.ok(
      profileProblems({ ...profileDraft(values()), dockerfile: path })
        .dockerfile,
      path,
    );
  }
  assert.ok(
    profileProblems({ ...profileDraft(values()), dockerfile: "." }).dockerfile,
  );
  assert.equal(profileProblems(profileDraft(values())).context, "");
});
test("explicit argv and nonroot identity validate before request construction", () => {
  for (const patch of [
    { command: "" },
    { command: "node\n" },
    { healthcheck: "node\tbad" },
    { test_command: Array(41).fill("arg").join("\n") },
    { user: "0:0" },
    { user: "root" },
    { target: "bad stage" },
  ]) {
    assert.ok(
      Object.values(
        profileProblems({ ...profileDraft(values()), ...patch }),
      ).some(Boolean),
    );
    assert.throws(() =>
      profilePayload("app", "development", reviewed(), {
        ...profileDraft(values()),
        ...patch,
      }),
    );
  }
  for (const patch of [
    { profile_revision: -1 },
    { project_revision: 0 },
    { environment_revision: null },
  ])
    assert.throws(() =>
      profilePayload(
        "app",
        "development",
        { ...reviewed(), ...patch },
        profileDraft(values()),
      ),
    );
});
test("empty profile starts unconfigured without inventing commands or runtime identity", () => {
  const draft = profileDraft(null);
  assert.equal(draft.context, ".");
  assert.equal(draft.command, "");
  assert.equal(draft.user, "");
  assert.throws(() => profilePayload("app", "development", reviewed(), draft));
});
test("HTTP endpoint round-trips its exact port and health path without caller schema version", () => {
  const source = {
    ...values(),
    http_endpoint: { port: 8080, health_path: "/health/ready" },
  };
  const draft = profileDraft(source);
  assert.equal(draft.http_port, "8080");
  assert.equal(draft.http_health_path, "/health/ready");
  const request = profilePayload("app", "development", reviewed(), draft);
  assert.deepEqual(request.values, source);
  assert.equal("schema_version" in request.values, false);
});
test("blank HTTP port preserves v1 payload and removes previously saved endpoint", () => {
  const draft = profileDraft({
    ...values(),
    http_endpoint: { port: 8080, health_path: "/health" },
  });
  draft.http_port = "";
  draft.http_health_path = "ignored draft";
  assert.deepEqual(
    profilePayload("app", "development", reviewed(), draft).values,
    values(),
  );
  assert.equal(profileDraft(values()).http_health_path, "/");
});
test("HTTP endpoint validates bounded integer port and normalized absolute health path", () => {
  for (const http_port of [
    "0",
    "65536",
    "-1",
    "1.5",
    "1e3",
    " 80",
    "80 ",
    "http",
  ])
    assert.ok(
      profileProblems({ ...profileDraft(values()), http_port }).http_port,
      http_port,
    );
  for (const http_port of ["1", "80", "65535"])
    assert.equal(
      profileProblems({ ...profileDraft(values()), http_port }).http_port,
      "",
    );
  for (const http_health_path of [
    "",
    "health",
    "https://site/health",
    "//host",
    "/a//b",
    "/a/",
    "/a/../b",
    "/./health",
    "/health?x=1",
    "/health:ready",
    "/health@check",
    "/صحة",
    "/héalth",
    "/health#part",
    "/a%2fb",
    "/a\\b",
    "/a b",
    "/a\n",
    "/" + "a".repeat(240),
  ])
    assert.ok(
      profileProblems({
        ...profileDraft(values()),
        http_port: "8080",
        http_health_path,
      }).http_health_path,
      http_health_path,
    );
  for (const http_health_path of ["/", "/health", "/health/ready"])
    assert.equal(
      profileProblems({
        ...profileDraft(values()),
        http_port: "8080",
        http_health_path,
      }).http_health_path,
      "",
    );
});
