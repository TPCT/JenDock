import { PackageIcon, PlayIcon, SyncIcon, TerminalIcon, StopIcon } from "@primer/octicons-react";
import { Button, ConfirmationDialog, Heading, LinkButton } from "@primer/react";
import { UnderlinePanels } from "@primer/react/experimental";
import { useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { useConfirmAction, useContainer, useRequestAction } from "../api/containerHooks";
import type { ActionPlan, ContainerView, LifecycleAction } from "../api/containerTypes";
import { isApiError } from "../api/client";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { HealthLabel, StateLabel } from "../components/ContainerBits";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { useDocumentTitle } from "../components/PageHead";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";
import { containersPath } from "./Containers";
import { EnvPanel } from "./container/EnvPanel";
import { FilesPanel } from "./container/FilesPanel";
import { LimitsPanel } from "./container/LimitsPanel";
import { LogsPanel } from "./container/LogsPanel";
import { OverviewPanel } from "./container/OverviewPanel";

const TABS = ["overview", "logs", "environment", "files", "limits"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, string> = {
  overview: "Overview",
  logs: "Logs",
  environment: "Environment",
  files: "Files",
  limits: "Limits",
};

const ACTION_ICON = { start: PlayIcon, stop: StopIcon, restart: SyncIcon } as const;
const DONE: Record<LifecycleAction, string> = {
  start: "Started",
  stop: "Stopped",
  restart: "Restarted",
};

/** Start, stop and restart with the server's two-step confirmation: the dialog lists the effects. */
function ActionControls({
  slug,
  env,
  container,
  onDone,
}: {
  slug: string;
  env: EnvironmentName;
  container: ContainerView;
  onDone: (message: string) => void;
}) {
  const request = useRequestAction(slug, env, container.id);
  const confirm = useConfirmAction(slug, env, container.id);
  const [plan, setPlan] = useState<ActionPlan | null>(null);
  const running = container.state === "running";
  const available: Record<LifecycleAction, boolean> = {
    start: !running,
    stop: running,
    restart: true,
  };
  const close = () => {
    setPlan(null);
    confirm.reset();
  };

  return (
    <>
      <div className={box.cluster}>
        {(["start", "stop", "restart"] as const).map((action) => (
          <Button
            key={action}
            leadingVisual={ACTION_ICON[action]}
            variant={action === "stop" ? "danger" : "default"}
            disabled={!available[action] || request.isPending}
            onClick={() => {
              request.mutate(action, { onSuccess: setPlan });
            }}
          >
            {capitalize(action)}
          </Button>
        ))}
        <LinkButton
          as={Link}
          to="terminal"
          leadingVisual={TerminalIcon}
          aria-disabled={!running}
          inactive={!running}
        >
          Terminal
        </LinkButton>
      </div>
      {request.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(request.error)}
        </Callout>
      ) : null}
      {plan ? (
        <ConfirmationDialog
          title={`${capitalize(plan.action)} ${plan.container.service}?`}
          confirmButtonContent={`${capitalize(plan.action)} container`}
          cancelButtonContent="Cancel"
          confirmButtonType={plan.action === "start" ? "primary" : "danger"}
          confirmButtonLoading={confirm.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              close();
              return;
            }
            confirm.mutate(plan.confirmation, {
              onSuccess: (result) => {
                onDone(
                  `${DONE[result.action]} ${result.container.name}. It was ${result.previous_state}.`,
                );
                close();
              },
            });
          }}
        >
          <div className={box.stack}>
            <p>This is what will happen:</p>
            <ul>
              {plan.effects.map((effect) => (
                <li key={effect}>{effect}</li>
              ))}
            </ul>
            <p className="muted">
              Container <code>{plan.container.name}</code> ({plan.container.id.slice(0, 12)}). This
              confirmation is valid for one minute and for this container only.
            </p>
            {confirm.isError ? (
              <Callout variant="danger" role="alert">
                {isApiError(confirm.error) && confirm.error.status === 422
                  ? "The confirmation expired or no longer matches. Close this window and try again."
                  : errorMessage(confirm.error)}
              </Callout>
            ) : null}
          </div>
        </ConfirmationDialog>
      ) : null}
    </>
  );
}

export function ContainerDetailPage() {
  const { slug = "", env: rawEnv, id = "" } = useParams();
  const env = ENVIRONMENTS.find((name) => name === rawEnv) ?? "development";
  const [params, setParams] = useSearchParams();
  const container = useContainer(slug, env, id);
  const location = useLocation();
  const arrived = (location.state as { notice?: string } | null)?.notice ?? null;
  const [done, setNotice] = useState<string | null>(null);
  const notice = done ?? arrived;
  useDocumentTitle(container.data ? `${container.data.name} · containers` : "Container");
  const wanted = params.get("tab");
  const tab: Tab = TABS.find((item) => item === wanted) ?? "overview";
  const back = containersPath(slug, env);

  if (container.isPending) return <Loading label="Loading container" />;
  if (container.isError) {
    if (isApiError(container.error) && container.error.status === 404) {
      return (
        <EmptyState
          icon={<PackageIcon size={32} aria-hidden="true" />}
          title="Container not found"
          action={
            <Button as={Link} to={back}>
              Back to containers
            </Button>
          }
        >
          This container does not exist any more. Restarts and deployments create new containers, so
          the list may have changed.
        </EmptyState>
      );
    }
    return (
      <ErrorCallout
        title="Could not load this container"
        error={container.error}
        onRetry={() => void container.refetch()}
      />
    );
  }
  const data = container.data;

  return (
    <div className={box.stack}>
      <div>
        <Link to={back}>← {capitalize(env)} containers</Link>
      </div>
      <div className={box.spread}>
        <div className={box.cluster}>
          <Heading as="h2" variant="medium">
            {data.name}
          </Heading>
          <StateLabel state={data.state} />
          <HealthLabel health={data.health} />
        </div>
      </div>
      <ActionControls slug={slug} env={env} container={data} onDone={setNotice} />
      {notice ? (
        <Callout variant="success" role="status">
          {notice}
        </Callout>
      ) : null}
      <UnderlinePanels
        aria-label="Container sections"
        value={tab}
        onChange={({ value }) => {
          setParams(value === "overview" ? {} : { tab: value }, { replace: true });
        }}
      >
        {TABS.map((item) => (
          <UnderlinePanels.Tab key={item} value={item}>
            {TAB_LABEL[item]}
          </UnderlinePanels.Tab>
        ))}
        {TABS.map((item) => (
          <UnderlinePanels.Panel key={item} value={item}>
            <div className={box.section}>
              {tab !== item ? null : item === "overview" ? (
                <OverviewPanel slug={slug} env={env} container={data} />
              ) : item === "logs" ? (
                <LogsPanel slug={slug} env={env} container={data} />
              ) : item === "environment" ? (
                <EnvPanel slug={slug} env={env} container={data} />
              ) : item === "files" ? (
                <FilesPanel slug={slug} env={env} container={data} />
              ) : (
                <LimitsPanel slug={slug} env={env} container={data} />
              )}
            </div>
          </UnderlinePanels.Panel>
        ))}
      </UnderlinePanels>
    </div>
  );
}
