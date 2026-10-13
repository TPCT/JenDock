import { KebabHorizontalIcon, PersonAddIcon, PeopleIcon } from "@primer/octicons-react";
import {
  ActionList,
  ActionMenu,
  Button,
  ConfirmationDialog,
  Dialog,
  FormControl,
  IconButton,
  Label,
  Select,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  useCreateUser,
  useDeleteUser,
  useResetPassword,
  useUpdateUser,
  useUserAccess,
  useUsers,
} from "../api/accessHooks";
import { GLOBAL_ROLES, type OneTimePassword, type UserSummary } from "../api/accessTypes";
import { useSession } from "../api/hooks";
import type { GlobalRole } from "../api/types";
import { CopyField } from "../components/CopyField";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";

export const ROLE_HELP: Record<GlobalRole, string> = {
  owner: "Everything, including managing owners and members.",
  admin: "Everything except managing owners.",
  member: "Only what is granted on projects, environments and containers.",
};

function roleVariant(role: GlobalRole): "accent" | "done" | "secondary" {
  if (role === "owner") return "done";
  return role === "admin" ? "accent" : "secondary";
}

/** Owners may hand out every role; admins cannot create or promote owners. */
function assignableRoles(actor: GlobalRole): readonly GlobalRole[] {
  return actor === "owner" ? GLOBAL_ROLES : GLOBAL_ROLES.filter((role) => role !== "owner");
}

function RoleSelect({
  value,
  onChange,
  actor,
}: {
  value: GlobalRole;
  onChange: (role: GlobalRole) => void;
  actor: GlobalRole;
}) {
  return (
    <FormControl>
      <FormControl.Label>Role</FormControl.Label>
      <Select
        block
        value={value}
        onChange={(event) => {
          onChange(event.target.value as GlobalRole);
        }}
      >
        {assignableRoles(actor).map((role) => (
          <Select.Option key={role} value={role}>
            {capitalize(role)}
          </Select.Option>
        ))}
      </Select>
      <FormControl.Caption>{ROLE_HELP[value]}</FormControl.Caption>
    </FormControl>
  );
}

/** The generated password is shown exactly once; the person must change it at first sign-in. */
function OneTimePasswordDialog({
  result,
  heading,
  onClose,
}: {
  result: OneTimePassword;
  heading: string;
  onClose: () => void;
}) {
  return (
    <Dialog
      title={heading}
      width="large"
      onClose={onClose}
      footerButtons={[{ buttonType: "primary", content: "I have copied it", onClick: onClose }]}
    >
      <div className={box.stack}>
        <Callout variant="warning" title="This is the only time you will see it">
          Give this one-time password to {result.user.display_name || result.user.username} through
          a safe channel. They must choose a new password the next time they sign in; Jendock keeps
          only a fingerprint, so it cannot be shown again.
        </Callout>
        <CopyField
          label={`One-time password of ${result.user.username}`}
          value={result.one_time_password}
        />
      </div>
    </Dialog>
  );
}

const USERNAME = /^[a-z0-9][a-z0-9._-]{2,63}$/;

function CreateUserDialog({
  actor,
  onClose,
  onCreated,
}: {
  actor: GlobalRole;
  onClose: () => void;
  onCreated: (created: OneTimePassword) => void;
}) {
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<GlobalRole>("member");
  const [problem, setProblem] = useState<string | null>(null);
  const create = useCreateUser();
  const submit = () => {
    if (!USERNAME.test(username.trim())) {
      setProblem(
        "The username needs 3 to 64 lowercase letters, digits, dots, dashes or underscores.",
      );
      return;
    }
    setProblem(null);
    create.mutate(
      {
        username: username.trim(),
        display_name: displayName.trim(),
        email: email.trim() === "" ? null : email.trim(),
        role,
      },
      { onSuccess: onCreated },
    );
  };
  return (
    <Dialog
      title="Add a member"
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: create.isPending ? "Creating…" : "Create member",
          disabled: create.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        <FormControl required>
          <FormControl.Label>Username</FormControl.Label>
          <TextInput
            block
            monospace
            autoCapitalize="none"
            spellCheck={false}
            value={username}
            onChange={(event) => {
              setUsername(event.target.value);
            }}
          />
          <FormControl.Caption>
            What they type to sign in. It cannot be changed later.
          </FormControl.Caption>
        </FormControl>
        <FormControl>
          <FormControl.Label>Name</FormControl.Label>
          <TextInput
            block
            value={displayName}
            onChange={(event) => {
              setDisplayName(event.target.value);
            }}
          />
        </FormControl>
        <FormControl>
          <FormControl.Label>Email</FormControl.Label>
          <TextInput
            block
            type="email"
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
          />
          <FormControl.Caption>Optional. Jendock does not send email.</FormControl.Caption>
        </FormControl>
        <RoleSelect value={role} onChange={setRole} actor={actor} />
        <p className="muted">
          Jendock generates a one-time password. After creating the member you copy it and hand it
          over; they must change it when they first sign in.
        </p>
        {problem ? (
          <Callout variant="danger" role="alert">
            {problem}
          </Callout>
        ) : null}
        {create.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(create.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function EditUserDialog({
  user,
  actor,
  isSelf,
  onClose,
}: {
  user: UserSummary;
  actor: GlobalRole;
  isSelf: boolean;
  onClose: () => void;
}) {
  const [displayName, setDisplayName] = useState(user.display_name);
  const [email, setEmail] = useState(user.email ?? "");
  const [role, setRole] = useState<GlobalRole>(user.role);
  const update = useUpdateUser(user.id);
  return (
    <Dialog
      title={`Edit ${user.username}`}
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: update.isPending ? "Saving…" : "Save",
          disabled: update.isPending,
          onClick: () => {
            update.mutate(
              {
                display_name: displayName.trim(),
                email: email.trim(),
                ...(isSelf ? {} : { role }),
              },
              { onSuccess: onClose },
            );
          },
        },
      ]}
    >
      <div className={box.stack}>
        <FormControl>
          <FormControl.Label>Name</FormControl.Label>
          <TextInput
            block
            value={displayName}
            onChange={(event) => {
              setDisplayName(event.target.value);
            }}
          />
        </FormControl>
        <FormControl>
          <FormControl.Label>Email</FormControl.Label>
          <TextInput
            block
            type="email"
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
          />
        </FormControl>
        {isSelf ? (
          <p className="muted">You cannot change your own role.</p>
        ) : (
          <RoleSelect value={role} onChange={setRole} actor={actor} />
        )}
        {update.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(update.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function UserAccessDialog({ user, onClose }: { user: UserSummary; onClose: () => void }) {
  const access = useUserAccess(user.id);
  return (
    <Dialog title={`Access of ${user.username}`} width="large" onClose={onClose}>
      {user.role !== "member" ? (
        <p>
          {capitalize(user.role)}s have full access to every project. Grants only matter for
          members.
        </p>
      ) : null}
      {access.isPending ? <Loading label="Loading access" /> : null}
      {access.isError ? (
        <ErrorCallout title="Could not load the access" error={access.error} />
      ) : null}
      {access.data?.length === 0 && user.role === "member" ? (
        <p>
          No grants yet. Open a project, then Settings and Access, to give {user.username} a role.
        </p>
      ) : null}
      {access.data && access.data.length > 0 ? (
        <ul className={box.list} aria-label={`Grants of ${user.username}`}>
          {access.data.map((grant) => (
            <li key={grant.id} className={box.row}>
              <Link
                to={`/projects/${encodeURIComponent(grant.project)}/settings/access`}
                onClick={onClose}
              >
                {grant.project}
              </Link>
              {" · "}
              {grant.environment ? capitalize(grant.environment) : "All environments"}
              {grant.service ? ` · ${grant.service}` : ""}{" "}
              {grant.role ? <Label>{grant.role}</Label> : null}
              {grant.permissions.map((permission) => (
                <Label key={permission}>{permission}</Label>
              ))}
            </li>
          ))}
        </ul>
      ) : null}
    </Dialog>
  );
}

type Pending = {
  kind: "edit" | "access" | "disable" | "enable" | "reset" | "delete";
  user: UserSummary;
} | null;

function MemberRow({
  user,
  isSelf,
  actor,
  onAction,
}: {
  user: UserSummary;
  isSelf: boolean;
  actor: GlobalRole;
  onAction: (kind: NonNullable<Pending>["kind"], user: UserSummary) => void;
}) {
  // Admins cannot touch owners; the server enforces it too.
  const locked = actor !== "owner" && user.role === "owner";
  const label = user.display_name ? `${user.display_name} (${user.username})` : user.username;
  return (
    <SettingsRow
      title={label}
      description={
        <div className={box.stack}>
          <span>
            {user.email ?? "No email"} · added <RelativeTime iso={user.created_at} />
          </span>
          <span className={box.cluster}>
            <Label variant={roleVariant(user.role)}>{capitalize(user.role)}</Label>
            {isSelf ? <Label>You</Label> : null}
            {user.is_active ? null : <Label variant="danger">Disabled</Label>}
            {user.must_change_password ? (
              <Label variant="attention">Must change password</Label>
            ) : null}
          </span>
        </div>
      }
      action={
        locked ? null : (
          <ActionMenu>
            <ActionMenu.Anchor>
              <IconButton
                icon={KebabHorizontalIcon}
                variant="invisible"
                aria-label={`Actions for ${user.username}`}
              />
            </ActionMenu.Anchor>
            <ActionMenu.Overlay width="medium">
              <ActionList>
                <ActionList.Item
                  onSelect={() => {
                    onAction("edit", user);
                  }}
                >
                  Edit
                </ActionList.Item>
                <ActionList.Item
                  onSelect={() => {
                    onAction("access", user);
                  }}
                >
                  View access
                </ActionList.Item>
                <ActionList.Item
                  onSelect={() => {
                    onAction("reset", user);
                  }}
                >
                  Reset password
                </ActionList.Item>
                {isSelf ? null : (
                  <>
                    <ActionList.Divider />
                    <ActionList.Item
                      onSelect={() => {
                        onAction(user.is_active ? "disable" : "enable", user);
                      }}
                    >
                      {user.is_active ? "Disable" : "Enable"}
                    </ActionList.Item>
                    <ActionList.Item
                      variant="danger"
                      onSelect={() => {
                        onAction("delete", user);
                      }}
                    >
                      Delete
                    </ActionList.Item>
                  </>
                )}
              </ActionList>
            </ActionMenu.Overlay>
          </ActionMenu>
        )
      }
    />
  );
}

/** Settings → Members: everyone who can sign in, their role, and the administration of both. */
export function MembersPage() {
  const session = useSession();
  const users = useUsers();
  const [creating, setCreating] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [shown, setShown] = useState<{ result: OneTimePassword; heading: string } | null>(null);
  const update = useUpdateUser(pending?.user.id ?? 0);
  const remove = useDeleteUser();
  const reset = useResetPassword();
  const me = session.data?.user;
  if (!me) return null;
  const close = () => {
    setPending(null);
  };

  return (
    <SettingsSection
      title="Members"
      intro="Everyone who can sign in to Jendock. Owners and admins see everything; members only the projects, environments and containers they have been granted access to."
      actions={
        <Button
          variant="primary"
          size="small"
          leadingVisual={PersonAddIcon}
          onClick={() => {
            setCreating(true);
          }}
        >
          Add member
        </Button>
      }
    >
      {users.isPending ? <Loading label="Loading members" /> : null}
      {users.isError ? (
        <ErrorCallout
          title="Could not load the members"
          error={users.error}
          onRetry={() => void users.refetch()}
        />
      ) : null}
      {users.data?.length === 0 ? (
        <EmptyState icon={<PeopleIcon size={32} aria-hidden="true" />} title="No members">
          Add the first member to share Jendock.
        </EmptyState>
      ) : null}
      {users.data && users.data.length > 0 ? (
        <div className={box.box}>
          <div className={box.header}>
            <span>{users.data.length} members</span>
          </div>
          <ul className={box.list} aria-label="Members">
            {users.data.map((user) => (
              <li key={user.id}>
                <MemberRow
                  user={user}
                  isSelf={user.id === me.id}
                  actor={me.role}
                  onAction={(kind, target) => {
                    setPending({ kind, user: target });
                  }}
                />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {creating ? (
        <CreateUserDialog
          actor={me.role}
          onClose={() => {
            setCreating(false);
          }}
          onCreated={(created) => {
            setCreating(false);
            setShown({ result: created, heading: "Copy the one-time password" });
          }}
        />
      ) : null}

      {pending?.kind === "edit" ? (
        <EditUserDialog
          user={pending.user}
          actor={me.role}
          isSelf={pending.user.id === me.id}
          onClose={close}
        />
      ) : null}
      {pending?.kind === "access" ? <UserAccessDialog user={pending.user} onClose={close} /> : null}

      {pending?.kind === "disable" || pending?.kind === "enable" ? (
        <ConfirmationDialog
          title={`${capitalize(pending.kind)} ${pending.user.username}?`}
          confirmButtonContent={capitalize(pending.kind)}
          cancelButtonContent="Cancel"
          confirmButtonType={pending.kind === "disable" ? "danger" : "primary"}
          confirmButtonLoading={update.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              close();
              return;
            }
            update.mutate({ is_active: pending.kind === "enable" }, { onSuccess: close });
          }}
        >
          <p>
            {pending.kind === "disable"
              ? "They are signed out at once and cannot sign in again until you enable them. Their API tokens stop working too. Nothing they created is removed."
              : "They can sign in again with their current password, and their API tokens work again."}
          </p>
          {update.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(update.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}

      {pending?.kind === "reset" ? (
        <ConfirmationDialog
          title={`Reset the password of ${pending.user.username}?`}
          confirmButtonContent="Reset password"
          cancelButtonContent="Cancel"
          confirmButtonType="danger"
          confirmButtonLoading={reset.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              close();
              return;
            }
            reset.mutate(pending.user.id, {
              onSuccess: (result) => {
                close();
                setShown({ result, heading: "Copy the one-time password" });
              },
            });
          }}
        >
          <p>
            Jendock generates a one-time password and signs them out everywhere. They must choose a
            new password at their next sign-in.
          </p>
          {reset.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(reset.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}

      {pending?.kind === "delete" ? (
        <ConfirmationDialog
          title={`Delete ${pending.user.username}?`}
          confirmButtonContent="Delete member"
          cancelButtonContent="Keep"
          confirmButtonType="danger"
          confirmButtonLoading={remove.isPending}
          onClose={(gesture) => {
            if (gesture !== "confirm") {
              close();
              return;
            }
            remove.mutate(pending.user.id, { onSuccess: close });
          }}
        >
          <p>
            Their sessions, API tokens and access grants are deleted. Runs they started stay in the
            history. This cannot be undone; disable the member instead to keep the account.
          </p>
          {remove.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(remove.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}

      {shown ? (
        <OneTimePasswordDialog
          result={shown.result}
          heading={shown.heading}
          onClose={() => {
            setShown(null);
          }}
        />
      ) : null}
    </SettingsSection>
  );
}
