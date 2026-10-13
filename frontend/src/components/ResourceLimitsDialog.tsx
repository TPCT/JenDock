import { Button, Checkbox, Dialog, FormControl, Select, TextInput } from "@primer/react";
import { useState } from "react";
import { useRemoveResources, useSaveResources } from "../api/resourceHooks";
import type { HostCapacity, ResourceChange, ResourceLimits } from "../api/resourceTypes";
import type { EnvironmentName } from "../api/types";
import {
  PRESETS,
  formFromLimits,
  parseForm,
  type LimitsForm,
  type MemoryUnit,
} from "../lib/resources";
import { formatBytes } from "../lib/format";
import box from "../styles/box.module.css";
import { Callout, errorMessage } from "./Feedback";
import styles from "./Resources.module.css";

function SizeField({
  id,
  label,
  caption,
  value,
  unit,
  invalid,
  onValue,
  onUnit,
}: {
  id: string;
  label: string;
  caption: string;
  value: string;
  unit: MemoryUnit;
  invalid: boolean;
  onValue: (value: string) => void;
  onUnit: (unit: MemoryUnit) => void;
}) {
  return (
    <FormControl id={id} {...(invalid ? { validationStatus: "error" as const } : {})}>
      <FormControl.Label>{label}</FormControl.Label>
      <div className={styles.sizeRow}>
        <TextInput
          id={id}
          inputMode="decimal"
          value={value}
          placeholder="No limit"
          onChange={(event) => {
            onValue(event.target.value);
          }}
          block
        />
        <Select
          id={`${id}-unit`}
          aria-label={`${label} unit`}
          value={unit}
          onChange={(event) => {
            onUnit(event.target.value as MemoryUnit);
          }}
        >
          <Select.Option value="MiB">MiB</Select.Option>
          <Select.Option value="GiB">GiB</Select.Option>
        </Select>
      </div>
      <FormControl.Caption>{caption}</FormControl.Caption>
    </FormControl>
  );
}

/**
 * Edit the CPU and memory limits of one compose service. Saving keeps them for every future
 * deploy; "Apply now" also updates the running containers without a restart.
 */
export function ResourceLimitsDialog({
  slug,
  env,
  service,
  host,
  current,
  hasRunning,
  onClose,
  onDone,
}: {
  slug: string;
  env: EnvironmentName;
  service: string;
  host: HostCapacity;
  current: ResourceLimits | null;
  hasRunning: boolean;
  onClose: () => void;
  onDone: (change: ResourceChange, removed: boolean) => void;
}) {
  const save = useSaveResources(slug, env, service);
  const remove = useRemoveResources(slug, env, service);
  const [form, setForm] = useState<LimitsForm>(() => formFromLimits(current));
  const [applyNow, setApplyNow] = useState(hasRunning);
  const [problem, setProblem] = useState<{ field: keyof LimitsForm; message: string } | null>(null);
  const [advanced, setAdvanced] = useState(
    current?.memory_reservation_bytes != null || current?.cpu_shares != null,
  );
  const busy = save.isPending || remove.isPending;
  const set = (patch: Partial<LimitsForm>) => {
    setForm((previous) => ({ ...previous, ...patch }));
    setProblem(null);
  };

  const submit = () => {
    const parsed = parseForm(form, host);
    if (!parsed.ok) {
      setProblem({ field: parsed.field, message: parsed.message });
      return;
    }
    save.mutate(
      { ...parsed.limits, apply_now: applyNow },
      {
        onSuccess: (change) => {
          onDone(change, false);
        },
      },
    );
  };
  const removeLimit = () => {
    remove.mutate(applyNow, {
      onSuccess: (change) => {
        onDone(change, true);
      },
    });
  };
  const invalid = (field: keyof LimitsForm) => problem?.field === field;

  return (
    <Dialog
      title={`Edit limits for ${service}`}
      subtitle={`This host has ${String(host.cpus)} CPUs and ${formatBytes(host.memory_bytes)} of memory.`}
      width="large"
      onClose={onClose}
      footerButtons={[
        ...(current
          ? [
              {
                buttonType: "danger" as const,
                content: remove.isPending ? "Removing…" : "Remove limit",
                disabled: busy,
                onClick: removeLimit,
              },
            ]
          : []),
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: save.isPending ? "Saving…" : "Save limits",
          disabled: busy,
          onClick: submit,
        },
      ]}
    >
      <form
        className={box.stack}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div className={styles.presets} role="group" aria-label="Presets">
          {PRESETS.map((preset) => (
            <Button
              key={preset.name}
              size="small"
              onClick={() => {
                set({
                  cpus: String(preset.cpus),
                  memory: String(preset.memory),
                  memoryUnit: preset.unit,
                });
              }}
            >
              {preset.name}: {String(preset.cpus)} CPU, {String(preset.memory)} {preset.unit}
            </Button>
          ))}
        </div>
        <FormControl
          id="limit-cpus"
          {...(invalid("cpus") ? { validationStatus: "error" as const } : {})}
        >
          <FormControl.Label>CPU cores</FormControl.Label>
          <TextInput
            type="number"
            inputMode="decimal"
            min={0.01}
            max={host.cpus}
            step={0.1}
            value={form.cpus}
            placeholder="No limit"
            onChange={(event) => {
              set({ cpus: event.target.value });
            }}
            block
          />
          <FormControl.Caption>
            0.5 is half a core, 2 is two full cores. At most {String(host.cpus)}.
          </FormControl.Caption>
        </FormControl>
        <SizeField
          id="limit-memory"
          label="Memory"
          caption="A hard limit: a container that goes over it is stopped by the system."
          value={form.memory}
          unit={form.memoryUnit}
          invalid={invalid("memory")}
          onValue={(memory) => {
            set({ memory });
          }}
          onUnit={(memoryUnit) => {
            set({ memoryUnit });
          }}
        />
        <div>
          <Button
            variant="invisible"
            size="small"
            aria-expanded={advanced}
            onClick={() => {
              setAdvanced(!advanced);
            }}
          >
            {advanced ? "Hide advanced limits" : "Show advanced limits"}
          </Button>
        </div>
        {advanced ? (
          <>
            <SizeField
              id="limit-reservation"
              label="Memory reservation"
              caption="A soft limit the system tries to keep free for the container when memory is scarce."
              value={form.reservation}
              unit={form.reservationUnit}
              invalid={invalid("reservation")}
              onValue={(reservation) => {
                set({ reservation });
              }}
              onUnit={(reservationUnit) => {
                set({ reservationUnit });
              }}
            />
            <FormControl
              id="limit-shares"
              {...(invalid("shares") ? { validationStatus: "error" as const } : {})}
            >
              <FormControl.Label>CPU shares</FormControl.Label>
              <TextInput
                inputMode="numeric"
                value={form.shares}
                placeholder="1024 (default)"
                onChange={(event) => {
                  set({ shares: event.target.value });
                }}
                block
              />
              <FormControl.Caption>
                A relative weight that only matters when the host is busy: 2048 gets twice the CPU
                of 1024.
              </FormControl.Caption>
            </FormControl>
          </>
        ) : null}
        <FormControl>
          <Checkbox
            checked={applyNow}
            onChange={(event) => {
              setApplyNow(event.target.checked);
            }}
          />
          <FormControl.Label>Apply now to the running containers</FormControl.Label>
          <FormControl.Caption>
            Limits are always saved and kept for every future deploy. With this on, the running
            containers of {service} are updated immediately, without a restart.
            {hasRunning ? "" : " Nothing is running for this service right now."}
          </FormControl.Caption>
        </FormControl>
        {problem ? (
          <Callout variant="danger" role="alert">
            {problem.message}
          </Callout>
        ) : null}
        {save.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(save.error)}
          </Callout>
        ) : null}
        {remove.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(remove.error)}
          </Callout>
        ) : null}
      </form>
    </Dialog>
  );
}
