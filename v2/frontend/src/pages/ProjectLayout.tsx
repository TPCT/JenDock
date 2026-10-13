import {
  type Icon,
  DatabaseIcon,
  GearIcon,
  GlobeIcon,
  HomeIcon,
  PackageIcon,
  PlayIcon,
  RepoIcon,
  WorkflowIcon,
} from "@primer/octicons-react";
import { Button, UnderlineNav } from "@primer/react";
import { Link, Outlet, useLocation, useParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import box from "../styles/box.module.css";

type TabId = "overview" | "runs" | "pipeline" | "containers" | "database" | "domains" | "settings";

interface Tab {
  id: TabId;
  label: string;
  icon: Icon;
}

export const PROJECT_TABS: readonly Tab[] = [
  { id: "overview", label: "Overview", icon: HomeIcon },
  { id: "runs", label: "Runs", icon: PlayIcon },
  { id: "pipeline", label: "Pipeline", icon: WorkflowIcon },
  { id: "containers", label: "Containers", icon: PackageIcon },
  { id: "database", label: "Database", icon: DatabaseIcon },
  { id: "domains", label: "Domains", icon: GlobeIcon },
  { id: "settings", label: "Settings", icon: GearIcon },
];

function activeTab(pathname: string): TabId {
  if (pathname.includes("/runs")) return "runs";
  if (pathname.includes("/pipeline")) return "pipeline";
  if (pathname.includes("/domains")) return "domains";
  if (pathname.includes("/containers")) return "containers";
  if (pathname.includes("/databases")) return "database";
  if (pathname.includes("/settings")) return "settings";
  return "overview";
}

function currentEnv(pathname: string): EnvironmentName {
  return ENVIRONMENTS.find((name) => pathname.includes(`/environments/${name}/`)) ?? "development";
}

/** GitHub's repository frame: header with the project name and a tab strip, page below. */
export function ProjectLayout() {
  const { slug = "" } = useParams();
  const { pathname } = useLocation();
  const project = useProject(slug);

  if (project.isPending) return <Loading label="Loading project" />;
  if (project.isError) {
    if (isApiError(project.error) && project.error.status === 404) {
      return (
        <EmptyState
          icon={<RepoIcon size={32} aria-hidden="true" />}
          title="Project not found"
          action={
            <Button as={Link} to="/projects">
              Back to projects
            </Button>
          }
        >
          There is no project called {slug}. It may have been archived or the address is mistyped.
        </EmptyState>
      );
    }
    return (
      <ErrorCallout
        title="Could not load this project"
        error={project.error}
        onRetry={() => void project.refetch()}
      />
    );
  }

  const base = `/projects/${encodeURIComponent(slug)}`;
  const env = currentEnv(pathname);
  const current = activeTab(pathname);
  const hrefFor = (id: TabId): string => {
    switch (id) {
      case "overview":
        return base;
      case "runs":
        return `${base}/environments/${env}/runs`;
      case "pipeline":
        return `${base}/environments/${env}/pipeline`;
      case "domains":
        return `${base}/environments/${env}/domains`;
      case "containers":
        return `${base}/environments/${env}/containers`;
      case "database":
        return `${base}/environments/${env}/databases`;
      default:
        return `${base}/${id}`;
    }
  };
  const tab = PROJECT_TABS.find((item) => item.id === current);

  return (
    <>
      <PageHead
        crumbs={[{ label: "Projects", to: "/projects" }, { label: project.data.name }]}
        title={project.data.name}
        docTitle={
          tab && tab.id !== "overview" ? `${tab.label} · ${project.data.name}` : project.data.name
        }
        leading={<RepoIcon size={24} />}
        description={<span className="ltr">{project.data.repository}</span>}
        navigation={
          <UnderlineNav aria-label="Project">
            {PROJECT_TABS.map((item) => (
              <UnderlineNav.Item
                key={item.id}
                as={Link}
                to={hrefFor(item.id)}
                leadingVisual={<item.icon />}
                aria-current={item.id === current ? "page" : undefined}
              >
                {item.label}
              </UnderlineNav.Item>
            ))}
          </UnderlineNav>
        }
      />
      <div className={box.section}>
        <Outlet />
      </div>
    </>
  );
}
