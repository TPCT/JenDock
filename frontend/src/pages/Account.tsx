import { DeviceDesktopIcon, DeviceMobileIcon, HistoryIcon } from "@primer/octicons-react";
import { Button, ConfirmationDialog, FormControl, Label, TextInput } from "@primer/react";
import { useState, type SyntheticEvent } from "react";
import {
  useAccountSessions,
  useProfile,
  useRevokeOtherSessions,
  useRevokeSession,
  useSecurityLog,
  useUpdateProfile,
} from "../api/accountHooks";
import type { AccountSession, Profile } from "../api/accountTypes";
import { isApiError } from "../api/client";
import { AuditList } from "../components/AuditList";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { Initials } from "../components/Initials";
import { useDocumentTitle } from "../components/PageHead";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import box from "../styles/box.module.css";
import styles from "./Account.module.css";
import { AccountPage } from "./ChangePassword";
import { TwoFactorSection } from "./TwoFactor";

// ---- public profile ------------------------------------------------------------------------

function ProfileForm({ profile }: { profile: Profile }) {
  const update = useUpdateProfile();
  const [name, setName] = useState(profile.display_name);
  const [email, setEmail] = useState(profile.email ?? "");
  const [saved, setSaved] = useState(false);
  const changed = name.trim() !== profile.display_name || email.trim() !== (profile.email ?? "");
  const emailProblem =
    update.isError && isApiError(update.error) && /email/i.test(update.error.message)
      ? update.error.message.charAt(0).toUpperCase() + update.error.message.slice(1) + "."
      : null;

  const submit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaved(false);
    update.mutate(
      { display_name: name.trim() === "" ? null : name.trim(), email: email.trim() },
      {
        onSuccess: () => {
          setSaved(true);
        },
      },
    );
  };

  return (
    <form className={box.stack} onSubmit={submit} noValidate>
      <div className={styles.identity}>
        <Initials displayName={name} username={profile.username} size={64} />
        <div>
          <strong>{name.trim() === "" ? profile.username : name.trim()}</strong>
          <div className="muted">@{profile.username}</div>
        </div>
      </div>
      <FormControl>
        <FormControl.Label>Display name</FormControl.Label>
        <TextInput
          block
          value={name}
          maxLength={120}
          autoComplete="name"
          onChange={(event) => {
            setName(event.target.value);
            setSaved(false);
          }}
        />
        <FormControl.Caption>
          Shown instead of your username in the audit log and the header. Optional.
        </FormControl.Caption>
      </FormControl>
      <FormControl>
        <FormControl.Label>Email</FormControl.Label>
        <TextInput
          block
          type="email"
          value={email}
          maxLength={254}
          autoComplete="email"
          validationStatus={emailProblem ? "error" : undefined}
          onChange={(event) => {
            setEmail(event.target.value);
            setSaved(false);
          }}
        />
        {emailProblem ? (
          <FormControl.Validation variant="error">{emailProblem}</FormControl.Validation>
        ) : (
          <FormControl.Caption>
            Optional. Jendock does not send email today; this is for your administrator to reach
            you.
          </FormControl.Caption>
        )}
      </FormControl>
      {update.isError && !emailProblem ? (
        <Callout variant="danger" role="alert">
          {errorMessage(update.error)}
        </Callout>
      ) : null}
      {saved ? (
        <Callout variant="success" role="status">
          Profile updated.
        </Callout>
      ) : null}
      <div>
        <Button type="submit" variant="primary" disabled={!changed || update.isPending}>
          {update.isPending ? "Saving…" : "Update profile"}
        </Button>
      </div>
    </form>
  );
}

export function ProfilePage() {
  useDocumentTitle("Public profile");
  const profile = useProfile();
  return (
    <>
      <SettingsSection
        title="Public profile"
        intro="How you appear to other people who use this Jendock."
      >
        {profile.isPending ? <Loading label="Loading your profile" /> : null}
        {profile.isError ? (
          <ErrorCallout
            title="Could not load your profile"
            error={profile.error}
            onRetry={() => void profile.refetch()}
          />
        ) : null}
        {profile.data ? <ProfileForm profile={profile.data} /> : null}
      </SettingsSection>
      <AccountPage />
      <TwoFactorSection />
      <SessionsSection />
    </>
  );
}

// ---- account: password (existing screen) and two-factor ---------------------------------------

/** Settings -> Account: the password change that already exists, then two-factor. */
export function SecurityPage() {
  useDocumentTitle("Account");
  return (
    <>
      <AccountPage />
      <TwoFactorSection />
    </>
  );
}

// ---- sessions ------------------------------------------------------------------------------

function SessionRow({
  session,
  onRevoke,
}: {
  session: AccountSession;
  onRevoke: (session: AccountSession) => void;
}) {
  const mobile = /iOS|Android|iPadOS/.test(session.client);
  const Icon = mobile ? DeviceMobileIcon : DeviceDesktopIcon;
  return (
    <SettingsRow
      title={session.client}
      description={
        <div className={box.stack}>
          <span className={box.cluster}>
            <Icon size={16} aria-hidden="true" />
            <span>{session.ip_address ?? "Unknown address"}</span>
            {session.current ? <Label variant="success">This device</Label> : null}
          </span>
          <span>
            Last active <RelativeTime iso={session.last_seen_at} /> · signed in{" "}
            <RelativeTime iso={session.created_at} />
          </span>
        </div>
      }
      action={
        session.current ? null : (
          <Button
            size="small"
            variant="danger"
            aria-label={`Revoke the session of ${session.client}`}
            onClick={() => {
              onRevoke(session);
            }}
          >
            Revoke
          </Button>
        )
      }
    />
  );
}

function SessionsSection() {
  const sessions = useAccountSessions();
  const revoke = useRevokeSession();
  const revokeOthers = useRevokeOtherSessions();
  const [target, setTarget] = useState<AccountSession | null>(null);
  const [all, setAll] = useState(false);
  const others = sessions.data?.filter((session) => !session.current).length ?? 0;

  return (
    <SettingsSection
      title="Sessions"
      intro="Devices that are signed in to your account. Revoke any you do not recognise."
      actions={
        others > 0 ? (
          <Button
            size="small"
            variant="danger"
            onClick={() => {
              setAll(true);
            }}
          >
            Revoke all other sessions
          </Button>
        ) : null
      }
    >
      {sessions.isPending ? <Loading label="Loading sessions" /> : null}
      {sessions.isError ? (
        <ErrorCallout
          title="Could not load your sessions"
          error={sessions.error}
          onRetry={() => void sessions.refetch()}
        />
      ) : null}
      {sessions.data ? (
        <div className={box.box}>
          <ul className={box.list} aria-label="Sessions">
            {sessions.data.map((session) => (
              <li key={session.id}>
                <SessionRow session={session} onRevoke={setTarget} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {target ? (
        <ConfirmationDialog
          title={`Revoke the session of ${target.client}?`}
          confirmButtonContent="Revoke session"
          cancelButtonContent="Keep it"
          confirmButtonType="danger"
          confirmButtonLoading={revoke.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") {
              revoke.mutate(target.id, {
                onSuccess: () => {
                  setTarget(null);
                },
              });
            } else setTarget(null);
          }}
        >
          <p>That device is signed out at once and must sign in again.</p>
          {revoke.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(revoke.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
      {all ? (
        <ConfirmationDialog
          title="Revoke all other sessions?"
          confirmButtonContent="Revoke all"
          cancelButtonContent="Keep them"
          confirmButtonType="danger"
          confirmButtonLoading={revokeOthers.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") {
              revokeOthers.mutate(undefined, {
                onSuccess: () => {
                  setAll(false);
                },
              });
            } else setAll(false);
          }}
        >
          <p>
            {String(others)} {others === 1 ? "device is" : "devices are"} signed out at once. This
            browser stays signed in.
          </p>
        </ConfirmationDialog>
      ) : null}
    </SettingsSection>
  );
}

export function SessionsPage() {
  useDocumentTitle("Sessions");
  return <SessionsSection />;
}

// ---- security log --------------------------------------------------------------------------

export function SecurityLogPage() {
  useDocumentTitle("Security log");
  const log = useSecurityLog();
  const events = log.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <SettingsSection
      title="Security log"
      intro="What you did, and what happened to your account: sign-ins, password and two-factor changes, tokens."
    >
      {log.isPending ? <Loading label="Loading the security log" /> : null}
      {log.isError ? (
        <ErrorCallout
          title="Could not load the security log"
          error={log.error}
          onRetry={() => void log.refetch()}
        />
      ) : null}
      {log.data && events.length === 0 ? (
        <EmptyState icon={<HistoryIcon size={32} aria-hidden="true" />} title="Nothing here yet">
          Sign-ins and changes to your account will be listed here.
        </EmptyState>
      ) : null}
      {events.length > 0 ? (
        <div className={box.box}>
          <AuditList events={events} label="Security log" />
          {log.hasNextPage ? (
            <div className={box.footer}>
              <span className="muted">Showing {String(events.length)} events</span>
              <Button
                size="small"
                disabled={log.isFetchingNextPage}
                onClick={() => void log.fetchNextPage()}
              >
                {log.isFetchingNextPage ? "Loading…" : "Load older events"}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </SettingsSection>
  );
}
