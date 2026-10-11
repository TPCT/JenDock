import test from "node:test";
import assert from "node:assert/strict";
import {
  applicationDraft,
  applicationValues,
  endpointKey,
  scopedApplicationBinding,
} from "./applicationDatabaseSettings.js";
const definition = {
  project: "demo",
  environment: "development",
  service_id: "service",
  engine: "postgresql",
  database_name: "app",
  database_port: 5432,
  ownership: { kind: "adopted-compose", reference: "db", revision: 1 },
  secret_id: "account",
  secret_revision: 2,
  environment_mapping: { url: "DATABASE_URL" },
};
const binding = {
  id: "binding",
  project: "demo",
  environment: "development",
  service_id: "service",
  revision: 1,
  definition_digest: "digest",
  definition,
};
const targets = [{ definition }],
  accounts = [{ id: "account", enabled: true, revision: 3 }];
test("application connection preserves service and owned endpoint, pins current account revision", () => {
  assert.deepEqual(
    applicationValues(applicationDraft(binding), targets, accounts, "service"),
    {
      service_id: "service",
      engine: "postgresql",
      database_name: "app",
      database_port: 5432,
      ownership: definition.ownership,
      secret_id: "account",
      secret_revision: 3,
      environment_mapping: { url: "DATABASE_URL" },
    },
  );
});
test("application connection rejects cross container and environment metadata", () => {
  assert.equal(
    scopedApplicationBinding(binding, "demo", "development", "service"),
    binding,
  );
  assert.throws(() =>
    scopedApplicationBinding(binding, "demo", "production", "service"),
  );
  assert.throws(() =>
    scopedApplicationBinding(binding, "demo", "development", "another"),
  );
});
test("application variable mapping rejects duplicates, runtime injection names and revoked credentials", () => {
  const draft = applicationDraft(null);
  draft.endpoint = endpointKey(definition);
  draft.credential = "account";
  assert.equal(
    applicationValues(draft, targets, accounts, "service").environment_mapping
      .host,
    "DB_HOST",
  );
  for (const name of ["PATH", "LD_PRELOAD", "bad-name", "DB_PORT"])
    assert.throws(() =>
      applicationValues(
        { ...draft, mapping: { ...draft.mapping, host: name } },
        targets,
        accounts,
        "service",
      ),
    );
  assert.throws(() =>
    applicationValues(
      draft,
      targets,
      [{ ...accounts[0], enabled: false }],
      "service",
    ),
  );
});
test("application URL mapping emits only URL name, never connection values", () => {
  const draft = {
    ...applicationDraft(binding),
    mapping: { url: "APP_DATABASE_URL", password: "OLD_PASSWORD" },
  };
  assert.deepEqual(
    applicationValues(draft, targets, accounts, "service").environment_mapping,
    { url: "APP_DATABASE_URL" },
  );
});

import {
  persistAttachmentOperation,
  attachmentPin,
  sameAttachment,
  restoreAttachmentOperation,
  attachmentOperationRecord,
} from "./applicationRuntime.js";
test("runtime attachment pins only enabled current configuration", () => {
  const configured = { ...binding, enabled: true, status: "configured" };
  assert.deepEqual(attachmentPin(configured), {
    binding_id: "binding",
    revision: 1,
    digest: "digest",
  });
  assert.throws(() =>
    attachmentPin({ ...configured, status: "credential-stale" }),
  );
  assert.throws(() => attachmentPin({ ...configured, enabled: false }));
  assert.equal(
    sameAttachment(attachmentPin(configured), {
      binding_id: "binding",
      revision: 2,
      digest: "digest",
    }),
    false,
  );
});
test("recovery restores only attachment operation, preserving exact identity and no generic values", () => {
  const op = {
    kind: "environment-save",
    requestId: "request",
    containerId: "oldcontainer",
    revision: "oldrevision",
    submitted: true,
    changes: {},
    databaseAttachment: {
      binding_id: "binding",
      revision: 1,
      digest: "digest",
    },
  };
  assert.deepEqual(restoreAttachmentOperation(JSON.stringify(op)), op);
  assert.equal(
    restoreAttachmentOperation(
      JSON.stringify({ ...op, changes: { SECRET: "must-not-persist" } }),
    ),
    null,
  );
  assert.equal(restoreAttachmentOperation("invalid"), null);
  const row = {
    request_id: "request",
    project: "example",
    container: "web",
    operation: "environment-save",
    status: "recovery-required",
  };
  assert.equal(attachmentOperationRecord([row], op, "example", "web"), row);
  assert.equal(attachmentOperationRecord([row], op, "other", "web"), null);
});

test("persisted attachment operation contains only allowlisted recovery fields and storage failures propagate", () => {
  const operation = {
    kind: "environment-save",
    requestId: "r",
    containerId: "c",
    revision: "v",
    changes: {},
    databaseAttachment: {
      binding_id: "b",
      revision: 1,
      digest: "d",
      password: "private",
    },
    privateMetadata: "private",
  };
  let value;
  const saved = persistAttachmentOperation(
    {
      setItem: (key, v) => {
        value = v;
      },
    },
    "key",
    operation,
  );
  assert.equal(value.includes("private"), false);
  assert.equal(saved.submitted, true);
  assert.throws(
    () =>
      persistAttachmentOperation(
        {
          setItem: () => {
            throw Error("storage unavailable");
          },
        },
        "key",
        operation,
      ),
    /storage unavailable/,
  );
});
