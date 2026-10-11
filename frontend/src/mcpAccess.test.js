import test from "node:test";
import assert from "node:assert/strict";
import { freshMcpDraft, mcpCredentialValues } from "./mcpAccess.js";
const projects = {
  demo: { id: "demo", repository_id: 42 },
  other: { id: "other", repository_id: 99 },
};
const draft = () => ({
  ...freshMcpDraft(),
  label: "Editor",
  projects: ["demo"],
  environments: [JSON.stringify(["demo", "development"])],
});
test("grants exact selected project, repository and environment, with bounded expiration", () => {
  const values = mcpCredentialValues(draft(), projects, 1000000);
  assert.deepEqual(values.allowed_projects, ["demo"]);
  assert.deepEqual(values.allowed_repository_ids, [42]);
  assert.deepEqual(values.allowed_environments, [["demo", "development"]]);
  assert.equal(values.expires_at, 1000 + 7 * 86400);
});
test("removed projects cannot retain environment access", () => {
  const d = draft();
  d.environments.push(JSON.stringify(["other", "production"]));
  assert.deepEqual(mcpCredentialValues(d, projects).allowed_environments, [
    ["demo", "development"],
  ]);
});
test("every service permission requires explicit environment selection", () => {
  for (const scope of ["services:read", "services:create", "services:start"]) {
    assert.throws(
      () =>
        mcpCredentialValues(
          { ...draft(), scopes: [scope], environments: [] },
          projects,
        ),
      /exact environments/,
    );
  }
});
test("credential labels respect the server 80-character limit", () => {
  assert.equal(
    mcpCredentialValues({ ...draft(), label: "a".repeat(80) }, projects).label
      .length,
    80,
  );
  assert.throws(
    () => mcpCredentialValues({ ...draft(), label: "a".repeat(81) }, projects),
    /1–80/,
  );
});
test("future project access requires creation scope, unique slug and numeric repository", () => {
  const d = {
    ...draft(),
    projects: [],
    newProject: "new-app",
    repositoryId: "123",
    scopes: ["projects:create"],
  };
  const values = mcpCredentialValues(d, projects);
  assert.deepEqual(values.allowed_projects, ["new-app"]);
  assert.deepEqual(values.allowed_repository_ids, [123]);
  for (const changes of [
    { scopes: ["projects:read"] },
    { repositoryId: "owner/repo" },
    { newProject: "demo" },
    { repositoryId: "9007199254740993" },
  ])
    assert.throws(() => mcpCredentialValues({ ...d, ...changes }, projects));
});
test("missing repository identity, unregistered targets and unlimited lifetime fail closed", () => {
  assert.throws(() => mcpCredentialValues(draft(), { demo: { id: "demo" } }));
  assert.throws(() =>
    mcpCredentialValues({ ...draft(), projects: ["missing"] }, projects),
  );
  assert.throws(() =>
    mcpCredentialValues({ ...draft(), lifetime: 0 }, projects),
  );
});

test("database grants require exact environments and explicit row plus metadata scopes", () => {
  assert.equal(
    freshMcpDraft().scopes.some((scope) => scope.startsWith("databases:")),
    false,
  );
  assert.throws(
    () =>
      mcpCredentialValues(
        { ...draft(), scopes: ["databases:read"], environments: [] },
        projects,
      ),
    /exact environments/,
  );
  assert.throws(
    () =>
      mcpCredentialValues(
        { ...draft(), scopes: ["databases:data:read"] },
        projects,
      ),
    /metadata access/,
  );
  const scopes = ["databases:read", "databases:data:read"];
  assert.deepEqual(
    mcpCredentialValues({ ...draft(), scopes }, projects).scopes,
    scopes,
  );
});
