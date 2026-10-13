import { HistoryIcon, WorkflowIcon } from "@primer/octicons-react";
import { Button, LinkButton } from "@primer/react";
import { Link, useParams } from "react-router-dom";
import { usePipeline, useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { EnvToolbar } from "../components/EnvSwitch";
import { pipelinePath, runsPath } from "../components/EnvironmentCard";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";
import { Workspace } from "../pipeline/Workspace";

export default function PipelinePage() {
  const { slug = "", env: rawEnv } = useParams();
  const env: EnvironmentName | null = ENVIRONMENTS.find((name) => name === rawEnv) ?? null;
  const project = useProject(slug);
  const pipeline = usePipeline(slug, env ?? "development");

  if (env === null) {
    return (
      <EmptyState
        icon={<WorkflowIcon size={32} aria-hidden="true" />}
        title="Unknown environment"
        action={
          <Button as={Link} to={`/projects/${encodeURIComponent(slug)}`}>
            Back to project
          </Button>
        }
      >
        Every project has exactly two environments: development and production.
      </EmptyState>
    );
  }
  if (project.isPending || pipeline.isPending) return <Loading label="Loading the pipeline" />;
  const failure = project.error ?? pipeline.error;
  if (failure) {
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
    <div className={box.stack}>
      <EnvToolbar
        title={`${capitalize(env)} pipeline`}
        subtitle="The steps that build, test and deploy this environment."
        env={env}
        pathFor={(next) => pipelinePath(slug, next)}
        actions={
          <LinkButton as={Link} to={runsPath(slug, env)} leadingVisual={HistoryIcon}>
            View runs
          </LinkButton>
        }
      />
      <Workspace key={`${slug}/${env}`} project={project.data} env={env} doc={pipeline.data} />
    </div>
  );
}
