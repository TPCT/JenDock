import { KeyIcon, ShieldCheckIcon } from "@primer/octicons-react";
import { Button, Dialog, FormControl, Label, TextInput } from "@primer/react";
import { useEffect, useRef, useState } from "react";
import {
  useBeginTwoFactor,
  useDisableTwoFactor,
  useEnableTwoFactor,
  useRegenerateRecoveryCodes,
  useTwoFactorStatus,
} from "../api/accountHooks";
import { isApiError } from "../api/client";
import { CopyField } from "../components/CopyField";
import { Callout, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { QrCode } from "../components/QrCode";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import box from "../styles/box.module.css";

function problemText(error: unknown): string {
  if (isApiError(error) && error.status === 429) {
    return "Too many attempts. Wait a few minutes and try again.";
  }
  if (isApiError(error) && error.status === 422) {
    return error.message.charAt(0).toUpperCase() + error.message.slice(1) + ".";
  }
  return errorMessage(error);
}

function CodeField({
  value,
  onChange,
  label = "Authentication code",
  caption = "The six-digit code from your authenticator app.",
}: {
  value: string;
  onChange: (value: string) => void;
  label?: string;
  caption?: string;
}) {
  return (
    <FormControl required>
      <FormControl.Label>{label}</FormControl.Label>
      <TextInput
        block
        value={value}
        autoComplete="one-time-code"
        inputMode="numeric"
        spellCheck={false}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      <FormControl.Caption>{caption}</FormControl.Caption>
    </FormControl>
  );
}

/** The ten codes, shown once. They are the way back in when the phone is lost. */
function RecoveryCodesDialog({ codes, onClose }: { codes: string[]; onClose: () => void }) {
  return (
    <Dialog
      title="Save your recovery codes"
      width="large"
      onClose={onClose}
      footerButtons={[{ buttonType: "primary", content: "I have saved them", onClick: onClose }]}
    >
      <div className={box.stack}>
        <Callout variant="warning" title="You will not see these codes again">
          Each code signs you in once if you lose access to your authenticator app. Store them
          somewhere safe, such as a password manager.
        </Callout>
        <CopyField label="Recovery codes" value={codes.join("\n")} multiline />
      </div>
    </Dialog>
  );
}

function EnrollDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (codes: string[]) => void;
}) {
  const begin = useBeginTwoFactor();
  const enable = useEnableTwoFactor();
  const [code, setCode] = useState("");
  const started = useRef(false);
  const { mutate: start } = begin;
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    start();
  }, [start]);

  const submit = () => {
    if (code.trim() === "") return;
    enable.mutate(code.trim(), {
      onSuccess: (result) => {
        onDone(result.recovery_codes);
      },
    });
  };

  return (
    <Dialog
      title="Set up two-factor authentication"
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: enable.isPending ? "Verifying…" : "Verify and turn on",
          disabled: begin.data === undefined || enable.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        {begin.isPending ? <Loading label="Preparing your key" /> : null}
        {begin.isError ? (
          <ErrorCallout title="Could not start the setup" error={begin.error} />
        ) : null}
        {begin.data ? (
          <>
            <ol className={box.stack}>
              <li>
                Scan this QR code with an authenticator app such as 1Password, Authy or Google
                Authenticator.
                <div className={box.cluster}>
                  <QrCode value={begin.data.uri} label="QR code for your authenticator app" />
                </div>
              </li>
              <li>
                Cannot scan it? Enter this key by hand.
                <CopyField label="Setup key" value={begin.data.secret} />
              </li>
              <li>Enter the six-digit code the app shows to confirm.</li>
            </ol>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                submit();
              }}
            >
              <CodeField value={code} onChange={setCode} />
            </form>
          </>
        ) : null}
        {enable.isError ? (
          <Callout variant="danger" role="alert">
            {problemText(enable.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function RegenerateDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (codes: string[]) => void;
}) {
  const regenerate = useRegenerateRecoveryCodes();
  const [code, setCode] = useState("");
  const submit = () => {
    if (code.trim() === "") return;
    regenerate.mutate(code.trim(), {
      onSuccess: (result) => {
        onDone(result.recovery_codes);
      },
    });
  };
  return (
    <Dialog
      title="Generate new recovery codes"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: regenerate.isPending ? "Generating…" : "Generate codes",
          disabled: regenerate.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        <p>The codes you have now stop working as soon as new ones are created.</p>
        <CodeField value={code} onChange={setCode} />
        {regenerate.isError ? (
          <Callout variant="danger" role="alert">
            {problemText(regenerate.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function DisableDialog({ onClose }: { onClose: () => void }) {
  const disable = useDisableTwoFactor();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const submit = () => {
    if (password === "" || code.trim() === "") return;
    disable.mutate({ password, code: code.trim() }, { onSuccess: onClose });
  };
  return (
    <Dialog
      title="Turn off two-factor authentication?"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Keep it on", onClick: onClose },
        {
          buttonType: "danger",
          content: disable.isPending ? "Turning off…" : "Turn off",
          disabled: disable.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        <p>Your account will be protected by your password alone.</p>
        <FormControl required>
          <FormControl.Label>Password</FormControl.Label>
          <TextInput
            block
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(event) => {
              setPassword(event.target.value);
            }}
          />
        </FormControl>
        <CodeField
          value={code}
          onChange={setCode}
          caption="A code from your authenticator app, or one recovery code."
        />
        {disable.isError ? (
          <Callout variant="danger" role="alert">
            {problemText(disable.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

type Flow = "enroll" | "regenerate" | "disable" | null;

export function TwoFactorSection() {
  const status = useTwoFactorStatus();
  const [flow, setFlow] = useState<Flow>(null);
  const [codes, setCodes] = useState<string[] | null>(null);

  const finish = (fresh: string[]) => {
    setFlow(null);
    setCodes(fresh);
  };

  return (
    <SettingsSection
      title="Two-factor authentication"
      intro="Ask for a code from an authenticator app after your password. A stolen password alone then cannot sign in."
    >
      {status.isPending ? <Loading label="Loading two-factor status" /> : null}
      {status.isError ? (
        <ErrorCallout
          title="Could not load the two-factor status"
          error={status.error}
          onRetry={() => void status.refetch()}
        />
      ) : null}
      {status.data && !status.data.enabled ? (
        <div className={box.box}>
          <SettingsRow
            title="Authenticator app"
            description={
              <span className={box.cluster}>
                <Label>Off</Label>
                <span>Not set up yet.</span>
              </span>
            }
            action={
              <Button
                variant="primary"
                size="small"
                leadingVisual={ShieldCheckIcon}
                onClick={() => {
                  setFlow("enroll");
                }}
              >
                Set up
              </Button>
            }
          />
        </div>
      ) : null}
      {status.data?.enabled ? (
        <div className={box.box}>
          <ul className={box.list} aria-label="Two-factor authentication">
            <li>
              <SettingsRow
                title="Authenticator app"
                description={
                  <span className={box.cluster}>
                    <Label variant="success">On</Label>
                    {status.data.enabled_at ? (
                      <span>
                        since <RelativeTime iso={status.data.enabled_at} />
                      </span>
                    ) : null}
                  </span>
                }
                action={
                  <Button
                    size="small"
                    variant="danger"
                    onClick={() => {
                      setFlow("disable");
                    }}
                  >
                    Turn off
                  </Button>
                }
              />
            </li>
            <li>
              <SettingsRow
                title="Recovery codes"
                description={
                  <span className={box.cluster}>
                    <Label variant={status.data.recovery_codes_left <= 2 ? "danger" : "secondary"}>
                      {String(status.data.recovery_codes_left)} left
                    </Label>
                    <span>Each one signs you in once without the app.</span>
                  </span>
                }
                action={
                  <Button
                    size="small"
                    leadingVisual={KeyIcon}
                    onClick={() => {
                      setFlow("regenerate");
                    }}
                  >
                    New codes
                  </Button>
                }
              />
            </li>
          </ul>
        </div>
      ) : null}

      {flow === "enroll" ? (
        <EnrollDialog
          onClose={() => {
            setFlow(null);
          }}
          onDone={finish}
        />
      ) : null}
      {flow === "regenerate" ? (
        <RegenerateDialog
          onClose={() => {
            setFlow(null);
          }}
          onDone={finish}
        />
      ) : null}
      {flow === "disable" ? (
        <DisableDialog
          onClose={() => {
            setFlow(null);
          }}
        />
      ) : null}
      {codes ? (
        <RecoveryCodesDialog
          codes={codes}
          onClose={() => {
            setCodes(null);
          }}
        />
      ) : null}
    </SettingsSection>
  );
}
