import { PackageIcon } from "@primer/octicons-react";
import { Button } from "@primer/react";
import { Link, useParams } from "react-router-dom";
import { useContainers } from "../api/containerHooks";
import { ENVIRONMENTS } from "../api/types";
import {
  HealthLabel,
  StateLabel,
  Uptime,
  portText,
  shortImageId,
} from "../components/ContainerBits";
import { runsPath } from "../components/EnvironmentCard";
import { EnvToolbar } from "../components/EnvSwitch";
import { EmptyState } from "../components/Feedback";
import { HostCard } from "../components/HostCard";
import { PagedList } from "../components/PagedList";
import { ResourcesSection } from "../components/Resources";
import { capitalize, shortSha } from "../lib/format";
import { useListParams, type FilterDef, type ListSpec, type SortOption } from "../lib/listParams";
import box from "../styles/box.module.css";
import styles from "./Containers.module.css";

export function containersPath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/containers`;
}

const STATE_FILTER: FilterDef = {
  param: "state",
  label: "State",
  allLabel: "All states",
  options: ["running", "restarting", "paused", "exited", "created", "dead"].map((value) => ({
    value,
    label: capitalize(value),
  })),
};
const CONTAINER_SORTS: readonly SortOption[] = [
  { value: "service", label: "Service", sort: "service" },
  { value: "name", label: "Container name", sort: "name" },
  { value: "started", label: "Recently started", sort: "started", order: "desc" },
];
const CONTAINER_LIST: ListSpec = { filters: [STATE_FILTER], sorts: CONTAINER_SORTS };

/** Inventory of the environment's compose project: one row per container. */
export function ContainersPage() {
  const { slug = "", env: rawEnv } = useParams();
  const env = ENVIRONMENTS.find((name) => name === rawEnv) ?? null;
  const list = useListParams(CONTAINER_LIST);
  const containers = useContainers(slug, env ?? "development", list.apiQuery);

  if (env === null) {
    return (
      <EmptyState
        icon={<PackageIcon size={32} aria-hidden="true" />}
        title="Unknown environment"
        action={
          <Button as={Link} to={containersPath(slug, "development")}>
            Go to Development containers
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
        title={`${capitalize(env)} containers`}
        subtitle="Everything running for this environment. Open a container to see logs, change its settings or open a terminal."
        env={env}
        pathFor={(next) => containersPath(slug, next)}
      />
      <PagedList
        noun="containers"
        query={containers}
        list={list}
        filters={[STATE_FILTER]}
        sorts={CONTAINER_SORTS}
        placeholder="Search by service, container name, image or commit…"
        empty={
          <EmptyState
            icon={<PackageIcon size={32} aria-hidden="true" />}
            title="No containers yet"
            action={
              <Button as={Link} to={`${runsPath(slug, env)}?run=new`} variant="primary">
                Run the pipeline
              </Button>
            }
          >
            Nothing is running for {capitalize(env)}. Containers appear here after a run deploys
            this environment.
          </EmptyState>
        }
      >
        {(items) => (
          <div className={styles.scroll}>
            <table className={styles.table} aria-label={`${capitalize(env)} containers`}>
              <thead>
                <tr>
                  <th scope="col">Service</th>
                  <th scope="col">Image</th>
                  <th scope="col">Commit</th>
                  <th scope="col">State</th>
                  <th scope="col">Restarts</th>
                  <th scope="col">Uptime</th>
                  <th scope="col">Ports</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <th scope="row">
                      <Link to={`${containersPath(slug, env)}/${item.id}`}>{item.service}</Link>
                      <div className="muted">{item.name}</div>
                    </th>
                    <td>
                      <code>{item.image}</code>
                      <div className="muted">
                        <code>{shortImageId(item.image_id)}</code>
                      </div>
                    </td>
                    <td>
                      {item.commit ? (
                        <code>{shortSha(item.commit)}</code>
                      ) : (
                        <span className="muted">-</span>
                      )}
                    </td>
                    <td>
                      <div className={box.cluster}>
                        <StateLabel state={item.state} />
                        <HealthLabel health={item.health} />
                      </div>
                    </td>
                    <td>{item.restart_count}</td>
                    <td>
                      <Uptime startedAt={item.started_at} state={item.state} />
                    </td>
                    <td>
                      {item.ports.length === 0 ? (
                        <span className="muted">none</span>
                      ) : (
                        item.ports.map((port) => (
                          <div key={portText(port)}>
                            <code>{portText(port)}</code>
                          </div>
                        ))
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PagedList>
      <ResourcesSection slug={slug} env={env} />
      <HostCard />
    </div>
  );
}
