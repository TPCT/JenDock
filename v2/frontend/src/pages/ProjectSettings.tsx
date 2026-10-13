import { BellIcon, GearIcon, KeyIcon, RocketIcon, WebhookIcon } from "@primer/octicons-react";
import {
  Button,
  Checkbox,
  ConfirmationDialog,
  FormControl,
  Radio,
  RadioGroup,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useUpdateEnvironment, useUpdateProject } from "../api/adminHooks";
import type { GitTransport } from "../api/adminTypes";
import { useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentSummary, type Project } from "../api/types";
import { Callout, errorMessage } from "../components/Feedback";
import { GithubSetup } from "../components/GithubSetup";
import { SettingsLayout, type SettingsItem } from "../components/SettingsLayout";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";

export function ProjectSettingsLayout() {
  const { slug = "" } = useParams();
  const base = `/projects/${encodeURIComponent(slug)}/settings`;
  const items: SettingsItem[] = [
    { to: base, label: "General", icon: GearIcon },
    { to: `${base}/environments`, label: "Environments", icon: RocketIcon },
    { to: `${base}/webhook`, label: "GitHub connection", icon: WebhookIcon },
    { to: `${base}/secrets`, label: "Secrets", icon: KeyIcon },
    { to: `${base}/notifications`, label: "Notifications", icon: BellIcon },
  ];
  return <SettingsLayout label="Project settings" items={items} />;
}

function useCurrent(): Project | undefined {
  const { slug = "" } = useParams();
  return useProject(slug).data;
}

function ChangeRepository({ project }: { project: Project }) {
  const update = useUpdateProject(project.slug);
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(project.repository);
  const valid = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/.test(value.trim());
  return (
    <>
      <Button
        variant="danger"
        onClick={() => {
          setValue(project.repository);
          setOpen(true);
        }}
      >
        Change repository
      </Button>
      {open ? (
        <ConfirmationDialog
          title="Change the repository?"
          confirmButtonContent="Change repository"
          confirmButtonType="danger"
          cancelButtonContent="Cancel"
          confirmButtonLoading={update.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              setOpen(false);
              return;
            }
            if (!valid) return;
            update.mutate(
              { repository: value.trim() },
              {
                onSuccess: () => {
                  setOpen(false);
                },
              },
            );
          }}
        >
          <div className={box.stack}>
            <p>
              Pushes from the old repository stop matching and the webhook and deploy key of the new
              one must be added in GitHub (see GitHub connection).
            </p>
            <FormControl>
              <FormControl.Label>New repository</FormControl.Label>
              <TextInput
                block
                monospace
                value={value}
                validationStatus={valid ? undefined : "error"}
                onChange={(event) => {
                  setValue(event.target.value);
                }}
              />
              <FormControl.Caption>Written as owner/name.</FormControl.Caption>
            </FormControl>
            {update.isError ? (
              <Callout variant="danger" role="alert">
                {errorMessage(update.error)}
              </Callout>
            ) : null}
          </div>
        </ConfirmationDialog>
      ) : null}
    </>
  );
}

export function GeneralSettingsPage() {
  const project = useCurrent();
  const update = useUpdateProject(project?.slug ?? "");
  const [name, setName] = useState<string | null>(null);
  const [transport, setTransport] = useState<GitTransport | null>(null);
  if (!project) return null;
  const nameValue = name ?? project.name;
  const transportValue = transport ?? project.git_transport;
  return (
    <>
      <SettingsSection
        title="General"
        intro="How the project is shown and how Jendock reads the code."
      >
        <form
          className={box.stack}
          onSubmit={(event) => {
            event.preventDefault();
            if (nameValue.trim() === "") return;
            update.mutate(
              { name: nameValue.trim() },
              {
                onSuccess: () => {
                  setName(null);
                },
              },
            );
          }}
        >
          <FormControl>
            <FormControl.Label>Project name</FormControl.Label>
            <TextInput
              block
              value={nameValue}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
            <FormControl.Caption>
              Shown in lists and headings. The short name ({project.slug}) cannot change.
            </FormControl.Caption>
          </FormControl>
          <div>
            <Button type="submit" disabled={update.isPending || nameValue.trim() === project.name}>
              Save name
            </Button>
          </div>
        </form>
        <form
          className={box.stack}
          onSubmit={(event) => {
            event.preventDefault();
            update.mutate(
              { git_transport: transportValue },
              {
                onSuccess: () => {
                  setTransport(null);
                },
              },
            );
          }}
        >
          <RadioGroup
            name="settings-transport"
            onChange={(selected) => {
              if (selected === "ssh" || selected === "https") setTransport(selected);
            }}
          >
            <RadioGroup.Label>How the code is fetched</RadioGroup.Label>
            <FormControl>
              <Radio value="ssh" checked={transportValue === "ssh"} />
              <FormControl.Label>SSH deploy key (recommended)</FormControl.Label>
              <FormControl.Caption>
                Private and public repositories; needs the deploy key in GitHub.
              </FormControl.Caption>
            </FormControl>
            <FormControl>
              <Radio value="https" checked={transportValue === "https"} />
              <FormControl.Label>HTTPS (public repositories only)</FormControl.Label>
              <FormControl.Caption>
                No key needed; private repositories cannot be read.
              </FormControl.Caption>
            </FormControl>
          </RadioGroup>
          <div>
            <Button
              type="submit"
              disabled={update.isPending || transportValue === project.git_transport}
            >
              Save access method
            </Button>
          </div>
        </form>
        {update.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(update.error)}
          </Callout>
        ) : null}
        {update.isSuccess ? (
          <Callout variant="success" role="status">
            Saved.
          </Callout>
        ) : null}
      </SettingsSection>
      <SettingsSection title="Danger zone" danger>
        <SettingsRow
          title="Change repository"
          description={
            <>
              This project builds <code>{project.repository}</code>. Pointing it at another
              repository changes what every future run builds.
            </>
          }
          action={<ChangeRepository project={project} />}
        />
      </SettingsSection>
    </>
  );
}

function EnvironmentForm({
  project,
  environment,
}: {
  project: Project;
  environment: EnvironmentSummary;
}) {
  const update = useUpdateEnvironment(project.slug, environment.name);
  const [branch, setBranch] = useState(environment.branch);
  const [auto, setAuto] = useState(environment.auto_deploy);
  const dirty = branch.trim() !== environment.branch || auto !== environment.auto_deploy;
  return (
    <form
      className={`${box.box} ${box.row} ${box.stack}`}
      aria-label={`${capitalize(environment.name)} environment`}
      onSubmit={(event) => {
        event.preventDefault();
        if (branch.trim() === "") return;
        update.mutate({ branch: branch.trim(), auto_deploy: auto });
      }}
    >
      <strong>{capitalize(environment.name)}</strong>
      <FormControl>
        <FormControl.Label>{capitalize(environment.name)} branch</FormControl.Label>
        <TextInput
          block
          monospace
          value={branch}
          onChange={(event) => {
            setBranch(event.target.value);
          }}
        />
        <FormControl.Caption>
          A push to this branch deploys the {environment.name} environment. The two environments
          must use different branches.
        </FormControl.Caption>
      </FormControl>
      <FormControl>
        <Checkbox
          checked={auto}
          onChange={(event) => {
            setAuto(event.target.checked);
          }}
        />
        <FormControl.Label>Deploy automatically on push</FormControl.Label>
        <FormControl.Caption>
          On: every push to the branch starts a run. Off: GitHub is still told, the push is
          recorded, but no run starts until you press Run pipeline yourself. Choose Off for
          Production if you want to release deliberately.
        </FormControl.Caption>
      </FormControl>
      {update.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(update.error)}
        </Callout>
      ) : null}
      {update.isSuccess && !dirty ? (
        <Callout variant="success" role="status">
          Saved.
        </Callout>
      ) : null}
      <div>
        <Button type="submit" variant="primary" disabled={!dirty || update.isPending}>
          {update.isPending ? "Saving…" : `Save ${environment.name}`}
        </Button>
      </div>
    </form>
  );
}

export function EnvironmentsSettingsPage() {
  const project = useCurrent();
  if (!project) return null;
  return (
    <SettingsSection
      title="Environments"
      intro="Every project has exactly two environments. Choose which branch each one deploys and whether pushes deploy by themselves."
    >
      {ENVIRONMENTS.map((name) => {
        const environment = project.environments.find((item) => item.name === name);
        return environment ? (
          <EnvironmentForm
            key={`${name}-${environment.branch}-${String(environment.auto_deploy)}`}
            project={project}
            environment={environment}
          />
        ) : null;
      })}
    </SettingsSection>
  );
}

export function WebhookSettingsPage() {
  const project = useCurrent();
  if (!project) return null;
  return (
    <SettingsSection
      title="GitHub connection"
      intro={
        <>
          What to set up in GitHub for <span className="ltr">{project.repository}</span> so that
          Jendock can read the code and learn about pushes.
        </>
      }
    >
      <div className={box.box}>
        <div className={box.row}>
          <GithubSetup repository={project.repository} transport={project.git_transport} />
        </div>
      </div>
    </SettingsSection>
  );
}
