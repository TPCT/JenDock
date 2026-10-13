import { RepoIcon } from "@primer/octicons-react";
import { Link } from "react-router-dom";
import { useProjects } from "../api/hooks";
import { ENVIRONMENTS, type Project } from "../api/types";
import { useCrumbs } from "../components/Breadcrumbs";
import { EnvironmentCard } from "../components/EnvironmentCard";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";

export function ProjectBox({ project }: { project: Project }) {
  return (
    <article className="box" aria-label={project.name}>
      <header className="box-header">
        <div>
          <h2>
            <Link to={`/projects/${encodeURIComponent(project.slug)}`}>{project.name}</Link>
          </h2>
          <p className="muted ltr">{project.repository}</p>
        </div>
      </header>
      <div className="env-pair">
        {ENVIRONMENTS.map((name) => {
          const environment = project.environments.find((item) => item.name === name);
          return environment ? (
            <EnvironmentCard key={name} project={project} environment={environment} />
          ) : null;
        })}
      </div>
    </article>
  );
}

export function ProjectsPage() {
  useCrumbs([{ label: "Projects" }]);
  const projects = useProjects();

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Projects</h1>
          <p className="subtitle">
            Each project has a Development and a Production environment, each with its own branch.
          </p>
        </div>
      </div>
      {projects.isPending ? <Loading label="Loading projects" /> : null}
      {projects.isError ? (
        <ErrorCallout
          title="Could not load projects"
          error={projects.error}
          onRetry={() => void projects.refetch()}
        />
      ) : null}
      {projects.data?.length === 0 ? (
        <div className="box">
          <EmptyState icon={<RepoIcon size={32} aria-hidden="true" />} title="No projects yet">
            A project connects one GitHub repository to a Development and a Production environment.
            Creating projects from this screen is not available yet, so ask an administrator to
            create one through the API. It will appear here with both environments as soon as it
            exists.
          </EmptyState>
        </div>
      ) : null}
      {projects.data && projects.data.length > 0 ? (
        <div className="stack">
          {projects.data.map((project) => (
            <ProjectBox key={project.slug} project={project} />
          ))}
        </div>
      ) : null}
    </>
  );
}
