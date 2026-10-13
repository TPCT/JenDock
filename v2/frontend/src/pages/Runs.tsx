import { HistoryIcon, PlayIcon } from "@primer/octicons-react";
import { useCallback, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { RUNS_PAGE_SIZE, useAllRuns, useEnvironmentRuns, useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName, type Page, type RunSummary } from "../api/types";
import { useCrumbs } from "../components/Breadcrumbs";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { RunList } from "../components/RunList";
import { StartRunDialog } from "../components/StartRunDialog";
import { capitalize } from "../lib/format";

function Pager({ page, onChange }: { page: Page<RunSummary>; onChange: (offset: number) => void }) {
  const from = page.total === 0 ? 0 : page.offset + 1;
  const to = Math.min(page.offset + page.limit, page.total);
  return (
    <div className="pager">
      <span className="muted">
        {from}-{to} of {page.total}
      </span>
      <div className="row-actions">
        <button
          type="button"
          className="btn"
          disabled={page.offset === 0}
          onClick={() => {
            onChange(Math.max(0, page.offset - RUNS_PAGE_SIZE));
          }}
        >
          Newer
        </button>
        <button
          type="button"
          className="btn"
          disabled={page.offset + page.limit >= page.total}
          onClick={() => {
            onChange(page.offset + RUNS_PAGE_SIZE);
          }}
        >
          Older
        </button>
      </div>
    </div>
  );
}

function isEnvironment(value: string | undefined): value is EnvironmentName {
  return ENVIRONMENTS.some((name) => name === value);
}

export function EnvironmentRunsPage() {
  const { slug = "", env: rawEnv } = useParams();
  const env: EnvironmentName = isEnvironment(rawEnv) ? rawEnv : "development";
  const [params, setParams] = useSearchParams();
  const [offset, setOffset] = useState(0);
  const project = useProject(slug);
  const runs = useEnvironmentRuns(slug, env, offset);
  const environment = project.data?.environments.find((item) => item.name === env);
  const dialogOpen = params.get("run") === "new" && environment !== undefined;

  useCrumbs([
    { label: "Projects", to: "/projects" },
    { label: project.data?.name ?? slug, to: `/projects/${encodeURIComponent(slug)}` },
    { label: `${capitalize(env)} runs` },
  ]);

  const closeDialog = useCallback(() => {
    setParams({}, { replace: true });
  }, [setParams]);

  if (!isEnvironment(rawEnv)) {
    return (
      <EmptyState
        icon={<HistoryIcon size={32} aria-hidden="true" />}
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

  return (
    <>
      <div className="page-header">
        <div>
          <h1>
            {project.data?.name ?? slug} · {capitalize(env)}
          </h1>
          <p className="subtitle">
            {environment ? (
              <>
                Branch <code>{environment.branch}</code>
                {environment.auto_deploy ? ", deploys automatically on push" : ", manual deploys"}
              </>
            ) : (
              "Runs of this environment"
            )}
          </p>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!environment}
          onClick={() => {
            setParams({ run: "new" });
          }}
        >
          <PlayIcon size={16} aria-hidden="true" />
          Run pipeline
        </button>
      </div>
      {runs.isPending ? <Loading label="Loading runs" /> : null}
      {runs.isError ? (
        <ErrorCallout
          title="Could not load runs"
          error={runs.error}
          onRetry={() => void runs.refetch()}
        />
      ) : null}
      {runs.data?.total === 0 ? (
        <div className="box">
          <EmptyState icon={<HistoryIcon size={32} aria-hidden="true" />} title="No runs yet">
            A run builds and deploys one commit.{" "}
            {environment?.auto_deploy
              ? `Push to ${environment.branch} on GitHub to start the first one, or press Run pipeline.`
              : "Press Run pipeline to start the first one."}
          </EmptyState>
        </div>
      ) : null}
      {runs.data && runs.data.total > 0 ? (
        <div className="box">
          <RunList runs={runs.data.items} />
          <Pager page={runs.data} onChange={setOffset} />
        </div>
      ) : null}
      {dialogOpen ? (
        <StartRunDialog
          slug={slug}
          env={env}
          defaultBranch={environment.branch}
          onClose={closeDialog}
        />
      ) : null}
    </>
  );
}

export function AllRunsPage() {
  useCrumbs([{ label: "Runs" }]);
  const [offset, setOffset] = useState(0);
  const runs = useAllRuns(offset);
  return (
    <>
      <div className="page-header">
        <div>
          <h1>Runs</h1>
          <p className="subtitle">The latest runs across all projects and environments.</p>
        </div>
      </div>
      {runs.isPending ? <Loading label="Loading runs" /> : null}
      {runs.isError ? (
        <ErrorCallout
          title="Could not load runs"
          error={runs.error}
          onRetry={() => void runs.refetch()}
        />
      ) : null}
      {runs.data?.total === 0 ? (
        <div className="box">
          <EmptyState
            icon={<HistoryIcon size={32} aria-hidden="true" />}
            title="No runs yet"
            action={
              <Link className="btn" to="/projects">
                Go to projects
              </Link>
            }
          >
            Runs appear here after a push to a project branch or a manual run. Open a project to
            start one.
          </EmptyState>
        </div>
      ) : null}
      {runs.data && runs.data.total > 0 ? (
        <div className="box">
          <RunList runs={runs.data.items} showProject />
          <Pager page={runs.data} onChange={setOffset} />
        </div>
      ) : null}
    </>
  );
}
