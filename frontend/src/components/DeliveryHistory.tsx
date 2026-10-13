import { BellIcon } from "@primer/octicons-react";
import { Label } from "@primer/react";
import { useDeliveries } from "../api/notificationHooks";
import type { Delivery, DeliveryStatus } from "../api/notificationTypes";
import { useListParams, type FilterDef, type ListSpec, type SortOption } from "../lib/listParams";
import { EmptyState } from "./Feedback";
import { PagedList } from "./PagedList";
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

const STATUS_FILTER: FilterDef = {
  param: "status",
  label: "Status",
  allLabel: "All statuses",
  options: [
    { value: "sent", label: "Sent" },
    { value: "pending", label: "Queued or retrying" },
    { value: "sending", label: "Sending" },
    { value: "failed", label: "Failed" },
  ],
};
const SORTS: readonly SortOption[] = [
  { value: "newest", label: "Newest", sort: "created", order: "desc" },
  { value: "oldest", label: "Oldest", sort: "created", order: "asc" },
];
const DELIVERY_LIST: ListSpec = { filters: [STATUS_FILTER], sorts: SORTS };

function statusText(delivery: Delivery): string {
  if (delivery.status === "pending" && delivery.attempts === 0) return "Queued";
  return STATUS[delivery.status].text;
}

/** The outbox as the operator needs it: what was sent where, when, and why it failed. */
export function DeliveryHistory({ slug }: { slug: string | null }) {
  const list = useListParams(DELIVERY_LIST);
  const history = useDeliveries(slug, list.apiQuery);
  return (
    <PagedList
      noun="messages"
      singular="message"
      query={history}
      list={list}
      filters={[STATUS_FILTER]}
      sorts={SORTS}
      placeholder="Search by title, event or error…"
      empty={
        <EmptyState icon={<BellIcon size={32} aria-hidden="true" />} title="Nothing sent yet">
          Messages appear here with their status as soon as an event matches a rule.
        </EmptyState>
      }
    >
      {(items) => (
        <ul className={box.list} aria-label="Delivery history">
          {items.map((delivery) => (
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
      )}
    </PagedList>
  );
}
