import { CommentDiscussionIcon, KeyIcon, PlusIcon } from "@primer/octicons-react";
import {
  Button,
  Checkbox,
  CheckboxGroup,
  ConfirmationDialog,
  Dialog,
  FormControl,
  Label,
  Select,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import { useCreateToken, useRevokeToken, useTokens } from "../api/adminHooks";
import {
  TOKEN_SCOPES,
  type CreatedToken,
  type TokenInfo,
  type TokenScope,
} from "../api/adminTypes";
import { CopyField } from "../components/CopyField";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import { SettingsLayout, type SettingsItem } from "../components/SettingsLayout";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import { formatDateTime } from "../lib/format";
import box from "../styles/box.module.css";

export const SCOPE_HELP: Record<TokenScope, string> = {
  read: "See projects, runs, logs and pipelines.",
  run: "Start, retry and cancel runs.",
  deploy: "Deploy and roll back services.",
  containers: "See and manage running containers.",
  terminal: "Open a terminal inside a container.",
  pipelines: "Save pipelines.",
  admin: "Everything, including secrets and domains. Use sparingly.",
};

const EXPIRY = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "never", label: "Never expires" },
] as const;

export function GlobalSettingsLayout() {
  const items: SettingsItem[] = [
    { to: "/settings/tokens", label: "API tokens", icon: KeyIcon },
    { to: "/settings/agents", label: "Connect an AI agent", icon: CommentDiscussionIcon },
  ];
  return (
    <>
      <PageHead
        title="Settings"
        description="Your access to Shiftcodes from scripts and AI agents."
      />
      <div className={box.section}>
        <SettingsLayout label="Settings" items={items} />
      </div>
    </>
  );
}

function tokenState(token: TokenInfo): {
  label: string;
  variant: "success" | "secondary" | "danger";
} {
  if (token.revoked_at) return { label: "Revoked", variant: "secondary" };
  if (token.expires_at && Date.parse(token.expires_at) < Date.now()) {
    return { label: "Expired", variant: "danger" };
  }
  return { label: "Active", variant: "success" };
}

function CreateTokenDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: CreatedToken) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<TokenScope[]>(["read"]);
  const [expiry, setExpiry] = useState<string>("90");
  const [problem, setProblem] = useState<string | null>(null);
  const create = useCreateToken();
  const submit = () => {
    if (name.trim() === "") {
      setProblem("Give the token a name so you can recognise it later.");
      return;
    }
    if (scopes.length === 0) {
      setProblem("Choose at least one permission.");
      return;
    }
    setProblem(null);
    create.mutate(
      {
        name: name.trim(),
        scopes,
        expires_at:
          expiry === "never"
            ? null
            : new Date(Date.now() + Number(expiry) * 86_400_000).toISOString(),
      },
      { onSuccess: onCreated },
    );
  };
  return (
    <Dialog
      title="New API token"
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: create.isPending ? "Creating…" : "Create token",
          disabled: create.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        <FormControl required>
          <FormControl.Label>Name</FormControl.Label>
          <TextInput
            block
            value={name}
            placeholder="CI deploy"
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <FormControl.Caption>
            What or who will use it, for example &quot;GitHub Actions&quot; or &quot;Claude
            agent&quot;.
          </FormControl.Caption>
        </FormControl>
        <CheckboxGroup
          onChange={(selected) => {
            setScopes(selected as TokenScope[]);
          }}
        >
          <CheckboxGroup.Label>Permissions</CheckboxGroup.Label>
          {TOKEN_SCOPES.map((scope) => (
            <FormControl key={scope}>
              <Checkbox value={scope} checked={scopes.includes(scope)} />
              <FormControl.Label>{scope}</FormControl.Label>
              <FormControl.Caption>{SCOPE_HELP[scope]}</FormControl.Caption>
            </FormControl>
          ))}
        </CheckboxGroup>
        <FormControl>
          <FormControl.Label>Expiration</FormControl.Label>
          <Select
            block
            value={expiry}
            onChange={(event) => {
              setExpiry(event.target.value);
            }}
          >
            {EXPIRY.map((option) => (
              <Select.Option key={option.value} value={option.value}>
                {option.label}
              </Select.Option>
            ))}
          </Select>
          <FormControl.Caption>
            A token that expires limits the damage if it ever leaks.
          </FormControl.Caption>
        </FormControl>
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

export function TokensPage() {
  const tokens = useTokens();
  const revoke = useRevokeToken();
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const [revoking, setRevoking] = useState<TokenInfo | null>(null);

  return (
    <SettingsSection
      title="API tokens"
      intro="Tokens let scripts and AI agents use the API with limited permissions. They can only be managed here, from a signed-in browser."
      actions={
        <Button
          variant="primary"
          size="small"
          leadingVisual={PlusIcon}
          onClick={() => {
            setCreating(true);
          }}
        >
          New token
        </Button>
      }
    >
      {tokens.isPending ? <Loading label="Loading tokens" /> : null}
      {tokens.isError ? (
        <ErrorCallout
          title="Could not load the tokens"
          error={tokens.error}
          onRetry={() => void tokens.refetch()}
        />
      ) : null}
      {tokens.data?.length === 0 ? (
        <EmptyState icon={<KeyIcon size={32} aria-hidden="true" />} title="No tokens yet">
          Create a token when a script or an AI agent needs to call Shiftcodes.
        </EmptyState>
      ) : null}
      {tokens.data && tokens.data.length > 0 ? (
        <div className={box.box}>
          <ul className={box.list} aria-label="API tokens">
            {tokens.data.map((token) => {
              const state = tokenState(token);
              return (
                <li key={token.id}>
                  <SettingsRow
                    title={token.name}
                    description={
                      <div className={box.stack}>
                        <span>
                          <code>{token.prefix}…</code> · created{" "}
                          <RelativeTime iso={token.created_at} /> ·{" "}
                          {token.last_used_at ? (
                            <>
                              last used <RelativeTime iso={token.last_used_at} />
                            </>
                          ) : (
                            "never used"
                          )}{" "}
                          ·{" "}
                          {token.expires_at ? (
                            <>expires {formatDateTime(token.expires_at)}</>
                          ) : (
                            "does not expire"
                          )}
                        </span>
                        <span className={box.cluster}>
                          <Label variant={state.variant}>{state.label}</Label>
                          {token.scopes.map((scope) => (
                            <Label key={scope}>{scope}</Label>
                          ))}
                        </span>
                      </div>
                    }
                    action={
                      token.revoked_at ? null : (
                        <Button
                          size="small"
                          variant="danger"
                          aria-label={`Revoke ${token.name}`}
                          onClick={() => {
                            setRevoking(token);
                          }}
                        >
                          Revoke
                        </Button>
                      )
                    }
                  />
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {creating ? (
        <CreateTokenDialog
          onClose={() => {
            setCreating(false);
          }}
          onCreated={(result) => {
            setCreating(false);
            setCreated(result);
          }}
        />
      ) : null}

      {created ? (
        <Dialog
          title="Copy your new token"
          width="large"
          onClose={() => {
            setCreated(null);
          }}
          footerButtons={[
            {
              buttonType: "primary",
              content: "I have copied it",
              onClick: () => {
                setCreated(null);
              },
            },
          ]}
        >
          <div className={box.stack}>
            <Callout variant="warning" title="This is the only time you will see it">
              Copy the token now and store it somewhere safe, such as a password manager. Shiftcodes
              keeps only a fingerprint, so a lost token cannot be shown again; create a new one
              instead.
            </Callout>
            <CopyField label={`Token ${created.info.name}`} value={created.token} />
          </div>
        </Dialog>
      ) : null}

      {revoking ? (
        <ConfirmationDialog
          title={`Revoke ${revoking.name}?`}
          confirmButtonContent="Revoke token"
          cancelButtonContent="Keep it"
          confirmButtonType="danger"
          confirmButtonLoading={revoke.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") {
              revoke.mutate(revoking.id, {
                onSuccess: () => {
                  setRevoking(null);
                },
              });
            } else setRevoking(null);
          }}
        >
          <p>Anything using this token stops working at once. This cannot be undone.</p>
          {revoke.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(revoke.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </SettingsSection>
  );
}

export function AgentsPage() {
  return (
    <SettingsSection
      title="Connect an AI agent"
      intro="Let an AI assistant such as Claude start runs, read logs and manage deployments through the Model Context Protocol (MCP)."
    >
      <EmptyState
        icon={<CommentDiscussionIcon size={32} aria-hidden="true" />}
        title="MCP access is coming soon"
      >
        Shiftcodes will offer an MCP server at <code>/mcp</code> on this address. When it is ready
        you will create an API token here with only the permissions the agent needs, then paste the
        address and the token into the agent&apos;s settings. The server is not available yet.
      </EmptyState>
    </SettingsSection>
  );
}
