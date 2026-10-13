import { PersonAddIcon, PeopleIcon, TrashIcon } from "@primer/octicons-react";
import {
  Button,
  Checkbox,
  CheckboxGroup,
  ConfirmationDialog,
  Dialog,
  FormControl,
  IconButton,
  Label,
  Radio,
  RadioGroup,
  Select,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useProjectAccess, useRemoveGrant, useSetGrant, useUsers } from "../api/accessHooks";
import {
  ACCESS_ROLES,
  CONTAINER_PERMISSIONS,
  type Grant,
  type SetGrantBody,
} from "../api/accessTypes";
import { useProject } from "../api/hooks";
import {
  ENVIRONMENTS,
  type AccessRole,
  type ContainerPermission,
  type EnvironmentName,
  type Project,
} from "../api/types";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { SettingsSection } from "../components/SettingsSection";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";

export const ROLE_TEXT: Record<AccessRole, string> = {
  viewer: "Read projects, runs, logs, deployments, the container list and database structure.",
  developer: "Viewer, plus start, cancel and retry runs and edit the development pipeline.",
  maintainer:
    "Developer, plus deploy and redeploy, manage containers, edit variables, open terminals, manage domains and edit the production pipeline.",
};

const PERMISSION_TEXT: Record<ContainerPermission, string> = {
  logs: "Read the container's logs.",
  terminal: "Open a terminal in the container.",
  actions: "Start, stop and restart the container.",
};

type Scope = "project" | "environment" | "container";

function scopeLabel(grant: Grant): string {
  if (grant.environment === null) return "Entire project (all environments)";
  const env = capitalize(grant.environment);
  return grant.service ? `Container ${grant.service} · ${env}` : `${env} environment`;
}

/** Group grants by person, the way GitHub's "Manage access" lists people once. */
function byPerson(grants: Grant[]): { key: number; title: string; grants: Grant[] }[] {
  const groups = new Map<number, { key: number; title: string; grants: Grant[] }>();
  for (const grant of grants) {
    const found = groups.get(grant.user_id);
    if (found) found.grants.push(grant);
    else {
      const title = grant.display_name
        ? `${grant.display_name} (${grant.username})`
        : grant.username;
      groups.set(grant.user_id, { key: grant.user_id, title, grants: [grant] });
    }
  }
  return [...groups.values()];
}

function servicesOf(project: Project | undefined): string[] {
  const names = new Set<string>();
  for (const environment of project?.environments ?? []) {
    for (const service of environment.services) names.add(service);
  }
  return [...names].sort();
}

function AddAccessDialog({ slug, onClose }: { slug: string; onClose: () => void }) {
  const users = useUsers();
  const project = useProject(slug);
  const set = useSetGrant(slug);
  const [userId, setUserId] = useState("");
  const [scope, setScope] = useState<Scope>("project");
  const [environment, setEnvironment] = useState<EnvironmentName>("development");
  const [service, setService] = useState("");
  const [role, setRole] = useState<AccessRole>("viewer");
  const [permissions, setPermissions] = useState<ContainerPermission[]>(["logs"]);
  const [problem, setProblem] = useState<string | null>(null);
  const members = (users.data ?? []).filter((user) => user.role === "member" && user.is_active);
  const services = servicesOf(project.data);

  const submit = () => {
    if (userId === "") {
      setProblem("Choose a member.");
      return;
    }
    const body: SetGrantBody = { user_id: Number(userId) };
    if (scope !== "project") body.environment = environment;
    if (scope === "container") {
      if (service.trim() === "") {
        setProblem("Name the container service, for example web.");
        return;
      }
      if (permissions.length === 0) {
        setProblem("Choose at least one permission.");
        return;
      }
      body.service = service.trim();
      body.permissions = permissions;
    } else {
      body.role = role;
    }
    setProblem(null);
    set.mutate(body, { onSuccess: onClose });
  };

  return (
    <Dialog
      title="Add people"
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: set.isPending ? "Adding…" : "Add to project",
          disabled: set.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        <FormControl required>
          <FormControl.Label>Member</FormControl.Label>
          <Select
            block
            value={userId}
            onChange={(event) => {
              setUserId(event.target.value);
            }}
          >
            <Select.Option value="">Choose a member…</Select.Option>
            {members.map((user) => (
              <Select.Option key={user.id} value={String(user.id)}>
                {user.display_name ? `${user.display_name} (${user.username})` : user.username}
              </Select.Option>
            ))}
          </Select>
          <FormControl.Caption>
            Owners and admins already have access to everything. Add people under Settings, Members
            first.
          </FormControl.Caption>
        </FormControl>
        <RadioGroup
          name="scope"
          onChange={(value) => {
            if (value) setScope(value as Scope);
          }}
        >
          <RadioGroup.Label>Give access to</RadioGroup.Label>
          <FormControl>
            <Radio value="project" checked={scope === "project"} />
            <FormControl.Label>The whole project</FormControl.Label>
            <FormControl.Caption>Development and production.</FormControl.Caption>
          </FormControl>
          <FormControl>
            <Radio value="environment" checked={scope === "environment"} />
            <FormControl.Label>One environment</FormControl.Label>
          </FormControl>
          <FormControl>
            <Radio value="container" checked={scope === "container"} />
            <FormControl.Label>One container service</FormControl.Label>
            <FormControl.Caption>
              Only logs, terminal and start/stop/restart of that service.
            </FormControl.Caption>
          </FormControl>
        </RadioGroup>
        {scope !== "project" ? (
          <FormControl>
            <FormControl.Label>Environment</FormControl.Label>
            <Select
              block
              value={environment}
              onChange={(event) => {
                setEnvironment(event.target.value as EnvironmentName);
              }}
            >
              {ENVIRONMENTS.map((name) => (
                <Select.Option key={name} value={name}>
                  {capitalize(name)}
                </Select.Option>
              ))}
            </Select>
          </FormControl>
        ) : null}
        {scope === "container" ? (
          <>
            <FormControl required>
              <FormControl.Label>Service</FormControl.Label>
              <TextInput
                block
                monospace
                list="known-services"
                value={service}
                onChange={(event) => {
                  setService(event.target.value);
                }}
              />
              <datalist id="known-services">
                {services.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              <FormControl.Caption>The Compose service name, for example web.</FormControl.Caption>
            </FormControl>
            <CheckboxGroup
              onChange={(selected) => {
                setPermissions(selected as ContainerPermission[]);
              }}
            >
              <CheckboxGroup.Label>Permissions</CheckboxGroup.Label>
              {CONTAINER_PERMISSIONS.map((permission) => (
                <FormControl key={permission}>
                  <Checkbox value={permission} checked={permissions.includes(permission)} />
                  <FormControl.Label>{permission}</FormControl.Label>
                  <FormControl.Caption>{PERMISSION_TEXT[permission]}</FormControl.Caption>
                </FormControl>
              ))}
            </CheckboxGroup>
          </>
        ) : (
          <FormControl>
            <FormControl.Label>Role</FormControl.Label>
            <Select
              block
              value={role}
              onChange={(event) => {
                setRole(event.target.value as AccessRole);
              }}
            >
              {ACCESS_ROLES.map((name) => (
                <Select.Option key={name} value={name}>
                  {capitalize(name)}
                </Select.Option>
              ))}
            </Select>
            <FormControl.Caption>{ROLE_TEXT[role]}</FormControl.Caption>
          </FormControl>
        )}
        {problem ? (
          <Callout variant="danger" role="alert">
            {problem}
          </Callout>
        ) : null}
        {set.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(set.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function GrantRow({
  slug,
  grant,
  onRemove,
}: {
  slug: string;
  grant: Grant;
  onRemove: (grant: Grant) => void;
}) {
  const set = useSetGrant(slug);
  const label = scopeLabel(grant);
  return (
    <div className={`${box.row} ${box.spread}`}>
      <div className={box.stack}>
        <strong>{label}</strong>
        {grant.role ? null : (
          <span className={box.cluster}>
            {grant.permissions.map((permission) => (
              <Label key={permission}>{permission}</Label>
            ))}
          </span>
        )}
        {set.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(set.error)}
          </Callout>
        ) : null}
      </div>
      <span className={box.cluster}>
        {grant.role ? (
          <Select
            aria-label={`Role of ${grant.username} on ${label}`}
            value={grant.role}
            disabled={set.isPending}
            onChange={(event) => {
              const body: SetGrantBody = {
                user_id: grant.user_id,
                role: event.target.value as AccessRole,
              };
              if (grant.environment) body.environment = grant.environment;
              set.mutate(body);
            }}
          >
            {ACCESS_ROLES.map((name) => (
              <Select.Option key={name} value={name}>
                {capitalize(name)}
              </Select.Option>
            ))}
          </Select>
        ) : null}
        <IconButton
          icon={TrashIcon}
          variant="invisible"
          aria-label={`Remove ${grant.username} from ${label}`}
          onClick={() => {
            onRemove(grant);
          }}
        />
      </span>
    </div>
  );
}

/** Project settings → Access: GitHub's "Manage access" for roles, environments and containers. */
export function AccessSettingsPage() {
  const { slug = "" } = useParams();
  const access = useProjectAccess(slug);
  const remove = useRemoveGrant(slug);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Grant | null>(null);

  return (
    <SettingsSection
      title="Access"
      intro="Who can see and change this project. Owners and admins always have full access; members get exactly the roles granted here."
      actions={
        <Button
          variant="primary"
          size="small"
          leadingVisual={PersonAddIcon}
          onClick={() => {
            setAdding(true);
          }}
        >
          Add people
        </Button>
      }
    >
      {access.isPending ? <Loading label="Loading access" /> : null}
      {access.isError ? (
        <ErrorCallout
          title="Could not load the access list"
          error={access.error}
          onRetry={() => void access.refetch()}
        />
      ) : null}
      {access.data ? (
        <>
          <div className={box.box}>
            <div className={box.header}>
              <span>Full access ({access.data.privileged.length})</span>
            </div>
            <ul className={box.list} aria-label="Owners and admins">
              {access.data.privileged.map((user) => (
                <li key={user.id} className={`${box.row} ${box.spread}`}>
                  <span>
                    {user.display_name ? `${user.display_name} (${user.username})` : user.username}
                  </span>
                  <Label>{capitalize(user.role)}</Label>
                </li>
              ))}
            </ul>
          </div>
          {access.data.grants.length === 0 ? (
            <EmptyState icon={<PeopleIcon size={32} aria-hidden="true" />} title="No members yet">
              Nobody besides owners and admins can see this project. Add people to give them a role.
            </EmptyState>
          ) : (
            byPerson(access.data.grants).map((person) => (
              <div key={person.key} className={box.box}>
                <div className={box.header}>
                  <span>{person.title}</span>
                </div>
                {person.grants.map((grant) => (
                  <GrantRow key={grant.id} slug={slug} grant={grant} onRemove={setRemoving} />
                ))}
              </div>
            ))
          )}
        </>
      ) : null}

      {adding ? (
        <AddAccessDialog
          slug={slug}
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
      {removing ? (
        <ConfirmationDialog
          title={`Remove ${removing.username}?`}
          confirmButtonContent="Remove access"
          cancelButtonContent="Keep"
          confirmButtonType="danger"
          confirmButtonLoading={remove.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              setRemoving(null);
              return;
            }
            remove.mutate(removing.id, {
              onSuccess: () => {
                setRemoving(null);
              },
            });
          }}
        >
          <p>
            {removing.username} loses &quot;{scopeLabel(removing)}&quot; at once. Other grants of
            theirs stay.
          </p>
          {remove.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(remove.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </SettingsSection>
  );
}
