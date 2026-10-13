import { TerminalIcon } from "@primer/octicons-react";
import { Button, FormControl, Heading, TextInput } from "@primer/react";
import { useState, type SyntheticEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useChangePassword } from "../api/accessHooks";
import { isApiError } from "../api/client";
import { useLogout, useSession } from "../api/hooks";
import { Callout } from "../components/Feedback";
import { useDocumentTitle } from "../components/PageHead";
import { SettingsSection } from "../components/SettingsSection";
import styles from "./Login.module.css";

const MIN_LENGTH = 12;

function firstProblem(current: string, next: string, repeat: string): string | null {
  if (current === "") return "Enter your current password.";
  if (next.length < MIN_LENGTH) return `The new password needs at least ${MIN_LENGTH} characters.`;
  if (next !== repeat) return "The two new passwords do not match.";
  if (next === current) return "Choose a password different from the current one.";
  return null;
}

/** Current password, new password twice. `onDone` runs once the server accepted the change. */
/** Live hints under the new-password field: what is still missing, ticked as the user types. */
function Hints({ next, repeat }: { next: string; repeat: string }) {
  const rules = [
    { ok: next.length >= MIN_LENGTH, text: `At least ${String(MIN_LENGTH)} characters` },
    { ok: next.length >= 16, text: "Tip: 16 or more characters, or a few words, is stronger" },
    { ok: next !== "" && next === repeat, text: "Both entries match" },
  ];
  return (
    <ul className={styles.hints} aria-label="Password hints">
      {rules.map((rule) => (
        <li key={rule.text} className={rule.ok ? styles.hintOk : styles.hintTodo}>
          <span aria-hidden="true">{rule.ok ? "✓" : "•"}</span> {rule.text}
          <span className={styles.srOnly}>{rule.ok ? " (done)" : " (not yet)"}</span>
        </li>
      ))}
    </ul>
  );
}

function ChangePasswordForm({ onDone }: { onDone?: () => void }) {
  const change = useChangePassword();
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const onSubmit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const read = (name: string): string => {
      const value = data.get(name);
      return typeof value === "string" ? value : "";
    };
    const current = read("current");
    const next = read("next");
    setSaved(false);
    const invalid = firstProblem(current, next, read("repeat"));
    setProblem(invalid);
    if (invalid !== null) return;
    change.mutate(
      { current_password: current, new_password: next },
      {
        onSuccess: () => {
          form.reset();
          setNext("");
          setRepeat("");
          setSaved(true);
          onDone?.();
        },
        onError: (error) => {
          setProblem(
            isApiError(error) ? error.message : "Something went wrong. Try again in a moment.",
          );
        },
      },
    );
  };

  return (
    <form className={styles.form} onSubmit={onSubmit} noValidate>
      {problem ? (
        <Callout variant="danger" role="alert">
          {problem}
        </Callout>
      ) : null}
      {saved ? (
        <Callout variant="success" role="status">
          Your password was changed.
        </Callout>
      ) : null}
      <FormControl>
        <FormControl.Label>Current password</FormControl.Label>
        <TextInput name="current" type="password" block autoComplete="current-password" />
      </FormControl>
      <FormControl>
        <FormControl.Label>New password</FormControl.Label>
        <TextInput
          name="next"
          type="password"
          block
          autoComplete="new-password"
          onChange={(event) => {
            setNext(event.target.value);
          }}
        />
        <Hints next={next} repeat={repeat} />
      </FormControl>
      <FormControl>
        <FormControl.Label>Repeat the new password</FormControl.Label>
        <TextInput
          name="repeat"
          type="password"
          block
          autoComplete="new-password"
          onChange={(event) => {
            setRepeat(event.target.value);
          }}
        />
      </FormControl>
      <Button type="submit" variant="primary" block disabled={change.isPending}>
        {change.isPending ? "Saving…" : "Change password"}
      </Button>
    </form>
  );
}

/** The screen a user lands on after an administrator reset their password. */
export function ChangePasswordPage() {
  const navigate = useNavigate();
  const session = useSession();
  const logout = useLogout();
  useDocumentTitle("Choose a new password");
  const forced = session.data?.must_change_password === true;
  return (
    <div className={styles.login}>
      <div className={styles.card}>
        <TerminalIcon size={48} className={styles.logo} aria-hidden="true" />
        <Heading as="h1" className={styles.title}>
          Choose a new password
        </Heading>
        {forced ? (
          <Callout variant="warning">
            An administrator gave you a one-time password. Choose your own before you continue.
          </Callout>
        ) : null}
        <ChangePasswordForm
          onDone={() => {
            void navigate("/projects", { replace: true });
          }}
        />
        <Button
          variant="invisible"
          onClick={() => {
            logout.mutate(undefined, {
              onSettled: () => {
                void navigate("/login", { replace: true });
              },
            });
          }}
        >
          Sign out
        </Button>
      </div>
    </div>
  );
}

/** Settings → Account: change your own password at any time. */
export function AccountPage() {
  return (
    <SettingsSection
      title="Account"
      intro="Change the password you sign in with. Your other browser sessions are signed out."
    >
      <div className={styles.narrow}>
        <ChangePasswordForm />
      </div>
    </SettingsSection>
  );
}
