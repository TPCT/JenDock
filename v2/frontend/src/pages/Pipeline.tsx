import { WorkflowIcon } from "@primer/octicons-react";
import { Link, useParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { usePipeline, useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { useCrumbs } from "../components/Breadcrumbs";
import { runsPath } from "../components/EnvironmentCard";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { Workspace } from "../pipeline/Workspace";
import { capitalize } from "../lib/format";

export default function PipelinePage() {
  const { slug = "", env: rawEnv } = useParams();
  const env: EnvironmentName | null = ENVIRONMENTS.find((name) => name === rawEnv) ?? null;
  const project = useProject(slug);
  const pipeline = usePipeline(slug, env ?? "development");
  useCrumbs([
    { label: "Projects", to: "/projects" },
    { label: project.data?.name ?? slug, to: `/projects/${encodeURIComponent(slug)}` },
    { label: `${capitalize(env ?? "unknown")} pipeline` },
  ]);

  if (env === null) {
    return (
      <EmptyState
        icon={<WorkflowIcon size={32} aria-hidden="true" />}
        title="Unknown environment"
        action={
          <Link className="btn" to={`/projects/${encodeURIComponent(slug)}`}>
            Back to project
          </Link>
        }
      >
        Every project has exactly two environments: development and production.
      </EmptyState>
    );
  }
  if (project.isPending || pipeline.isPending) return <Loading label="Loading the pipeline" />;
  const failure = project.error ?? pipeline.error;
  if (failure) {
    if (isApiError(failure) && failure.status === 404) {
      return (
        <EmptyState
          icon={<WorkflowIcon size={32} aria-hidden="true" />}
          title="Project not found"
          action={
            <Link className="btn" to="/projects">
              Back to projects
            </Link>
          }
        >
          There is no project called {slug}.
        </EmptyState>
      );
    }
    return (
      <ErrorCallout
        title="Could not load the pipeline"
        error={failure}
        onRetry={() => {
          void project.refetch();
          void pipeline.refetch();
        }}
      />
    );
  }
  if (!project.data || !pipeline.data) return null;
  return (
    <>
      <div className="page-header">
        <div>
          <h1>
            {project.data.name} · {capitalize(env)} pipeline
          </h1>
          <p className="subtitle">
            The steps that build, test and deploy this environment.{" "}
            <Link to={runsPath(slug, env)}>View runs</Link>
          </p>
        </div>
      </div>
      <Workspace key={`${slug}/${env}`} project={project.data} env={env} doc={pipeline.data} />
    </>
  );
}
