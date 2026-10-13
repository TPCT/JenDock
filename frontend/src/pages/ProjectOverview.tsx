import { Label } from "@primer/react";
import { Link, useParams } from "react-router-dom";
import { useDomains } from "../api/adminHooks";
import { useContainers } from "../api/containerHooks";
import { useDeployments } from "../api/deploymentHooks";
import { useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName, type Project } from "../api/types";
import { containersPath } from "./Containers";
import { DeploymentStatusLabel, deploymentsPath } from "./Deployments";
import { EnvironmentCard, domainsPath } from "../components/EnvironmentCard";
import { HostCard } from "../components/HostCard";
import { RelativeTime } from "../components/Time";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";
import styles from "./ProjectOverview.module.css";

const UNAVAILABLE = <span className="muted">Not available to you</span>;
const NONE = <span className="muted">none</span>;

/** What is deployed, running and reachable in one environment, beside its latest run. */
function EnvironmentHealth({ slug, env }: { slug: string; env: EnvironmentName }) {
  const deployments = useDeployments(slug, env, "per_page=1");
  const containers = useContainers(slug, env, "per_page=100");
  const domains = useDomains(slug, env);

  const lastDeploy = deployments.data?.items[0];
  const running = containers.data?.items.filter((item) => item.state === "running").length ?? 0;
  const unhealthy =
    containers.data?.items.filter(
      (item) => item.health === "unhealthy" || item.state === "restarting" || item.state === "dead",
    ).length ?? 0;

  return (
    <dl className={`${box.defs} ${styles.defs}`} aria-label={`${capitalize(env)} health`}>
      <dt>Last deploy</dt>
      <dd>
        {deployments.isError ? (
          UNAVAILABLE
        ) : lastDeploy ? (
          <span className={box.cluster}>
            <DeploymentStatusLabel status={lastDeploy.status} />
            <span className="muted">
              <RelativeTime iso={lastDeploy.created_at} />
            </span>
            <Link to={deploymentsPath(slug, env)}>History</Link>
          </span>
        ) : deployments.data ? (
          <span className="muted">Nothing deployed yet</span>
        ) : null}
      </dd>
      <dt>Containers</dt>
      <dd>
        {containers.isError ? (
          UNAVAILABLE
        ) : containers.data ? (
          containers.data.total === 0 ? (
            <span className="muted">None running</span>
          ) : (
            <span className={box.cluster}>
              <span>
                {running} of {containers.data.total} running
              </span>
              {unhealthy > 0 ? (
                <Label variant="danger">
                  {unhealthy} {unhealthy === 1 ? "needs" : "need"} attention
                </Label>
              ) : (
                <Label variant="success">Healthy</Label>
              )}
              <Link to={containersPath(slug, env)}>Open</Link>
            </span>
          )
        ) : null}
      </dd>
      <dt>Domains</dt>
      <dd>
        {domains.isError ? (
          UNAVAILABLE
        ) : domains.data ? (
          domains.data.length === 0 ? (
            <span className={box.cluster}>
              {NONE}
              <Link to={`${domainsPath(slug, env)}?add=1`}>Add a domain</Link>
            </span>
          ) : (
            <ul className={styles.domains} aria-label={`${capitalize(env)} domains`}>
              {domains.data.map((domain) => (
                <li key={domain.id} className={box.cluster}>
                  <code>{domain.hostname}</code>
                  <Label
                    variant={
                      domain.status === "active"
                        ? "success"
                        : domain.status === "error"
                          ? "danger"
                          : "secondary"
                    }
                  >
                    {domain.status === "active"
                      ? "Active"
                      : domain.status === "error"
                        ? "Failed"
                        : "Draft"}
                  </Label>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </dd>
    </dl>
  );
}

function EnvironmentPanel({ project, env }: { project: Project; env: EnvironmentName }) {
  const environment = project.environments.find((item) => item.name === env);
  if (!environment) return null;
  return (
    <div className={box.box}>
      <div className={box.row}>
        <EnvironmentCard project={project} environment={environment} detailed />
      </div>
      <div className={box.row}>
        <EnvironmentHealth slug={project.slug} env={env} />
      </div>
    </div>
  );
}

/** Overview tab: both environments with their latest run, last deploy, containers and domains. */
export function ProjectOverviewPage() {
  const { slug = "" } = useParams();
  const project = useProject(slug);
  const data = project.data;
  if (!data) return null;
  return (
    <div className={box.stack}>
      <div className={styles.envs}>
        {ENVIRONMENTS.map((name) => (
          <EnvironmentPanel key={name} project={data} env={name} />
        ))}
      </div>
      <HostCard />
    </div>
  );
}
