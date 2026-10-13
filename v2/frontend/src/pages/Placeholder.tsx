import { DatabaseIcon, GearIcon, GlobeIcon, PackageIcon, SearchIcon } from "@primer/octicons-react";
import { Button } from "@primer/react";
import { Link, useLocation } from "react-router-dom";
import { EmptyState } from "../components/Feedback";
import { PageHead, useDocumentTitle } from "../components/PageHead";

export function SettingsPlaceholderPage() {
  return (
    <>
      <PageHead title="Settings" />
      <EmptyState
        icon={<GearIcon size={32} aria-hidden="true" />}
        title="Settings are coming soon"
        action={
          <Button as={Link} to="/projects">
            Go to projects
          </Button>
        }
      >
        GitHub connection, secrets, API tokens and users will be managed here in a later release.
      </EmptyState>
    </>
  );
}

const TAB_TEXT = {
  containers: {
    icon: PackageIcon,
    title: "Containers are coming soon",
    text: "Running containers, their logs and resource use will be listed here.",
  },
  database: {
    icon: DatabaseIcon,
    title: "Database is coming soon",
    text: "Provisioned databases and backups for this project will be managed here.",
  },
  domains: {
    icon: GlobeIcon,
    title: "Domains are coming soon",
    text: "Custom domains and certificates for each environment will be managed here.",
  },
  settings: {
    icon: GearIcon,
    title: "Project settings are coming soon",
    text: "Branches, auto-deploy, secrets and access for this project will be managed here.",
  },
} as const;

/** The project tabs whose backend does not exist yet. */
export function ProjectTabPlaceholder() {
  const { pathname } = useLocation();
  const key = pathname.split("/").pop() ?? "";
  const entry = key in TAB_TEXT ? TAB_TEXT[key as keyof typeof TAB_TEXT] : null;
  const Icon = entry?.icon ?? SearchIcon;
  return (
    <EmptyState
      icon={<Icon size={32} aria-hidden="true" />}
      title={entry?.title ?? "Nothing here yet"}
    >
      {entry?.text ?? "This section is not available yet."}
    </EmptyState>
  );
}

export function NotFoundPage() {
  useDocumentTitle("Page not found");
  return (
    <EmptyState
      icon={<SearchIcon size={32} aria-hidden="true" />}
      title="Page not found"
      action={
        <Button as={Link} to="/projects">
          Go to projects
        </Button>
      }
    >
      This address does not match any page. Check the link or start from the project list.
    </EmptyState>
  );
}
