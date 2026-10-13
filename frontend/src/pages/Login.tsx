import { TerminalIcon } from "@primer/octicons-react";
import { Button, FormControl, Heading, TextInput } from "@primer/react";
import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { useLogin } from "../api/hooks";
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

export function LoginPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const login = useLogin();
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  useDocumentTitle("Sign in");

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
        onSuccess: (session) => {
          // After an administrator reset the one-time password must be replaced first.
          const target = session.must_change_password
            ? "/change-password"
            : safeNext(params.get("next"));
          void navigate(target, { replace: true });
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
    <div className={styles.login}>
      <div className={styles.card}>
        <TerminalIcon size={48} className={styles.logo} aria-hidden="true" />
        <Heading as="h1" className={styles.title}>
          Sign in to Jendock
        </Heading>
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
              <FormControl.Validation variant="error">
                {fieldErrors.username}
              </FormControl.Validation>
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
              <FormControl.Validation variant="error">
                {fieldErrors.password}
              </FormControl.Validation>
            ) : null}
          </FormControl>
          <Button type="submit" variant="primary" block disabled={login.isPending}>
            {login.isPending ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </div>
    </div>
  );
}
