import { PlusIcon, RepoIcon, ZapIcon } from "@primer/octicons-react";
import { Button, ConfirmationDialog, Heading } from "@primer/react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useEnableAutoDeploy, type AutoDeployTarget } from "../api/adminHooks";
import { useProjects } from "../api/hooks";
import { ENVIRONMENTS, type Project } from "../api/types";
import { EnvironmentCard } from "../components/EnvironmentCard";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { capitalize } from "../lib/format";
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

interface Candidate extends AutoDeployTarget {
  project: string;
  branch: string;
}

/** Environments that can deploy by themselves but are set to manual: compose settings and a pipeline exist. */
export function autoDeployCandidates(projects: Project[]): Candidate[] {
  return projects.flatMap((project) =>
    ENVIRONMENTS.flatMap((name) => {
      const environment = project.environments.find((item) => item.name === name);
      if (
        !environment ||
        environment.auto_deploy ||
        !environment.compose_project ||
        environment.compose_files.length === 0 ||
        !environment.has_pipeline
      ) {
        return [];
      }
      return [{ slug: project.slug, env: name, project: project.name, branch: environment.branch }];
    }),
  );
}

function BulkAutoDeploy({ projects }: { projects: Project[] }) {
  const [open, setOpen] = useState(false);
  const enable = useEnableAutoDeploy();
  const candidates = autoDeployCandidates(projects);
  const result = enable.data;
  return (
    <div className={box.stack}>
      <div className={box.cluster}>
        <Button
          leadingVisual={ZapIcon}
          disabled={candidates.length === 0 || enable.isPending}
          onClick={() => {
            enable.reset();
            setOpen(true);
          }}
        >
          Turn on automatic deploy for all configured environments
        </Button>
        {candidates.length === 0 ? (
          <span className="muted">
            Nothing to turn on: every environment with compose settings and a pipeline already
            deploys automatically.
          </span>
        ) : (
          <span className="muted">
            {candidates.length} {candidates.length === 1 ? "environment is" : "environments are"} on
            manual deploys.
          </span>
        )}
      </div>
      {result && result.enabled.length > 0 ? (
        <Callout variant="success" role="status">
          Automatic deploy is on for {result.enabled.length}{" "}
          {result.enabled.length === 1 ? "environment" : "environments"}.
        </Callout>
      ) : null}
      {result?.failed.map((item) => (
        <Callout
          key={`${item.target.slug}/${item.target.env}`}
          variant="danger"
          role="alert"
          title={`Could not change ${item.target.slug} ${item.target.env}`}
        >
          {item.message}
        </Callout>
      ))}
      {enable.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(enable.error)}
        </Callout>
      ) : null}
      {open ? (
        <ConfirmationDialog
          title="Turn on automatic deploy?"
          confirmButtonContent={`Turn on for ${String(candidates.length)}`}
          cancelButtonContent="Cancel"
          confirmButtonLoading={enable.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") {
              enable.mutate(
                candidates.map(({ slug, env }) => ({ slug, env })),
                {
                  onSettled: () => {
                    setOpen(false);
                  },
                },
              );
            } else setOpen(false);
          }}
        >
          <p>
            From now on every push to these branches builds and deploys. Only environments that have
            Docker Compose settings and a pipeline are listed.
          </p>
          <ul aria-label="Environments that will deploy automatically">
            {candidates.map((item) => (
              <li key={`${item.slug}/${item.env}`}>
                <strong>{item.project}</strong> · {capitalize(item.env)} ·{" "}
                <code>{item.branch}</code>
              </li>
            ))}
          </ul>
        </ConfirmationDialog>
      ) : null}
    </div>
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
      <div className={`${box.section} ${box.stack}`}>
        {projects.isPending ? <Loading label="Loading projects" /> : null}
        {projects.isError ? (
          <ErrorCallout
            title="Could not load projects"
            error={projects.error}
            onRetry={() => void projects.refetch()}
          />
        ) : null}
        {projects.data?.length === 0 ? (
          <EmptyState
            icon={<RepoIcon size={32} aria-hidden="true" />}
            title="No projects yet"
            action={
              <Button as={Link} to="/projects/new" variant="primary" leadingVisual={PlusIcon}>
                Create your first project
              </Button>
            }
          >
            A project connects one GitHub repository to a Development and a Production environment.
            It appears here with both environments as soon as it exists.
          </EmptyState>
        ) : null}
        {projects.data && projects.data.length > 0 ? (
          <BulkAutoDeploy projects={projects.data} />
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
