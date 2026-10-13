import { PlusIcon, RepoIcon } from "@primer/octicons-react";
import { Button, Heading } from "@primer/react";
import { Link } from "react-router-dom";
import { useProjects } from "../api/hooks";
import { ENVIRONMENTS, type Project } from "../api/types";
import { EnvironmentCard } from "../components/EnvironmentCard";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import box from "../styles/box.module.css";
import styles from "./Projects.module.css";

export function ProjectBox({ project }: { project: Project }) {
  return (
    <article className={styles.project} aria-label={project.name}>
      <div className={styles.head}>
        <RepoIcon size={16} className="muted" aria-hidden="true" />
        <Heading as="h2" className={styles.name}>
          <Link to={`/projects/${encodeURIComponent(project.slug)}`}>{project.name}</Link>
        </Heading>
      </div>
      <p className={`muted ltr ${styles.repo}`}>{project.repository}</p>
      <div className={styles.envs}>
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
  const projects = useProjects();

  return (
    <>
      <PageHead
        title="Projects"
        actions={
          <Button as={Link} to="/projects/new" variant="primary" leadingVisual={PlusIcon}>
            New project
          </Button>
        }
        description="Each project has a Development and a Production environment, each with its own branch."
      />
      <div className={box.section}>
        {projects.isPending ? <Loading label="Loading projects" /> : null}
        {projects.isError ? (
          <ErrorCallout
            title="Could not load projects"
            error={projects.error}
            onRetry={() => void projects.refetch()}
          />
        ) : null}
        {projects.data?.length === 0 ? (
          <EmptyState icon={<RepoIcon size={32} aria-hidden="true" />} title="No projects yet">
            A project connects one GitHub repository to a Development and a Production environment.
            Creating projects from this screen is not available yet, so ask an administrator to
            create one through the API. It will appear here with both environments as soon as it
            exists.
          </EmptyState>
        ) : null}
        {projects.data && projects.data.length > 0 ? (
          <ul className={`${box.box} ${box.list}`} aria-label="Projects">
            {projects.data.map((project) => (
              <li key={project.slug} className={box.row}>
                <ProjectBox project={project} />
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </>
  );
}
