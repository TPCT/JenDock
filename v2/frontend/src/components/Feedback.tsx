import { AlertIcon, InfoIcon } from "@primer/octicons-react";
import type { ReactNode } from "react";
import { isApiError } from "../api/client";

export function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message;
  return "Something went wrong. Try again.";
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
    <div className="callout callout-danger" role="alert">
      <AlertIcon size={16} aria-hidden="true" />
      <div className="callout-body">
        <strong>{title}</strong>
        <span>{errorMessage(error)}</span>
        {onRetry ? (
          <div className="callout-actions">
            <button type="button" className="btn" onClick={onRetry}>
              Try again
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function InfoCallout({ children }: { children: ReactNode }) {
  return (
    <div className="callout">
      <InfoIcon size={16} aria-hidden="true" />
      <div className="callout-body">{children}</div>
    </div>
  );
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
    <div className="centered">
      {icon}
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="stack">
      <span className="sr-only">{label}…</span>
      <div className="skeleton" />
      <div className="skeleton" />
      <div className="skeleton" />
    </div>
  );
}
