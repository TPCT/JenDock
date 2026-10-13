import { Link, useParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { useProject } from "../api/hooks";
import { ENVIRONMENTS } from "../api/types";
import { useCrumbs } from "../components/Breadcrumbs";
import { EnvironmentCard } from "../components/EnvironmentCard";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { RepoIcon } from "@primer/octicons-react";

export function ProjectOverviewPage() {
  const { slug = "" } = useParams();
  const project = useProject(slug);
  useCrumbs([{ label: "Projects", to: "/projects" }, { label: project.data?.name ?? slug }]);

  if (project.isPending) return <Loading label="Loading project" />;
  if (project.isError) {
    if (isApiError(project.error) && project.error.status === 404) {
      return (
        <EmptyState
          icon={<RepoIcon size={32} aria-hidden="true" />}
          title="Project not found"
          action={
            <Link className="btn" to="/projects">
              Back to projects
            </Link>
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

  const data = project.data;
  return (
    <>
      <div className="page-header">
        <div>
          <h1>{data.name}</h1>
          <p className="subtitle ltr">{data.repository}</p>
        </div>
      </div>
      <div className="box">
        <div className="env-pair">
          {ENVIRONMENTS.map((name) => {
            const environment = data.environments.find((item) => item.name === name);
            return environment ? (
              <EnvironmentCard key={name} project={data} environment={environment} detailed />
            ) : null;
          })}
        </div>
      </div>
    </>
  );
}
