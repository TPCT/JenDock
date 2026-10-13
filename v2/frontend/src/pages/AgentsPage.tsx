import { CommentDiscussionIcon } from "@primer/octicons-react";
import { Button, FormControl, Label, Radio, RadioGroup, Select, TextInput } from "@primer/react";
import { useState } from "react";
import { useCreateToken } from "../api/adminHooks";
import type { CreatedToken, TokenScope } from "../api/adminTypes";
import { useMcpTools } from "../api/integrationHooks";
import { CopyField } from "../components/CopyField";
import { Callout, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { SettingsSection } from "../components/SettingsSection";
import box from "../styles/box.module.css";
import { SCOPE_HELP } from "./GlobalSettings";
import styles from "./Integrations.module.css";

interface Preset {
  id: string;
  label: string;
  scopes: TokenScope[];
  summary: string;
}

/** Bundles of scopes in the words of what an agent may do; the scopes themselves stay visible. */
export const PRESETS: readonly Preset[] = [
  {
    id: "read",
    label: "Read only",
    scopes: ["read"],
    summary: "Look at projects, runs, logs, pipelines, containers and domains. Changes nothing.",
  },
  {
    id: "run",
    label: "Run pipelines",
    scopes: ["read", "run"],
    summary: "Everything in Read only, plus start, retry and cancel runs.",
  },
  {
    id: "deploy",
    label: "Deploy",
    scopes: ["read", "run", "deploy"],
    summary: "Everything in Run pipelines, plus redeploy an earlier successful deployment.",
  },
  {
    id: "admin",
    label: "Full admin",
    scopes: ["admin"],
    summary:
      "Every tool, including saving pipelines, restarting containers and changing domains. Use sparingly.",
  },
];

const EXPIRY = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "never", label: "Never expires" },
] as const;

const PLACEHOLDER_TOKEN = "<paste your token here>";

export function claudeCodeCommand(url: string, token: string): string {
  return `claude mcp add --transport http jendock ${url} --header "Authorization: Bearer ${token}"`;
}

export function genericConfig(url: string, token: string): string {
  return JSON.stringify(
    {
      mcpServers: {
        jendock: {
          type: "http",
          url,
          headers: { Authorization: `Bearer ${token}` },
        },
      },
    },
    null,
    2,
  );
}

function expiresAt(days: string): string | null {
  return days === "never" ? null : new Date(Date.now() + Number(days) * 86_400_000).toISOString();
}

export function AgentsPage() {
  const [presetId, setPresetId] = useState("read");
  const [name, setName] = useState("Claude agent");
  const [expiry, setExpiry] = useState("90");
  const [problem, setProblem] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const create = useCreateToken();
  const preset = PRESETS.find((item) => item.id === presetId) ?? PRESETS[0];
  const tools = useMcpTools(preset?.scopes ?? []);

  if (!preset) return null;
  const endpoint = tools.data?.endpoint ?? `${window.location.origin}/mcp`;
  const token = created?.token ?? PLACEHOLDER_TOKEN;

  const submit = () => {
    if (name.trim() === "") {
      setProblem("Give the token a name so you can recognise it later.");
      return;
    }
    setProblem(null);
    create.mutate(
      {
        name: name.trim(),
        scopes: preset.scopes,
        expires_at: expiresAt(expiry),
      },
      {
        onSuccess: (result) => {
          setCreated(result);
        },
      },
    );
  };

  return (
    <SettingsSection
      title="Connect an AI agent"
      intro="Let an AI assistant such as Claude start runs, read logs and manage deployments through the Model Context Protocol (MCP). The server is live on this address; an agent signs in with an API token that carries only the permissions you choose."
    >
      {tools.data && !tools.data.enabled ? (
        <Callout variant="warning" title="MCP is switched off on this server">
          Ask the person who runs Jendock to set <code>JENDOCK_MCP_ENABLED=true</code>. Tokens you
          create here will work as soon as it is on.
        </Callout>
      ) : null}

      <div className={`${box.box} ${box.row} ${box.stack}`}>
        <h3 className={styles.h}>1. The address</h3>
        <CopyField label="MCP endpoint" value={endpoint} />
        <p className="muted">
          Agents connect over HTTP and send <code>Authorization: Bearer jdt_…</code> with every
          request. No cookies are used.
        </p>
      </div>

      <form
        className={`${box.box} ${box.row} ${box.stack}`}
        aria-label="Create a token for an agent"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <h3 className={styles.h}>2. Create a token for an agent</h3>
        <FormControl required>
          <FormControl.Label>Name</FormControl.Label>
          <TextInput
            block
            value={name}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
          <FormControl.Caption>
            Shown in the token list and in the audit log, so you can tell who did what.
          </FormControl.Caption>
        </FormControl>
        <RadioGroup
          name="agent-access"
          onChange={(selected) => {
            if (selected) {
              setPresetId(selected);
              setCreated(null);
            }
          }}
        >
          <RadioGroup.Label>What may the agent do?</RadioGroup.Label>
          {PRESETS.map((item) => (
            <FormControl key={item.id}>
              <Radio value={item.id} checked={presetId === item.id} />
              <FormControl.Label>{item.label}</FormControl.Label>
              <FormControl.Caption>{item.summary}</FormControl.Caption>
            </FormControl>
          ))}
        </RadioGroup>
        <div>
          <strong>Permissions in this token</strong>
          <ul className={styles.scopes} aria-label="Permissions in this token">
            {preset.scopes.map((scope) => (
              <li key={scope}>
                <Label>{scope}</Label> <span className="muted">{SCOPE_HELP[scope]}</span>
              </li>
            ))}
          </ul>
        </div>
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
        {created ? (
          <>
            <Callout variant="warning" title="Copy this token now: you will not see it again">
              Jendock keeps only a fingerprint. If you lose it, revoke it under API tokens and
              create another.
            </Callout>
            <CopyField label={`Token ${created.info.name}`} value={created.token} />
          </>
        ) : null}
        <div>
          <Button type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "Creating…" : created ? "Create another token" : "Create token"}
          </Button>
        </div>
      </form>

      <div className={`${box.box} ${box.row} ${box.stack}`}>
        <h3 className={styles.h}>3. Paste it into the agent</h3>
        {created ? null : (
          <p className="muted">
            Create a token above and these snippets fill in with it. Until then{" "}
            <code>{PLACEHOLDER_TOKEN}</code> stands for the token.
          </p>
        )}
        <CopyField
          label="Claude Code command"
          value={claudeCodeCommand(endpoint, token)}
          multiline
        />
        <CopyField
          label="Generic MCP JSON configuration"
          value={genericConfig(endpoint, token)}
          multiline
        />
      </div>

      <div className={`${box.box} ${box.row} ${box.stack}`}>
        <h3 className={styles.h}>
          What “{preset.label}” can use
          {tools.data ? ` (${String(tools.data.tools.length)} tools)` : ""}
        </h3>
        {tools.isPending ? <Loading label="Loading the tools" /> : null}
        {tools.isError ? (
          <ErrorCallout title="Could not load the tool list" error={tools.error} />
        ) : null}
        {tools.data ? (
          <ul className={styles.tools} aria-label="Available tools">
            {tools.data.tools.map((tool) => (
              <li key={tool.name}>
                <code>{tool.name}</code>{" "}
                <Label variant={tool.read_only ? "secondary" : "attention"}>
                  {tool.read_only ? "reads" : "changes things"}
                </Label>
                <div className="muted">{tool.description}</div>
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      <div className={`${box.box} ${box.row} ${box.stack}`}>
        <h3 className={styles.h}>What agents can never do</h3>
        <ul className={styles.limits} aria-label="Limits of agent access">
          <li>
            <strong>No secrets.</strong> An agent can see the <em>names</em> of an
            environment&apos;s secrets, never their values, and cannot create or change them.
          </li>
          <li>
            <strong>No terminal.</strong> There is no tool that opens a shell in a container or runs
            a command on the server, whatever permissions the token has.
          </li>
          <li>
            <strong>No token management.</strong> Only you, signed in with the browser, can create
            or revoke tokens.
          </li>
          <li>
            <strong>Two steps for anything that changes something.</strong> Starting a run, a
            redeploy, a container action or a domain change is previewed first and then confirmed.
          </li>
          <li>
            <strong>Everything is recorded.</strong> Each call is written to the audit log under
            that token.
          </li>
        </ul>
        <p className="muted">
          <CommentDiscussionIcon size={14} aria-hidden="true" /> Revoke a token under API tokens to
          cut an agent off at once.
        </p>
      </div>
    </SettingsSection>
  );
}
