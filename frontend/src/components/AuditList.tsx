import { Label } from "@primer/react";
import type { AuditEvent } from "../api/accountTypes";
import box from "../styles/box.module.css";
import styles from "./AuditList.module.css";
import { RelativeTime } from "./Time";

const ACTOR_LABEL = { user: "User", token: "API token", system: "System" } as const;

/** The category of an action, `secret.reveal` -> `secret`, for the small label. */
function category(action: string): string {
  return action.split(".")[0] ?? action;
}

/**
 * One audit event per row, GitHub's security log: what happened in words, who did it, when,
 * and the raw details (already free of secrets) behind a disclosure.
 */
export function AuditList({ events, label }: { events: AuditEvent[]; label: string }) {
  return (
    <ul className={box.list} aria-label={label}>
      {events.map((event) => (
        <li key={event.id} className={`${box.row} ${styles.item}`}>
          <div className={styles.main}>
            <span className={styles.description}>{event.description}</span>
            <span className={`muted ${styles.meta}`}>
              <span>{event.actor}</span>
              <span aria-hidden="true">·</span>
              <RelativeTime iso={event.occurred_at} />
              {event.target_id ? (
                <>
                  <span aria-hidden="true">·</span>
                  <span>
                    {event.target_type} <code>{event.target_id}</code>
                  </span>
                </>
              ) : null}
            </span>
            {Object.keys(event.details).length > 0 ? (
              <details className={styles.details}>
                <summary>Details</summary>
                <pre className={styles.json}>{JSON.stringify(event.details, null, 2)}</pre>
              </details>
            ) : null}
          </div>
          <span className={styles.labels}>
            <Label>{category(event.action)}</Label>
            <span className="muted">{ACTOR_LABEL[event.actor_type]}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}
