import { TerminalIcon } from "@primer/octicons-react";
import { Button, FormControl, Heading, TextInput } from "@primer/react";
import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { isApiError, isMfaRequired } from "../api/client";
import { useLogin, useLoginTwoFactor } from "../api/hooks";
import type { Session } from "../api/types";
import { safeNext } from "../lib/next";
import { Callout } from "../components/Feedback";
import { useDocumentTitle } from "../components/PageHead";
import styles from "./Login.module.css";

type FieldName = "username" | "password";
type FieldErrors = Partial<Record<FieldName, string>>;

function retryText(seconds: number | null): string {
  if (seconds === null) return "Wait a few minutes and try again.";
  const retryAt = new Date(Date.now() + seconds * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (seconds < 90) return `Try again in ${seconds} seconds (at ${retryAt}).`;
  const minutes = Math.ceil(seconds / 60);
  return `Try again in ${minutes} minutes (at ${retryAt}).`;
}

function PasswordStep({
  onChallenge,
  onSignedIn,
}: {
  onChallenge: (challenge: string) => void;
  onSignedIn: (session: Session) => void;
}) {
  const login = useLogin();
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    usernameRef.current?.focus();
  }, []);

  const focusFirstInvalid = (errors: FieldErrors) => {
    if (errors.username) usernameRef.current?.focus();
    else if (errors.password) passwordRef.current?.focus();
  };

  const onSubmit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const read = (name: string): string => {
      const value = form.get(name);
      return typeof value === "string" ? value : "";
    };
    const username = read("username").trim();
    const password = read("password");
    const errors: FieldErrors = {};
    if (username === "") errors.username = "Enter your username.";
    if (password === "") errors.password = "Enter your password.";
    setFormError(null);
    setFieldErrors(errors);
    if (errors.username || errors.password) {
      focusFirstInvalid(errors);
      return;
    }
    login.mutate(
      { username, password },
      {
        onSuccess: (outcome) => {
          if (isMfaRequired(outcome)) onChallenge(outcome.challenge);
          else onSignedIn(outcome);
        },
        onError: (error) => {
          if (!isApiError(error)) {
            setFormError("Something went wrong. Try again.");
            return;
          }
          if (error.status === 429) {
            setFormError(`Too many sign-in attempts. ${retryText(error.retryAfter)}`);
          } else if (error.status === 401) {
            setFormError("The username or password is incorrect.");
            passwordRef.current?.focus();
          } else if (error.status === 422 && error.fields.length > 0) {
            const mapped: FieldErrors = {};
            for (const issue of error.fields) {
              const name = issue.loc[issue.loc.length - 1];
              if (name === "username" || name === "password") mapped[name] = issue.msg;
            }
            setFieldErrors(mapped);
            focusFirstInvalid(mapped);
            if (!mapped.username && !mapped.password) setFormError(error.message);
          } else if (error.status === 0) {
            setFormError(error.message);
          } else {
            setFormError("Sign-in failed. Try again, or contact your administrator.");
          }
        },
      },
    );
  };

  return (
    <>
      {formError ? (
        <Callout variant="danger" role="alert">
          {formError}
        </Callout>
      ) : null}
      <form className={styles.form} onSubmit={onSubmit} noValidate>
        <FormControl>
          <FormControl.Label>Username</FormControl.Label>
          <TextInput
            ref={usernameRef}
            name="username"
            block
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            validationStatus={fieldErrors.username ? "error" : undefined}
          />
          {fieldErrors.username ? (
            <FormControl.Validation variant="error">{fieldErrors.username}</FormControl.Validation>
          ) : null}
        </FormControl>
        <FormControl>
          <FormControl.Label>Password</FormControl.Label>
          <TextInput
            ref={passwordRef}
            name="password"
            type="password"
            block
            autoComplete="current-password"
            validationStatus={fieldErrors.password ? "error" : undefined}
          />
          {fieldErrors.password ? (
            <FormControl.Validation variant="error">{fieldErrors.password}</FormControl.Validation>
          ) : null}
        </FormControl>
        <Button type="submit" variant="primary" block disabled={login.isPending}>
          {login.isPending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </>
  );
}

function retryMessage(error: unknown): string | null {
  return isApiError(error) && error.status === 429
    ? `Too many attempts. ${retryText(error.retryAfter)}`
    : null;
}

function CodeStep({
  challenge,
  onSignedIn,
  onBack,
}: {
  challenge: string;
  onSignedIn: (session: Session) => void;
  onBack: () => void;
}) {
  const verify = useLoginTwoFactor();
  const [recovery, setRecovery] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const codeRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    codeRef.current?.focus();
  }, [recovery]);

  const onSubmit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    const raw = new FormData(event.currentTarget).get("code");
    const code = (typeof raw === "string" ? raw : "").trim();
    if (code === "") {
      setProblem(recovery ? "Enter one of your recovery codes." : "Enter the six-digit code.");
      codeRef.current?.focus();
      return;
    }
    setProblem(null);
    verify.mutate(
      { challenge, code },
      {
        onSuccess: onSignedIn,
        onError: (error) => {
          const limited = retryMessage(error);
          if (limited) setProblem(limited);
          else if (isApiError(error) && error.status === 401) {
            setProblem(
              "That code did not work, or this sign-in expired. Check the code and try again, or go back and sign in again.",
            );
          } else setProblem(isApiError(error) ? error.message : "Something went wrong. Try again.");
          codeRef.current?.focus();
        },
      },
    );
  };

  return (
    <>
      {problem ? (
        <Callout variant="danger" role="alert">
          {problem}
        </Callout>
      ) : null}
      <form className={styles.form} onSubmit={onSubmit} noValidate>
        <FormControl>
          <FormControl.Label>
            {recovery ? "Recovery code" : "Authentication code"}
          </FormControl.Label>
          <TextInput
            ref={codeRef}
            name="code"
            block
            autoComplete="one-time-code"
            inputMode={recovery ? "text" : "numeric"}
            autoCapitalize="none"
            spellCheck={false}
            key={recovery ? "recovery" : "totp"}
          />
          <FormControl.Caption>
            {recovery
              ? "Each recovery code works once."
              : "Open your authenticator app and enter the six-digit code for Jendock."}
          </FormControl.Caption>
        </FormControl>
        <Button type="submit" variant="primary" block disabled={verify.isPending}>
          {verify.isPending ? "Verifying…" : "Verify"}
        </Button>
      </form>
      <div className={styles.alternatives}>
        <Button
          variant="invisible"
          size="small"
          onClick={() => {
            setRecovery((value) => !value);
            setProblem(null);
          }}
        >
          {recovery ? "Use your authenticator app" : "Use a recovery code"}
        </Button>
        <Button variant="invisible" size="small" onClick={onBack}>
          Back to sign in
        </Button>
      </div>
    </>
  );
}

export function LoginPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [challenge, setChallenge] = useState<string | null>(null);
  useDocumentTitle(challenge ? "Two-factor authentication" : "Sign in");
  const done = (session: Session) => {
    // After an administrator reset the one-time password must be replaced first.
    const target = session.must_change_password ? "/change-password" : safeNext(params.get("next"));
    void navigate(target, { replace: true });
  };
  return (
    <div className={styles.login}>
      <div className={styles.card}>
        <TerminalIcon size={48} className={styles.logo} aria-hidden="true" />
        <Heading as="h1" className={styles.title}>
          {challenge ? "Two-factor authentication" : "Sign in to Jendock"}
        </Heading>
        {challenge ? (
          <CodeStep
            challenge={challenge}
            onSignedIn={done}
            onBack={() => {
              setChallenge(null);
            }}
          />
        ) : (
          <PasswordStep onChallenge={setChallenge} onSignedIn={done} />
        )}
      </div>
    </div>
  );
}
