import { AlertIcon, InfoIcon } from "@primer/octicons-react";
import { Button, SkeletonBox, Stack, VisuallyHidden } from "@primer/react";
import { Flash } from "@primer/react/deprecated";
import { Blankslate } from "@primer/react/experimental";
import type { ReactNode } from "react";
import { isApiError } from "../api/client";
import styles from "./Feedback.module.css";

export function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message;
  return "Something went wrong. Try again.";
}

/**
 * Primer's Flash, which the product owner asked for. Primer now recommends Banner and marks
 * Flash deprecated, so the single reference lives here.
 */
// eslint-disable-next-line @typescript-eslint/no-deprecated -- see above
export const FlashMessage = Flash;

/** A Flash message with an icon, optional bold title and actions, GitHub style. */
export function Callout({
  variant = "default",
  title,
  children,
  actions,
  role,
}: {
  variant?: "default" | "warning" | "success" | "danger";
  title?: string;
  children?: ReactNode;
  actions?: ReactNode;
  role?: "alert" | "status";
}) {
  const Icon = variant === "danger" || variant === "warning" ? AlertIcon : InfoIcon;
  return (
    <FlashMessage variant={variant} role={role} className={styles.callout}>
      <Icon size={16} className={styles.icon} aria-hidden="true" />
      <div className={styles.body}>
        {title ? <strong>{title}</strong> : null}
        {children ? <div>{children}</div> : null}
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>
    </FlashMessage>
  );
}

export function ErrorCallout({
  title = "Could not load this page",
  error,
  onRetry,
}: {
  title?: string;
  error: unknown;
  onRetry?: () => void;
}) {
  return (
    <Callout
      variant="danger"
      role="alert"
      title={title}
      actions={onRetry ? <Button onClick={onRetry}>Try again</Button> : null}
    >
      {errorMessage(error)}
    </Callout>
  );
}

export function InfoCallout({ children }: { children: ReactNode }) {
  return <Callout>{children}</Callout>;
}

export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Blankslate border spacious>
      <Blankslate.Visual>{icon}</Blankslate.Visual>
      <Blankslate.Heading as="h2">{title}</Blankslate.Heading>
      <Blankslate.Description>{children}</Blankslate.Description>
      {action ? <div className={styles.emptyAction}>{action}</div> : null}
    </Blankslate>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite">
      <VisuallyHidden>{label}…</VisuallyHidden>
      <Stack gap="condensed" aria-hidden="true">
        <SkeletonBox height="2rem" width="40%" />
        <SkeletonBox height="5rem" />
        <SkeletonBox height="5rem" />
      </Stack>
    </div>
  );
}
