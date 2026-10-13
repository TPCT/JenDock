import {
  AlertIcon,
  CheckCircleFillIcon,
  DotFillIcon,
  HistoryIcon,
  RocketIcon,
  SyncIcon,
  UndoIcon,
  XCircleFillIcon,
} from "@primer/octicons-react";
import { Button, ConfirmationDialog, Label } from "@primer/react";
import { useState, type ComponentProps, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useDeployments, useRedeploy } from "../api/deploymentHooks";
import type { Deployment, DeploymentStatus } from "../api/deploymentTypes";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { CopySha } from "../components/CopySha";
import { runsPath } from "../components/EnvironmentCard";
import { EnvToolbar } from "../components/EnvSwitch";
import { Callout, EmptyState, errorMessage } from "../components/Feedback";
import { PagedList } from "../components/PagedList";
import { Duration, RelativeTime } from "../components/Time";
import { capitalize } from "../lib/format";
import { useListParams, type FilterDef, type ListSpec, type SortOption } from "../lib/listParams";
import { useCan } from "../lib/permissions";
import box from "../styles/box.module.css";
import styles from "./Deployments.module.css";

export function deploymentsPath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/deployments`;
}

type LabelVariant = NonNullable<ComponentProps<typeof Label>["variant"]>;

const STATUS: Record<
  DeploymentStatus,
  { text: string; variant: LabelVariant; icon: ReactNode; hint: string }
> = {
  succeeded: {
    text: "Succeeded",
    variant: "success",
    icon: <CheckCircleFillIcon size={12} />,
    hint: "The new version is running.",
  },
  deploying: {
    text: "Deploying",
    variant: "attention",
    icon: <SyncIcon size={12} className="spin" />,
    hint: "The new containers are starting.",
  },
  pending: {
    text: "Queued",
    variant: "attention",
    icon: <DotFillIcon size={12} />,
    hint: "A redeploy waiting for its run to start.",
  },
  failed: {
    text: "Failed",
    variant: "danger",
    icon: <XCircleFillIcon size={12} />,
    hint: "Nothing was changed; the previous version still runs.",
  },
  rolled_back: {
    text: "Rolled back",
    variant: "severe",
    icon: <UndoIcon size={12} />,
    hint: "The new version failed its health check, so the previous one runs again.",
  },
  rollback_failed: {
    text: "Rollback failed",
    variant: "danger",
    icon: <AlertIcon size={12} />,
    hint: "The new version failed and so did the rollback. Check the containers.",
  },
};

export function DeploymentStatusLabel({ status }: { status: DeploymentStatus }) {
  const { text, variant, icon, hint } = STATUS[status];
  return (
    <Label variant={variant} className={styles.badge} title={hint}>
      {icon}
      {text}
    </Label>
  );
}

const STATUS_FILTER: FilterDef = {
  param: "status",
  label: "Status",
  allLabel: "All statuses",
  options: (Object.keys(STATUS) as DeploymentStatus[]).map((value) => ({
    value,
    label: STATUS[value].text,
  })),
};
const SORTS: readonly SortOption[] = [
  { value: "newest", label: "Newest", sort: "created", order: "desc" },
  { value: "oldest", label: "Oldest", sort: "created", order: "asc" },
  { value: "finished", label: "Recently finished", sort: "finished", order: "desc" },
];
const DEPLOYMENT_LIST: ListSpec = { filters: [STATUS_FILTER], sorts: SORTS };

function Row({
  deployment,
  onRedeploy,
}: {
  deployment: Deployment;
  onRedeploy: ((deployment: Deployment) => void) | null;
}) {
  return (
    <li className={`${box.row} ${styles.row}`}>
      <div className={styles.main}>
        <div className={box.cluster}>
          <DeploymentStatusLabel status={deployment.status} />
          <strong>Deployment #{deployment.id}</strong>
          <Link to={`/runs/${String(deployment.run_id)}`}>Run #{deployment.run_number}</Link>
          <CopySha sha={deployment.commit_sha} />
          {deployment.redeploy_of !== null ? (
            <Label variant="secondary">Redeploy of #{deployment.redeploy_of}</Label>
          ) : null}
        </div>
        <ul className={styles.services} aria-label={`Services of deployment ${deployment.id}`}>
          {deployment.services.map((service) => (
            <li key={service.service}>
              <strong>{service.service}</strong>: <code>{service.new_image}</code>
              {service.previous_image !== null ? (
                <span className="muted">
                  {" "}
                  (was <code>{service.previous_image}</code>)
                </span>
              ) : null}
            </li>
          ))}
        </ul>
        {deployment.message ? (
          <p className={`muted ${styles.message}`} role="note">
            {deployment.message}
          </p>
        ) : null}
      </div>
      <div className={styles.side}>
        <span className="muted">
          <RelativeTime iso={deployment.created_at} />
        </span>
        <span className="muted">
          <Duration startedAt={deployment.started_at} finishedAt={deployment.finished_at} />
        </span>
        {onRedeploy !== null && deployment.status === "succeeded" ? (
          <Button
            size="small"
            leadingVisual={HistoryIcon}
            aria-label={`Redeploy deployment ${deployment.id}`}
            onClick={() => {
              onRedeploy(deployment);
            }}
          >
            Redeploy
          </Button>
        ) : null}
      </div>
    </li>
  );
}

/** The deployment history of one environment, with a confirmed redeploy of earlier versions. */
export function DeploymentsPage() {
  const { slug = "", env: rawEnv } = useParams();
  const env: EnvironmentName | null = ENVIRONMENTS.find((name) => name === rawEnv) ?? null;
  const list = useListParams(DEPLOYMENT_LIST);
  const deployments = useDeployments(slug, env ?? "development", list.apiQuery);
  const redeploy = useRedeploy(slug, env ?? "development");
  const can = useCan();
  const navigate = useNavigate();
  const [target, setTarget] = useState<Deployment | null>(null);

  if (env === null) {
    return (
      <EmptyState
        icon={<RocketIcon size={32} aria-hidden="true" />}
        title="Unknown environment"
        action={
          <Button as={Link} to={deploymentsPath(slug, "development")}>
            Go to Development deployments
          </Button>
        }
      >
        Every project has exactly two environments: development and production.
      </EmptyState>
    );
  }

  const mayRedeploy = can("deployment.redeploy", { project: slug, env });
  return (
    <div className={box.stack}>
      <EnvToolbar
        title={`${capitalize(env)} deployments`}
        subtitle="Every rollout of this environment. Redeploy an earlier version to go back to it without rebuilding."
        env={env}
        pathFor={(next) => deploymentsPath(slug, next)}
      />
      <PagedList
        noun="deployments"
        query={deployments}
        list={list}
        filters={[STATUS_FILTER]}
        sorts={SORTS}
        placeholder="Search by commit, message or run number…"
        empty={
          <EmptyState
            icon={<RocketIcon size={32} aria-hidden="true" />}
            title="Nothing deployed yet"
            action={
              <Button as={Link} to={runsPath(slug, env)} variant="primary">
                Go to runs
              </Button>
            }
          >
            A deployment is recorded when a run deploys this environment. Start a run from the Runs
            tab, or push to the tracked branch.
          </EmptyState>
        }
      >
        {(items) => (
          <ul className={box.list} aria-label="Deployments">
            {items.map((item) => (
              <Row
                key={item.id}
                deployment={item}
                onRedeploy={
                  mayRedeploy
                    ? (picked) => {
                        redeploy.reset();
                        setTarget(picked);
                      }
                    : null
                }
              />
            ))}
          </ul>
        )}
      </PagedList>
      {target ? (
        <ConfirmationDialog
          title={`Redeploy deployment #${String(target.id)}?`}
          confirmButtonContent="Redeploy"
          cancelButtonContent="Cancel"
          confirmButtonLoading={redeploy.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              setTarget(null);
              return;
            }
            redeploy.mutate(target.id, {
              onSuccess: (result) => {
                setTarget(null);
                void navigate(`/runs/${String(result.run.id)}`);
              },
            });
          }}
        >
          <p>
            This starts a run that puts these images back on {capitalize(env)}. Nothing is built
            again, and the containers are replaced one environment at a time.
          </p>
          <ul aria-label="Images that will be deployed">
            {target.services.map((service) => (
              <li key={service.service}>
                <strong>{service.service}</strong>: <code>{service.new_image}</code>
              </li>
            ))}
          </ul>
          {redeploy.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(redeploy.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </div>
  );
}
