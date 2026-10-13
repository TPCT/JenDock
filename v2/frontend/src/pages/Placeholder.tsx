import { GearIcon } from "@primer/octicons-react";
import { Link } from "react-router-dom";
import { useCrumbs } from "../components/Breadcrumbs";
import { EmptyState } from "../components/Feedback";

export function SettingsPlaceholderPage() {
  useCrumbs([{ label: "Settings" }]);
  return (
    <EmptyState
      icon={<GearIcon size={32} aria-hidden="true" />}
      title="Settings are coming soon"
      action={
        <Link className="btn" to="/projects">
          Go to projects
        </Link>
      }
    >
      GitHub connection, secrets, API tokens and users will be managed here in a later release.
    </EmptyState>
  );
}

export function NotFoundPage() {
  useCrumbs([{ label: "Page not found" }]);
  return (
    <EmptyState
      icon={<GearIcon size={32} aria-hidden="true" />}
      title="Page not found"
      action={
        <Link className="btn" to="/projects">
          Go to projects
        </Link>
      }
    >
      This address does not match any page. Check the link or start from the project list.
    </EmptyState>
  );
}
