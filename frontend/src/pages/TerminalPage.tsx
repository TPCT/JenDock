import { PackageIcon } from "@primer/octicons-react";
import { Button, Heading } from "@primer/react";
import { Link, useParams } from "react-router-dom";
import { useContainer } from "../api/containerHooks";
import { isApiError } from "../api/client";
import { ENVIRONMENTS } from "../api/types";
import { StateLabel } from "../components/ContainerBits";
import { Callout, EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { useDocumentTitle } from "../components/PageHead";
import { TerminalView } from "../components/TerminalView";
import box from "../styles/box.module.css";
import { containersPath } from "./Containers";

export default function TerminalPage() {
  const { slug = "", env: rawEnv, id = "" } = useParams();
  const env = ENVIRONMENTS.find((name) => name === rawEnv) ?? "development";
  const container = useContainer(slug, env, id);
  useDocumentTitle(container.data ? `Terminal · ${container.data.name}` : "Terminal");
  const back = `${containersPath(slug, env)}/${id}`;

  if (container.isPending) return <Loading label="Loading container" />;
  if (container.isError) {
    if (isApiError(container.error) && container.error.status === 404) {
      return (
        <EmptyState
          icon={<PackageIcon size={32} aria-hidden="true" />}
          title="Container not found"
          action={
            <Button as={Link} to={containersPath(slug, env)}>
              Back to containers
            </Button>
          }
        >
          This container does not exist any more.
        </EmptyState>
      );
    }
    return <ErrorCallout title="Could not load this container" error={container.error} />;
  }
  const data = container.data;
  return (
    <div className={box.stack}>
      <div>
        <Link to={back}>← {data.name}</Link>
      </div>
      <div className={box.cluster}>
        <Heading as="h2" variant="medium">
          Terminal: {data.name}
        </Heading>
        <StateLabel state={data.state} />
      </div>
      {data.state === "running" ? (
        <TerminalView slug={slug} env={env} containerId={data.id} />
      ) : (
        <Callout variant="warning">
          The container is {data.state}. A terminal needs a running container; start it first.
        </Callout>
      )}
    </div>
  );
}
