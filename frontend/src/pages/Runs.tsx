import { HistoryIcon, PlayIcon } from "@primer/octicons-react";
import { Button } from "@primer/react";
import { useCallback } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useAllRuns, useEnvironmentRuns, useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { EnvToolbar } from "../components/EnvSwitch";
import { runsPath } from "../components/EnvironmentCard";
import { EmptyState } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import { PagedList } from "../components/PagedList";
import { RunList } from "../components/RunList";
import { StartRunDialog } from "../components/StartRunDialog";
import { STATUS_LABEL, capitalize } from "../lib/format";
import { useListParams, type FilterDef, type ListSpec, type SortOption } from "../lib/listParams";
import { useCan } from "../lib/permissions";
import box from "../styles/box.module.css";

const STATUS_FILTER: FilterDef = {
  param: "status",
  label: "Status",
  allLabel: "All statuses",
  options: Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label })),
};
const TRIGGER_FILTER: FilterDef = {
  param: "trigger",
  label: "Started by",
  allLabel: "Anything",
  options: [
    { value: "push", label: "A push" },
    { value: "manual", label: "A manual run" },
    { value: "redeploy", label: "A redeploy" },
  ],
};
const ENVIRONMENT_FILTER: FilterDef = {
  param: "environment",
  label: "Environment",
  allLabel: "Both environments",
  options: ENVIRONMENTS.map((value) => ({ value, label: capitalize(value) })),
};
const RUN_SORTS: readonly SortOption[] = [
  { value: "newest", label: "Newest", sort: "created", order: "desc" },
  { value: "oldest", label: "Oldest", sort: "created", order: "asc" },
  { value: "finished", label: "Recently finished", sort: "finished", order: "desc" },
];
const ENVIRONMENT_RUNS: ListSpec = { filters: [STATUS_FILTER, TRIGGER_FILTER], sorts: RUN_SORTS };
const ALL_RUNS: ListSpec = {
  filters: [STATUS_FILTER, TRIGGER_FILTER, ENVIRONMENT_FILTER],
  sorts: RUN_SORTS,
};

function isEnvironment(value: string | undefined): value is EnvironmentName {
  return ENVIRONMENTS.some((name) => name === value);
}

export function EnvironmentRunsPage() {
  const { slug = "", env: rawEnv } = useParams();
  const env: EnvironmentName = isEnvironment(rawEnv) ? rawEnv : "development";
  const [params, setParams] = useSearchParams();
  const list = useListParams(ENVIRONMENT_RUNS);
  const project = useProject(slug);
  const can = useCan();
  const runs = useEnvironmentRuns(slug, env, list.apiQuery);
  const environment = project.data?.environments.find((item) => item.name === env);
  const dialogOpen = params.get("run") === "new" && environment !== undefined;

  const closeDialog = useCallback(() => {
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        next.delete("run");
        return next;
      },
      { replace: true },
    );
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

  const mayRun = can("run.create", { project: slug, env });
  const startRun = (
    <Button
      variant="primary"
      leadingVisual={PlayIcon}
      disabled={!environment}
      onClick={() => {
        setParams(
          (previous) => {
            const next = new URLSearchParams(previous);
            next.set("run", "new");
            return next;
          },
          { replace: false },
        );
      }}
    >
      Run pipeline
    </Button>
  );

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
        actions={mayRun ? startRun : null}
      />
      <PagedList
        noun="runs"
        query={runs}
        list={list}
        filters={ENVIRONMENT_RUNS.filters ?? []}
        sorts={RUN_SORTS}
        placeholder="Search by branch, commit, message or run number…"
        empty={
          <EmptyState
            icon={<HistoryIcon size={32} aria-hidden="true" />}
            title="No runs yet"
            action={mayRun ? startRun : undefined}
          >
            A run builds and deploys one commit.{" "}
            {environment?.auto_deploy
              ? `Push to ${environment.branch} on GitHub to start the first one${mayRun ? ", or press Run pipeline" : ""}.`
              : mayRun
                ? "Press Run pipeline to start the first one."
                : "Nothing has run here yet."}
          </EmptyState>
        }
      >
        {(items) => <RunList runs={items} />}
      </PagedList>
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
  const list = useListParams(ALL_RUNS);
  const runs = useAllRuns(list.apiQuery);
  return (
    <>
      <PageHead
        title="Runs"
        description="Every build and deploy across all projects and environments, newest first. Search by project, branch, commit or run number."
      />
      <div className={box.section}>
        <PagedList
          noun="runs"
          query={runs}
          list={list}
          filters={ALL_RUNS.filters ?? []}
          sorts={RUN_SORTS}
          placeholder="Search by project, branch, commit, message or run number…"
          empty={
            <EmptyState
              icon={<HistoryIcon size={32} aria-hidden="true" />}
              title="No runs yet"
              action={
                <Button as={Link} to="/projects" variant="primary">
                  Go to projects
                </Button>
              }
            >
              Runs appear here after a push to a project branch or a manual run. Open a project to
              start one.
            </EmptyState>
          }
        >
          {(items) => <RunList runs={items} showProject />}
        </PagedList>
      </div>
    </>
  );
}
