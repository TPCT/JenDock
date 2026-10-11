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

test("environment build payload captures reviewed target revisions without mutating its source", () => {
  const target = {
    environment: "production",
    project_revision: 3,
    environment_revision: 7,
    recipe_revision: 9,
  };
  const payload = buildPayload({
    project: "app",
    branch: "production",
    sha: "a".repeat(40),
    mode: "deploy",
    requestId: "stable-request",
    target,
  });
  target.environment_revision = 8;
  assert.equal(payload.environment, "production");
  assert.equal(payload.project_revision, 3);
  assert.equal(payload.environment_revision, 7);
  assert.equal(payload.recipe_revision, 9);
});

test("retry cannot move a pinned run to another environment or a legacy target", () => {
  const retry = { id: 42, target: { environment: "development" } };
  const base = {
    project: "app",
    branch: "development",
    sha: "b".repeat(40),
    mode: "validate",
    requestId: "stable-request",
    retry,
  };
  for (const target of [
    {
      environment: "production",
      project_revision: 1,
      environment_revision: 1,
      recipe_revision: 1,
    },
    { environment: "legacy-unclassified" },
    undefined,
  ])
    assert.throws(
      () => buildPayload({ ...base, target }),
      /original environment/,
    );
  assert.equal(
    buildPayload({
      ...base,
      target: {
        environment: "development",
        project_revision: 2,
        environment_revision: 3,
        recipe_revision: 4,
      },
    }).mode,
    "validate",
  );
});

test("unreviewed environment revisions are rejected and explicit legacy carries no inferred environment fields", () => {
  const base = {
    project: "app",
    branch: "main",
    sha: "c".repeat(40),
    mode: "deploy",
    requestId: "stable-request",
  };
  for (const revision of [undefined, null, 0, 1.5, true])
    assert.throws(
      () =>
        buildPayload({
          ...base,
          target: {
            environment: "production",
            project_revision: 1,
            environment_revision: revision,
            recipe_revision: 2,
          },
        }),
      /Refresh/,
    );
  const legacy = buildPayload({
    ...base,
    target: { environment: "legacy-unclassified" },
  });
  assert.equal("environment" in legacy, false);
  assert.equal("environment_revision" in legacy, false);
});

test('container navigation preserves explicit environment and terminal tab without inferring ownership', async () => {
  const {containerRoute, projectReturnRoute, workspaceEnvironment} = await import('./model.js');
  assert.equal(containerRoute('app', 'app-web-1', 'production', 'terminal'), 'container/app/app-web-1?environment=production&tab=terminal');
  assert.equal(containerRoute('a/b', 'web #1', 'development'), 'container/a%2Fb/web%20%231?environment=development');
  assert.equal(projectReturnRoute('app', 'production'), 'project/app?environment=production&tab=containers');
  assert.equal(containerRoute('app', 'web', 'staging'), 'container/app/web');
  assert.equal(projectReturnRoute('app', null), 'project/app');
  assert.equal(workspaceEnvironment('staging'), null);
});

test('same-project route changes resolve the requested workspace tab', async () => {
  const {projectReturnRoute, projectTabIndex} = await import('./model.js');
  assert.equal(projectTabIndex('settings'), 3);
  const next = projectReturnRoute('aic-website', 'development');
  const query = new URLSearchParams(next.split('?')[1]);
  assert.equal(query.get('environment'), 'development');
  assert.equal(projectTabIndex(query.get('tab')), 1);
  assert.equal(projectTabIndex('deployments'), 2);
  assert.equal(projectTabIndex(null), 0);
  assert.equal(projectTabIndex('invalid'), 0);
});

test('source builds pin their revision for validation and managed deployment',()=>{
 const base={project:'app',branch:'main',sha:'a'.repeat(40),mode:'validate',requestId:'request',target:{environment:'development',project_revision:3,environment_revision:4,source_revision:7}};
 const body=buildPayload(base);assert.equal(body.source_revision,7);assert.equal('recipe_revision' in body,false);
 const deployment=buildPayload({...base,mode:'deploy'});
 assert.equal(deployment.mode,'deploy');assert.equal(deployment.source_revision,7);
 assert.equal('recipe_revision' in deployment,false);
 assert.equal(deployment.environment,'development');
 assert.equal(deployment.project_revision,3);assert.equal(deployment.environment_revision,4);
 for(const revision of [0,1.5,null])assert.throws(()=>buildPayload({...base,target:{...base.target,source_revision:revision}}),/Refresh/);
});
