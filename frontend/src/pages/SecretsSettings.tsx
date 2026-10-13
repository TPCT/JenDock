import { KeyIcon, PlusIcon } from "@primer/octicons-react";
import {
  Button,
  ConfirmationDialog,
  Dialog,
  FormControl,
  SegmentedControl,
  Textarea,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useDeleteSecret, useRevealSecret, useSecrets, useSetSecret } from "../api/adminHooks";
import type { SecretInfo } from "../api/adminTypes";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { CopyField } from "../components/CopyField";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";

const NAME = /^[A-Z_][A-Z0-9_]{0,127}$/;

function SecretDialog({
  slug,
  env,
  existing,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  existing: string | null;
  onClose: () => void;
}) {
  const [name, setName] = useState(existing ?? "");
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const set = useSetSecret(slug, env);
  const save = () => {
    if (!NAME.test(name)) {
      setProblem("Use capital letters, digits and underscores, for example DB_PASSWORD.");
      return;
    }
    if (value === "") {
      setProblem("Enter the value.");
      return;
    }
    setProblem(null);
    set.mutate({ name, value }, { onSuccess: onClose });
  };
  return (
    <Dialog
      title={existing ? `Rotate ${existing}` : `New ${capitalize(env)} secret`}
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: set.isPending ? "Saving…" : existing ? "Save new value" : "Add secret",
          disabled: set.isPending,
          onClick: save,
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
            disabled={existing !== null}
            autoCapitalize="characters"
            spellCheck={false}
            validationStatus={problem && !NAME.test(name) ? "error" : undefined}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <FormControl.Caption>
            Pipelines refer to it as <code>credentials(&apos;{name || "NAME"}&apos;)</code>.
          </FormControl.Caption>
        </FormControl>
        <FormControl required>
          <FormControl.Label>Value</FormControl.Label>
          <Textarea
            block
            rows={4}
            className="mono"
            value={value}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => {
              setValue(event.target.value);
            }}
          />
          <FormControl.Caption>
            Stored encrypted; only the build worker can read it. Logs mask it. You cannot see it
            again except through the audited Reveal action.
          </FormControl.Caption>
        </FormControl>
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

function RevealFlow({
  slug,
  env,
  name,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  name: string;
  onClose: () => void;
}) {
  const reveal = useRevealSecret(slug, env);
  const revealed = reveal.data;
  if (revealed) {
    return (
      <Dialog
        title={`${name} (revealed)`}
        width="large"
        onClose={onClose}
        footerButtons={[{ buttonType: "primary", content: "Hide and close", onClick: onClose }]}
      >
        <div className={box.stack}>
          <Callout variant="warning">
            This view was recorded in the audit log. Close it when you are done; the value is not
            kept in the page.
          </Callout>
          <CopyField label={`Value of ${name}`} value={revealed.value} multiline />
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
        Showing a secret is recorded in the audit log with your name and the time. Reveal it only if
        you need the value, and do not paste it into chats or tickets.
      </p>
      {reveal.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(reveal.error)}
        </Callout>
      ) : null}
    </ConfirmationDialog>
  );
}

function DeleteFlow({
  slug,
  env,
  name,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  name: string;
  onClose: () => void;
}) {
  const remove = useDeleteSecret(slug, env);
  return (
    <ConfirmationDialog
      title={`Delete ${name}?`}
      confirmButtonContent="Delete secret"
      cancelButtonContent="Keep it"
      confirmButtonType="danger"
      confirmButtonLoading={remove.isPending}
      onClose={(gesture) => {
        if (gesture === "confirm") remove.mutate(name, { onSuccess: onClose });
        else onClose();
      }}
    >
      <p>
        Pipelines that use <code>credentials(&apos;{name}&apos;)</code> stop validating and their
        next run fails. This cannot be undone.
      </p>
      {remove.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(remove.error)}
        </Callout>
      ) : null}
    </ConfirmationDialog>
  );
}

type Action =
  | { kind: "set"; name: string | null }
  | { kind: "reveal"; name: string }
  | { kind: "delete"; name: string };

export function SecretsSettingsPage() {
  const { slug = "" } = useParams();
  const [env, setEnv] = useState<EnvironmentName>("development");
  const [action, setAction] = useState<Action | null>(null);
  const secrets = useSecrets(slug, env);
  const close = () => {
    setAction(null);
  };

  return (
    <SettingsSection
      title="Secrets"
      intro="Passwords, tokens and keys that pipelines need. Each environment has its own set. Only names are shown here."
      actions={
        <Button
          variant="primary"
          size="small"
          leadingVisual={PlusIcon}
          onClick={() => {
            setAction({ kind: "set", name: null });
          }}
        >
          New secret
        </Button>
      }
    >
      <SegmentedControl
        aria-label="Environment"
        onChange={(index) => {
          const next = ENVIRONMENTS[index];
          if (next) setEnv(next);
        }}
      >
        {ENVIRONMENTS.map((name) => (
          <SegmentedControl.Button key={name} selected={name === env}>
            {capitalize(name)}
          </SegmentedControl.Button>
        ))}
      </SegmentedControl>
      {secrets.isPending ? <Loading label="Loading secrets" /> : null}
      {secrets.isError ? (
        <ErrorCallout
          title="Could not load the secrets"
          error={secrets.error}
          onRetry={() => void secrets.refetch()}
        />
      ) : null}
      {secrets.data?.length === 0 ? (
        <EmptyState icon={<KeyIcon size={32} aria-hidden="true" />} title="No secrets yet">
          Add a secret when a pipeline needs a password or token, then use it with{" "}
          <code>credentials(&apos;NAME&apos;)</code>.
        </EmptyState>
      ) : null}
      {secrets.data && secrets.data.length > 0 ? (
        <div className={box.box}>
          <ul className={box.list} aria-label={`${capitalize(env)} secrets`}>
            {secrets.data.map((secret: SecretInfo) => (
              <li key={secret.name}>
                <SettingsRow
                  title={secret.name}
                  description={
                    <>
                      Updated <RelativeTime iso={secret.updated_at} />
                    </>
                  }
                  action={
                    <div className={box.cluster}>
                      <Button
                        size="small"
                        aria-label={`Rotate ${secret.name}`}
                        onClick={() => {
                          setAction({ kind: "set", name: secret.name });
                        }}
                      >
                        Rotate
                      </Button>
                      <Button
                        size="small"
                        aria-label={`Reveal ${secret.name}`}
                        onClick={() => {
                          setAction({ kind: "reveal", name: secret.name });
                        }}
                      >
                        Reveal
                      </Button>
                      <Button
                        size="small"
                        variant="danger"
                        aria-label={`Delete ${secret.name}`}
                        onClick={() => {
                          setAction({ kind: "delete", name: secret.name });
                        }}
                      >
                        Delete
                      </Button>
                    </div>
                  }
                />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {action?.kind === "set" ? (
        <SecretDialog slug={slug} env={env} existing={action.name} onClose={close} />
      ) : null}
      {action?.kind === "reveal" ? (
        <RevealFlow slug={slug} env={env} name={action.name} onClose={close} />
      ) : null}
      {action?.kind === "delete" ? (
        <DeleteFlow slug={slug} env={env} name={action.name} onClose={close} />
      ) : null}
    </SettingsSection>
  );
}
