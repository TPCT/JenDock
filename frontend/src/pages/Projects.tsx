import { PlusIcon, RepoIcon, ZapIcon } from "@primer/octicons-react";
import { Button, ConfirmationDialog, Heading } from "@primer/react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { useEnableAutoDeploy, type AutoDeployTarget } from "../api/adminHooks";
import { useAllProjects, useProjects } from "../api/hooks";
import { ENVIRONMENTS, type Project } from "../api/types";
import { EnvironmentCard } from "../components/EnvironmentCard";
import { Callout, EmptyState, errorMessage } from "../components/Feedback";
import { PagedList } from "../components/PagedList";
import { useListParams, type ListSpec, type SortOption } from "../lib/listParams";
import { capitalize } from "../lib/format";
import { PageHead } from "../components/PageHead";
import { useCan } from "../lib/permissions";
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
            <EnvironmentCard key={name} project={project} environment={environment} compact />
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
          className={styles.bulk}
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

const PROJECT_SORTS: readonly SortOption[] = [
  { value: "name", label: "Name", sort: "name" },
  { value: "newest", label: "Newest", sort: "created", order: "desc" },
  { value: "oldest", label: "Oldest", sort: "created", order: "asc" },
];
const PROJECT_LIST: ListSpec = { sorts: PROJECT_SORTS };

export function ProjectsPage() {
  const list = useListParams(PROJECT_LIST);
  const projects = useProjects(list.apiQuery);
  const everything = useAllProjects();
  const can = useCan();

  return (
    <>
      <PageHead
        title="Projects"
        actions={
          can("project.create") ? (
            <Button as={Link} to="/projects/new" variant="primary" leadingVisual={PlusIcon}>
              New project
            </Button>
          ) : null
        }
        description="Each project has a Development and a Production environment, each with its own branch."
      />
      <div className={`${box.section} ${box.stack}`}>
        {everything.data && everything.data.length > 0 ? (
          <BulkAutoDeploy projects={everything.data} />
        ) : null}
        <PagedList
          noun="projects"
          query={projects}
          list={list}
          sorts={PROJECT_SORTS}
          placeholder="Find a project by name, slug or repository…"
          empty={
            <EmptyState
              icon={<RepoIcon size={32} aria-hidden="true" />}
              title="No projects yet"
              action={
                can("project.create") ? (
                  <Button as={Link} to="/projects/new" variant="primary" leadingVisual={PlusIcon}>
                    Create your first project
                  </Button>
                ) : undefined
              }
            >
              {can("project.create")
                ? "A project connects one GitHub repository to a Development and a Production environment. It appears here with both environments as soon as it exists."
                : "Nothing has been shared with you yet. Ask an owner or admin to give you access to a project."}
            </EmptyState>
          }
        >
          {(items) => (
            <ul className={box.list} aria-label="Projects">
              {items.map((project) => (
                <li key={project.slug} className={box.row}>
                  <ProjectBox project={project} />
                </li>
              ))}
            </ul>
          )}
        </PagedList>
      </div>
    </>
  );
}
