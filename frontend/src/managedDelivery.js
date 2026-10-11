const outcomes = {
  succeeded: { label: "Deployed", tone: "success", icon: "succeeded" },
  pending: { label: "Pending", tone: "pending", icon: "queued" },
  queued: { label: "Queued", tone: "pending", icon: "queued" },
  running: { label: "In progress", tone: "pending", icon: "deploying" },
  reconciling: {
    label: "Reconciling runtime",
    tone: "pending",
    icon: "recovering",
  },
  unchanged: {
    label: "Unchanged · previous image",
    tone: "neutral",
    icon: "cancelled",
  },
  "skipped-stopped": {
    label: "Skipped · stopped",
    tone: "neutral",
    icon: "cancelled",
  },
  "rolled-back": { label: "Rolled back", tone: "failure", icon: "rolled-back" },
  "recovery-required": {
    label: "Recovery required",
    tone: "failure",
    icon: "failed",
  },
  failed: { label: "Failed", tone: "failure", icon: "failed" },
};
const text = (value) =>
  typeof value === "string" && value.trim() ? value : null;
export function environmentLabel(value) {
  return value === "development"
    ? "Development"
    : value === "production"
      ? "Production"
      : text(value) || "Not recorded";
}
export function deliveryOutcome(status) {
  return typeof status === "string" && Object.hasOwn(outcomes, status)
    ? outcomes[status]
    : {
        label: "Unknown outcome",
        tone: "neutral",
        icon: "unknown",
      };
}

// Never infer container success from the job's global status. A partial rollback
// restores only the affected container; earlier successful updates stay visible.
export function managedDeliverySummary(job) {
  const delivery = job?.managed_delivery;
  if (!delivery || typeof delivery !== "object" || Array.isArray(delivery))
    return null;
  const services = (
    Array.isArray(delivery.services) ? delivery.services : []
  ).map((service, index) => ({
    key: `${text(service?.service_id) || "service"}-${index}`,
    name:
      text(service?.name) || text(service?.service_id) || "Unnamed container",
    status: service?.status,
    ...deliveryOutcome(service?.status),
  }));
  const deployed = services.filter(
    (service) => service.status === "succeeded",
  ).length;
  const skipped = services.filter(
    (service) => service.status === "skipped-stopped",
  ).length;
  const failed = services.some((service) => service.tone === "failure");
  const unresolved = services.some(
    (service) => !["succeeded", "skipped-stopped"].includes(service.status),
  );
  let summary = deliveryOutcome(delivery.status);
  if (failed || summary.tone === "failure") {
    summary = {
      label: deployed
        ? "Partially deployed · attention required"
        : "Deployment requires attention",
      tone: "failure",
      icon: "failed",
    };
  } else if (delivery.status === "succeeded") {
    summary =
      !services.length || unresolved
        ? {
            label: "Deployment outcome incomplete",
            tone: "neutral",
            icon: "unknown",
          }
        : skipped
          ? {
              label: deployed
                ? "Deployed with stopped containers skipped"
                : "Stopped containers skipped",
              tone: "neutral",
              icon: "cancelled",
            }
          : outcomes.succeeded;
  }
  return {
    ...summary,
    environment:
      text(delivery.environment) ||
      text(job.target?.environment) ||
      "Not recorded",
    detail: text(delivery.detail),
    services,
    deployed,
    skipped,
  };
}
