import { AlertIcon, SignInIcon } from "@primer/octicons-react";
import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { isApiError } from "../api/client";
import { useLogin } from "../api/hooks";
import { safeNext } from "../lib/next";
import "./Login.css";

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

  useEffect(() => {
    document.title = "Sign in · Shiftcodes";
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
        onSuccess: () => {
          void navigate(safeNext(params.get("next")), { replace: true });
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
    <div className="login">
      <div className="login-card">
        <h1>Sign in to Shiftcodes</h1>
        {formError ? (
          <div className="callout callout-danger" role="alert">
            <AlertIcon size={16} aria-hidden="true" />
            <div className="callout-body">{formError}</div>
          </div>
        ) : null}
        <form className="box login-form" onSubmit={onSubmit} noValidate>
          <div className="field">
            <label htmlFor="username">Username</label>
            <input
              ref={usernameRef}
              id="username"
              name="username"
              className="input"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              aria-invalid={fieldErrors.username ? true : undefined}
              aria-describedby={fieldErrors.username ? "username-error" : undefined}
            />
            {fieldErrors.username ? (
              <p id="username-error" className="field-error">
                <AlertIcon size={14} aria-hidden="true" />
                {fieldErrors.username}
              </p>
            ) : null}
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              ref={passwordRef}
              id="password"
              name="password"
              type="password"
              className="input"
              autoComplete="current-password"
              aria-invalid={fieldErrors.password ? true : undefined}
              aria-describedby={fieldErrors.password ? "password-error" : undefined}
            />
            {fieldErrors.password ? (
              <p id="password-error" className="field-error">
                <AlertIcon size={14} aria-hidden="true" />
                {fieldErrors.password}
              </p>
            ) : null}
          </div>
          <button type="submit" className="btn btn-primary" disabled={login.isPending}>
            <SignInIcon size={16} aria-hidden="true" />
            {login.isPending ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="muted">
          No account yet? An administrator creates accounts; ask them for a username.
        </p>
      </div>
    </div>
  );
}
