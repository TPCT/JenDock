import { Heading } from "@primer/react";
import type { ReactNode } from "react";
import box from "../styles/box.module.css";
import styles from "./SettingsSection.module.css";

/**
 * GitHub's settings section: a large heading with a rule, an intro, then the content. With
 * `danger` the body gets the red border of GitHub's "Danger Zone".
 */
export function SettingsSection({
  title,
  intro,
  actions,
  danger = false,
  children,
}: {
  title: string;
  intro?: ReactNode;
  actions?: ReactNode;
  danger?: boolean;
  children?: ReactNode;
}) {
  return (
    <section className={styles.section} aria-label={title}>
      <div className={styles.head}>
        <Heading
          as="h2"
          className={danger ? `${styles.title} ${styles.dangerTitle}` : styles.title}
        >
          {title}
        </Heading>
        {actions}
      </div>
      {intro ? <p className={styles.intro}>{intro}</p> : null}
      {children ? (
        <div className={danger ? `${box.box} ${styles.danger}` : box.stack}>{children}</div>
      ) : null}
    </section>
  );
}

/** One row of a danger zone or a list: text on the left, the action on the right. */
export function SettingsRow({
  title,
  description,
  action,
}: {
  title: string;
  description?: ReactNode;
  action: ReactNode;
}) {
  return (
    <div className={`${box.row} ${styles.row}`}>
      <div className={styles.rowText}>
        <strong>{title}</strong>
        {description ? <div className="muted">{description}</div> : null}
      </div>
      {action}
    </div>
  );
}
