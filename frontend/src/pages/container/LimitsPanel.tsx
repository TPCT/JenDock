import { Button, FormControl, TextInput } from "@primer/react";
import { useState } from "react";
import { useUpdateLimits } from "../../api/containerHooks";
import type { ContainerView } from "../../api/containerTypes";
import type { EnvironmentName } from "../../api/types";
import { Callout, errorMessage } from "../../components/Feedback";
import { formatBytes } from "../../lib/format";
import box from "../../styles/box.module.css";

const MEMORY = /^\d+(?:\.\d+)?[kmg]$/i;

/** CPU and memory limits, applied live. The server enforces its own maxima and says so in its answer. */
export function LimitsPanel({
  slug,
  env,
  container,
}: {
  slug: string;
  env: EnvironmentName;
  container: ContainerView;
}) {
  const update = useUpdateLimits(slug, env, container.id);
  const [cpus, setCpus] = useState(container.cpus === null ? "" : String(container.cpus));
  const [memory, setMemory] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  const submit = () => {
    const body: { cpus?: number; memory?: string } = {};
    if (cpus.trim() !== "") {
      const value = Number(cpus);
      if (!Number.isFinite(value) || value <= 0) {
        setProblem("CPUs must be a number above 0, for example 1.5.");
        return;
      }
      if (value !== container.cpus) body.cpus = value;
    }
    if (memory.trim() !== "") {
      if (!MEMORY.test(memory.trim())) {
        setProblem("Memory looks like 512m or 2g.");
        return;
      }
      body.memory = memory.trim();
    }
    if (body.cpus === undefined && body.memory === undefined) {
      setProblem("Change at least one of the two values.");
      return;
    }
    setProblem(null);
    update.mutate(body, {
      onSuccess: () => {
        setMemory("");
      },
    });
  };

  return (
    <form
      className={box.stack}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <dl className={box.defs}>
        <dt>Current CPU limit</dt>
        <dd>{container.cpus === null ? "none" : `${String(container.cpus)} CPUs`}</dd>
        <dt>Current memory limit</dt>
        <dd>{container.memory_bytes === null ? "none" : formatBytes(container.memory_bytes)}</dd>
      </dl>
      <FormControl>
        <FormControl.Label>CPUs</FormControl.Label>
        <TextInput
          block
          inputMode="decimal"
          value={cpus}
          onChange={(event) => {
            setCpus(event.target.value);
          }}
        />
        <FormControl.Caption>
          How many CPU cores the container may use at most, for example 0.5 or 2. The server has a
          maximum and refuses higher values.
        </FormControl.Caption>
      </FormControl>
      <FormControl>
        <FormControl.Label>Memory</FormControl.Label>
        <TextInput
          block
          monospace
          value={memory}
          placeholder="512m"
          onChange={(event) => {
            setMemory(event.target.value);
          }}
        />
        <FormControl.Caption>
          Use m for megabytes or g for gigabytes, for example 512m or 2g. A container that goes over
          is stopped by the system. Leave empty to keep the current limit.
        </FormControl.Caption>
      </FormControl>
      <p className="muted">
        The change applies immediately, without a restart, and is kept for future deployments.
      </p>
      {problem ? (
        <Callout variant="danger" role="alert">
          {problem}
        </Callout>
      ) : null}
      {update.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(update.error)}
        </Callout>
      ) : null}
      {update.isSuccess ? (
        <Callout variant="success" role="status">
          Limits updated.
        </Callout>
      ) : null}
      <div>
        <Button type="submit" variant="primary" disabled={update.isPending}>
          {update.isPending ? "Saving…" : "Save limits"}
        </Button>
      </div>
    </form>
  );
}
