import { BellIcon } from "@primer/octicons-react";
import { Label } from "@primer/react";
import { useDeliveries } from "../api/notificationHooks";
import type { Delivery, DeliveryStatus } from "../api/notificationTypes";
import { EmptyState, ErrorCallout, Loading } from "./Feedback";
import { SettingsRow } from "./SettingsSection";
import { RelativeTime } from "./Time";
import box from "../styles/box.module.css";

const STATUS: Record<
  DeliveryStatus,
  { text: string; variant: "success" | "attention" | "danger" }
> = {
  sent: { text: "Sent", variant: "success" },
  pending: { text: "Retrying", variant: "attention" },
  sending: { text: "Sending", variant: "attention" },
  failed: { text: "Failed", variant: "danger" },
};

function statusText(delivery: Delivery): string {
  if (delivery.status === "pending" && delivery.attempts === 0) return "Queued";
  return STATUS[delivery.status].text;
}

/** The outbox as the operator needs it: what was sent where, when, and why it failed. */
export function DeliveryHistory({ slug }: { slug: string | null }) {
  const history = useDeliveries(slug);
  if (history.isPending) return <Loading label="Loading delivery history" />;
  if (history.isError) {
    return (
      <ErrorCallout
        title="Could not load the delivery history"
        error={history.error}
        onRetry={() => void history.refetch()}
      />
    );
  }
  if (history.data.items.length === 0) {
    return (
      <EmptyState icon={<BellIcon size={32} aria-hidden="true" />} title="Nothing sent yet">
        Messages appear here with their status as soon as an event matches a rule.
      </EmptyState>
    );
  }
  return (
    <div className={box.box}>
      <ul className={box.list} aria-label="Delivery history">
        {history.data.items.map((delivery) => (
          <li key={delivery.id}>
            <SettingsRow
              title={delivery.title}
              description={
                <div className={box.stack}>
                  <span>
                    {delivery.channel_name} · <RelativeTime iso={delivery.created_at} /> ·{" "}
                    {delivery.attempts} {delivery.attempts === 1 ? "attempt" : "attempts"}
                    {delivery.suppressed > 0
                      ? ` · ${delivery.suppressed} duplicate${delivery.suppressed === 1 ? "" : "s"} collapsed`
                      : ""}
                  </span>
                  {delivery.last_error ? (
                    <span role="note">Last error: {delivery.last_error}</span>
                  ) : null}
                </div>
              }
              action={
                <span className={box.cluster}>
                  {delivery.is_test ? <Label>Test</Label> : null}
                  <Label variant={STATUS[delivery.status].variant}>{statusText(delivery)}</Label>
                </span>
              }
            />
          </li>
        ))}
      </ul>
    </div>
  );
}
