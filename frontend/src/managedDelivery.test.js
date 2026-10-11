import test from "node:test";
import assert from "node:assert/strict";
import { deliveryOutcome, managedDeliverySummary } from "./managedDelivery.js";
const job = (status, states) => ({
  status: "succeeded",
  target: { environment: "development" },
  managed_delivery: {
    status,
    environment: "production",
    services: states.map((status, i) => ({
      service_id: `s${i}`,
      name: `Container ${i}`,
      status,
    })),
  },
});
test("mixed rollback preserves successful container and overrides global job success", () => {
  const result = managedDeliverySummary(
    job("failed", ["succeeded", "rolled-back", "pending"]),
  );
  assert.equal(result.environment, "production");
  assert.equal(result.label, "Partially deployed · attention required");
  assert.deepEqual(
    result.services.map((s) => s.label),
    ["Deployed", "Rolled back", "Pending"],
  );
  assert.equal(result.tone, "failure");
});
test("success with stopped services is explicitly partial, never a blanket deployed claim", () => {
  assert.equal(
    managedDeliverySummary(job("succeeded", ["succeeded", "skipped-stopped"]))
      .label,
    "Deployed with stopped containers skipped",
  );
  assert.equal(
    managedDeliverySummary(job("succeeded", ["skipped-stopped"])).label,
    "Stopped containers skipped",
  );
});
test("missing, malformed and future states cannot become success", () => {
  assert.equal(managedDeliverySummary({}), null);
  for (const states of [[], ["pending"], ["new-future-state"]])
    assert.equal(
      managedDeliverySummary(job("succeeded", states)).label,
      "Deployment outcome incomplete",
    );
  assert.equal(deliveryOutcome("new-future-state").label, "Unknown outcome");
  assert.equal(
    managedDeliverySummary({ managed_delivery: { services: [null, {}] } })
      .services[0].name,
    "Unnamed container",
  );
});
test("verified delivery and recovery retain their distinct evidence", () => {
  assert.equal(
    managedDeliverySummary(job("succeeded", ["succeeded"])).label,
    "Deployed",
  );
  assert.equal(
    managedDeliverySummary(job("recovery-required", ["pending"])).tone,
    "failure",
  );
  assert.equal(
    managedDeliverySummary(job("running", ["pending"])).label,
    "In progress",
  );
});

test("recovery observations distinguish unchanged services from rollback and stay pending until terminal", () => {
  const result = managedDeliverySummary(
    job("reconciling", ["succeeded", "unchanged", "pending"]),
  );
  assert.equal(result.label, "Reconciling runtime");
  assert.equal(result.tone, "pending");
  assert.equal(result.services[1].label, "Unchanged · previous image");
  assert.equal(result.services[1].tone, "neutral");
  assert.equal(
    managedDeliverySummary(job("reconciling", ["succeeded"])).tone,
    "pending",
  );
  assert.equal(
    managedDeliverySummary(job("succeeded", ["unchanged"])).label,
    "Deployment outcome incomplete",
  );
});
