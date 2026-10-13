import { HistoryIcon, PlayIcon } from "@primer/octicons-react";
import { Button, ButtonGroup } from "@primer/react";
import { useCallback, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { RUNS_PAGE_SIZE, useAllRuns, useEnvironmentRuns, useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName, type Page, type RunSummary } from "../api/types";
import { EnvToolbar } from "../components/EnvSwitch";
import { runsPath } from "../components/EnvironmentCard";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import { RunList } from "../components/RunList";
import { StartRunDialog } from "../components/StartRunDialog";
import { capitalize } from "../lib/format";
import { useCan } from "../lib/permissions";
import box from "../styles/box.module.css";

function Pager({ page, onChange }: { page: Page<RunSummary>; onChange: (offset: number) => void }) {
  const from = page.total === 0 ? 0 : page.offset + 1;
  const to = Math.min(page.offset + page.limit, page.total);
  return (
    <div className={box.footer}>
      <span className="muted">
        {from}-{to} of {page.total}
      </span>
      <ButtonGroup>
        <Button
          size="small"
          disabled={page.offset === 0}
          onClick={() => {
            onChange(Math.max(0, page.offset - RUNS_PAGE_SIZE));
          }}
        >
          Newer
        </Button>
        <Button
          size="small"
          disabled={page.offset + page.limit >= page.total}
          onClick={() => {
            onChange(page.offset + RUNS_PAGE_SIZE);
          }}
        >
          Older
        </Button>
      </ButtonGroup>
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
  const can = useCan();
  const runs = useEnvironmentRuns(slug, env, offset);
  const environment = project.data?.environments.find((item) => item.name === env);
  const dialogOpen = params.get("run") === "new" && environment !== undefined;

  const closeDialog = useCallback(() => {
    setParams({}, { replace: true });
  }, [setParams]);

  if (!isEnvironment(rawEnv)) {
    return (
      <EmptyState
        icon={<HistoryIcon size={32} aria-hidden="true" />}
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

  return (
    <div className={box.stack}>
      <EnvToolbar
        title={`${capitalize(env)} runs`}
        env={env}
        pathFor={(next) => runsPath(slug, next)}
        subtitle={
          environment ? (
            <>
              Branch <code>{environment.branch}</code>
              {environment.auto_deploy ? ", deploys automatically on push" : ", manual deploys"}
            </>
          ) : (
            "Runs of this environment"
          )
        }
        actions={
          can("run.create", { project: slug, env }) ? (
            <Button
              variant="primary"
              leadingVisual={PlayIcon}
              disabled={!environment}
              onClick={() => {
                setParams({ run: "new" });
              }}
            >
              Run pipeline
            </Button>
          ) : null
        }
      />
      {runs.isPending ? <Loading label="Loading runs" /> : null}
      {runs.isError ? (
        <ErrorCallout
          title="Could not load runs"
          error={runs.error}
          onRetry={() => void runs.refetch()}
        />
      ) : null}
      {runs.data?.total === 0 ? (
        <EmptyState icon={<HistoryIcon size={32} aria-hidden="true" />} title="No runs yet">
          A run builds and deploys one commit.{" "}
          {environment?.auto_deploy
            ? `Push to ${environment.branch} on GitHub to start the first one${can("run.create", { project: slug, env }) ? ", or press Run pipeline" : ""}.`
            : can("run.create", { project: slug, env })
              ? "Press Run pipeline to start the first one."
              : "Nothing has run here yet."}
        </EmptyState>
      ) : null}
      {runs.data && runs.data.total > 0 ? (
        <div className={box.box}>
          <div className={box.header}>
            <span>{runs.data.total} runs</span>
          </div>
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
    </div>
  );
}

export function AllRunsPage() {
  const [offset, setOffset] = useState(0);
  const runs = useAllRuns(offset);
  return (
    <>
      <PageHead title="Runs" description="The latest runs across all projects and environments." />
      <div className={box.section}>
        {runs.isPending ? <Loading label="Loading runs" /> : null}
        {runs.isError ? (
          <ErrorCallout
            title="Could not load runs"
            error={runs.error}
            onRetry={() => void runs.refetch()}
          />
        ) : null}
        {runs.data?.total === 0 ? (
          <EmptyState
            icon={<HistoryIcon size={32} aria-hidden="true" />}
            title="No runs yet"
            action={
              <Button as={Link} to="/projects">
                Go to projects
              </Button>
            }
          >
            Runs appear here after a push to a project branch or a manual run. Open a project to
            start one.
          </EmptyState>
        ) : null}
        {runs.data && runs.data.total > 0 ? (
          <div className={box.box}>
            <div className={box.header}>
              <span>{runs.data.total} runs</span>
            </div>
            <RunList runs={runs.data.items} showProject />
            <Pager page={runs.data} onChange={setOffset} />
          </div>
        ) : null}
      </div>
    </>
  );
}
