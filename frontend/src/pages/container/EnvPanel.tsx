import { PlusIcon } from "@primer/octicons-react";
import {
  Button,
  ConfirmationDialog,
  Dialog,
  FormControl,
  Label,
  Textarea,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useContainerEnv, useRevealEnv, useUpdateEnv } from "../../api/containerHooks";
import type { ContainerView } from "../../api/containerTypes";
import { isApiError } from "../../api/client";
import type { EnvironmentName } from "../../api/types";
import { CopyField } from "../../components/CopyField";
import { Callout, ErrorCallout, Loading, errorMessage } from "../../components/Feedback";
import { SettingsRow } from "../../components/SettingsSection";
import box from "../../styles/box.module.css";
import { containersPath } from "../Containers";

const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

function RevealFlow({
  slug,
  env,
  container,
  name,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  container: ContainerView;
  name: string;
  onClose: () => void;
}) {
  const reveal = useRevealEnv(slug, env, container.id);
  if (reveal.data) {
    return (
      <Dialog
        title={`${name} (revealed)`}
        width="large"
        onClose={onClose}
        footerButtons={[{ buttonType: "primary", content: "Hide and close", onClick: onClose }]}
      >
        <div className={box.stack}>
          <Callout variant="warning">
            This view was recorded in the audit log. The value is not kept once you close this
            window.
          </Callout>
          <CopyField label={`Value of ${name}`} value={reveal.data.value} multiline />
        </div>
      </Dialog>
    );
  }
  return (
    <ConfirmationDialog
      title={`Reveal ${name}?`}
      confirmButtonContent="Reveal value"
      cancelButtonContent="Cancel"
      confirmButtonLoading={reveal.isPending}
      onClose={(gesture) => {
        if (gesture === "confirm") reveal.mutate(name);
        else onClose();
      }}
    >
      <p>
        Showing an environment variable of {container.service} is recorded in the audit log with
        your name and the time. Reveal it only if you need the value.
      </p>
      {reveal.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(reveal.error)}
        </Callout>
      ) : null}
    </ConfirmationDialog>
  );
}

function ValueDialog({
  fixedName,
  onSave,
  onClose,
}: {
  fixedName: string | null;
  onSave: (name: string, value: string) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState(fixedName ?? "");
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <Dialog
      title={fixedName ? `New value for ${fixedName}` : "Add a variable"}
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: "Stage change",
          onClick: () => {
            if (!NAME.test(name)) {
              setProblem(
                "A name uses letters, digits and underscores and does not start with a digit.",
              );
              return;
            }
            onSave(name, value);
          },
        },
      ]}
    >
      <div className={box.stack}>
        <FormControl required>
          <FormControl.Label>Name</FormControl.Label>
          <TextInput
            block
            monospace
            value={name}
            disabled={fixedName !== null}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </FormControl>
        <FormControl>
          <FormControl.Label>Value</FormControl.Label>
          <Textarea
            block
            rows={3}
            className="mono"
            value={value}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => {
              setValue(event.target.value);
            }}
          />
          <FormControl.Caption>
            Nothing is applied yet: this only stages the change. You review it, by variable name,
            before the service restarts.
          </FormControl.Caption>
        </FormControl>
        {problem ? (
          <Callout variant="danger" role="alert">
            {problem}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

export function EnvPanel({
  slug,
  env,
  container,
}: {
  slug: string;
  env: EnvironmentName;
  container: ContainerView;
}) {
  const navigate = useNavigate();
  const vars = useContainerEnv(slug, env, container.id);
  const update = useUpdateEnv(slug, env, container.id);
  const [staged, setStaged] = useState<Record<string, string>>({});
  const [removals, setRemovals] = useState<string[]>([]);
  const [revealing, setRevealing] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ name: string | null } | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const setNames = Object.keys(staged);
  const pending = setNames.length + removals.length;
  const restored = update.isError && isApiError(update.error) && update.error.status === 409;

  const apply = () => {
    update.mutate(
      { set: staged, remove: removals },
      {
        onSuccess: (result) => {
          setReviewing(false);
          setStaged({});
          setRemovals([]);
          const message = result.restarted
            ? `Applied. The ${result.service} service was recreated with the new values.`
            : `Applied. The ${result.service} service stays stopped and will use the new values when it starts.`;
          const replacement = result.containers[0];
          if (replacement && replacement.id !== container.id) {
            // The container id changed, so this page is replaced: the message travels with the
            // navigation and is shown once, by the new page.
            void navigate(`${containersPath(slug, env)}/${replacement.id}?tab=environment`, {
              replace: true,
              state: { notice: message },
            });
          } else {
            setDone(message);
          }
        },
      },
    );
  };

  return (
    <div className={box.stack}>
      <p className="muted">
        Values are masked. Changes are saved as an override for the {container.service} service only
        and stay in place for later deployments.
      </p>
      {done ? (
        <Callout variant="success" role="status">
          {done}
        </Callout>
      ) : null}
      {vars.isPending ? <Loading label="Loading variables" /> : null}
      {vars.isError ? (
        <ErrorCallout title="Could not load the variables" error={vars.error} />
      ) : null}
      {vars.data ? (
        <div className={box.box}>
          <ul className={box.list} aria-label="Environment variables">
            {vars.data.map((item) => {
              const isStaged = item.name in staged;
              const isRemoved = removals.includes(item.name);
              return (
                <li key={item.name}>
                  <SettingsRow
                    title={item.name}
                    description={
                      <span className={box.cluster}>
                        <code>{item.masked_value}</code>
                        {item.sensitive ? <Label variant="attention">sensitive</Label> : null}
                        {item.overridden ? <Label variant="accent">overridden</Label> : null}
                        {isStaged ? <Label variant="success">new value staged</Label> : null}
                        {isRemoved ? (
                          <Label variant="danger">override will be removed</Label>
                        ) : null}
                      </span>
                    }
                    action={
                      <div className={box.cluster}>
                        <Button
                          size="small"
                          aria-label={`Reveal ${item.name}`}
                          onClick={() => {
                            setRevealing(item.name);
                          }}
                        >
                          Reveal
                        </Button>
                        <Button
                          size="small"
                          aria-label={`Edit ${item.name}`}
                          onClick={() => {
                            setEditing({ name: item.name });
                          }}
                        >
                          Edit
                        </Button>
                        {item.overridden ? (
                          <Button
                            size="small"
                            variant="danger"
                            aria-label={`Remove override of ${item.name}`}
                            onClick={() => {
                              setRemovals((current) =>
                                current.includes(item.name) ? current : [...current, item.name],
                              );
                              setStaged((current) =>
                                Object.fromEntries(
                                  Object.entries(current).filter(([key]) => key !== item.name),
                                ),
                              );
                            }}
                          >
                            Remove override
                          </Button>
                        ) : null}
                      </div>
                    }
                  />
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      <div className={box.cluster}>
        <Button
          leadingVisual={PlusIcon}
          onClick={() => {
            setEditing({ name: null });
          }}
        >
          Add variable
        </Button>
        <Button
          variant="primary"
          disabled={pending === 0}
          onClick={() => {
            update.reset();
            setReviewing(true);
          }}
        >
          Review {pending} {pending === 1 ? "change" : "changes"}
        </Button>
        {pending > 0 ? (
          <Button
            variant="invisible"
            onClick={() => {
              setStaged({});
              setRemovals([]);
            }}
          >
            Discard staged changes
          </Button>
        ) : null}
      </div>

      {revealing ? (
        <RevealFlow
          slug={slug}
          env={env}
          container={container}
          name={revealing}
          onClose={() => {
            setRevealing(null);
          }}
        />
      ) : null}
      {editing ? (
        <ValueDialog
          fixedName={editing.name}
          onClose={() => {
            setEditing(null);
          }}
          onSave={(name, value) => {
            setStaged((current) => ({ ...current, [name]: value }));
            setRemovals((current) => current.filter((item) => item !== name));
            setEditing(null);
          }}
        />
      ) : null}
      {reviewing ? (
        <ConfirmationDialog
          title={`Apply changes to ${container.service}?`}
          confirmButtonContent="Apply and restart service"
          cancelButtonContent="Back"
          confirmButtonType="danger"
          confirmButtonLoading={update.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") apply();
            else setReviewing(false);
          }}
        >
          <div className={box.stack}>
            <p>Only variable names are listed here; values are never shown in this review.</p>
            <ul aria-label="Changed variables">
              {setNames.map((name) => (
                <li key={name}>
                  Set or change <code>{name}</code>
                </li>
              ))}
              {removals.map((name) => (
                <li key={name}>
                  Remove the override of <code>{name}</code>
                </li>
              ))}
            </ul>
            <p>
              The <strong>{container.service}</strong> service is recreated (it gets a new container
              ID); other services are not touched.{" "}
              {container.state === "running"
                ? "It is unavailable for a few seconds."
                : "It is stopped, so it stays stopped."}{" "}
              If it does not come back healthy, the previous configuration is restored
              automatically.
            </p>
            {restored ? (
              <Callout variant="warning" role="alert" title="Nothing changed">
                {errorMessage(update.error)}. The service runs with its previous configuration.
              </Callout>
            ) : update.isError ? (
              <Callout variant="danger" role="alert">
                {errorMessage(update.error)}
              </Callout>
            ) : null}
          </div>
        </ConfirmationDialog>
      ) : null}
    </div>
  );
}
