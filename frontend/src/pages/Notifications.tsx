import { BellIcon, PlusIcon } from "@primer/octicons-react";
import {
  Button,
  ConfirmationDialog,
  Dialog,
  FormControl,
  Label,
  Select,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import {
  useChannels,
  useCreateChannel,
  useDeleteChannel,
  useNotificationPolicy,
  useRules,
  useTestChannel,
  useUpdateChannel,
} from "../api/notificationHooks";
import type {
  ChannelConfig,
  ChannelKind,
  NotificationChannel,
  TestResult,
} from "../api/notificationTypes";
import { DeliveryHistory } from "../components/DeliveryHistory";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { RuleEditor } from "../components/RuleEditor";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import box from "../styles/box.module.css";

export const KIND_LABEL: Record<ChannelKind, string> = {
  slack: "Slack",
  webhook: "Webhook",
  email: "Email",
};

const KIND_HELP: Record<ChannelKind, string> = {
  slack: "Posts to a Slack incoming webhook.",
  webhook:
    "Posts JSON to any URL. The body also carries text and content fields, so Microsoft Teams and Discord webhooks can show it.",
  email: "Sends mail through your SMTP server.",
};

interface Fields {
  webhookUrl: string;
  url: string;
  signingSecret: string;
  host: string;
  port: string;
  security: "none" | "starttls" | "tls";
  username: string;
  password: string;
  from: string;
  to: string;
}

const EMPTY: Fields = {
  webhookUrl: "",
  url: "",
  signingSecret: "",
  host: "",
  port: "587",
  security: "starttls",
  username: "",
  password: "",
  from: "",
  to: "",
};

/** Only the fields the user filled in: a blank secret means "keep what is stored". */
function configOf(kind: ChannelKind, f: Fields, editing: boolean): ChannelConfig {
  const config: ChannelConfig = {};
  const put = (key: string, value: string) => {
    if (value.trim() !== "") config[key] = value.trim();
  };
  if (kind === "slack") put("webhook_url", f.webhookUrl);
  if (kind === "webhook") {
    put("url", f.url);
    put("signing_secret", f.signingSecret);
  }
  if (kind === "email") {
    put("host", f.host);
    if (f.port.trim() !== "") config.port = Number(f.port);
    config.security = f.security;
    put("from_address", f.from);
    const to = f.to
      .split(/[\s,;]+/)
      .map((item) => item.trim())
      .filter((item) => item !== "");
    if (to.length > 0 || !editing) config.to_addresses = to;
    put("username", f.username);
    put("password", f.password);
  }
  return config;
}

function validate(kind: ChannelKind, f: Fields, editing: boolean): string | null {
  if (editing) return null; // the server validates the merged configuration
  if (kind === "slack" && !f.webhookUrl.trim().startsWith("https://")) {
    return "The Slack webhook URL must start with https://.";
  }
  if (kind === "webhook" && !/^https?:\/\//.test(f.url.trim())) {
    return "Enter the URL to post to, starting with http:// or https://.";
  }
  if (kind === "email") {
    if (f.host.trim() === "") return "Enter the SMTP host.";
    if (!f.from.includes("@")) return "Enter the address mail is sent from.";
    if (f.to.trim() === "") return "Enter at least one recipient.";
    if (f.username.trim() !== "" && f.password === "") return "Enter the SMTP password.";
  }
  return null;
}

function SecretField({
  label,
  value,
  onChange,
  keep,
  placeholder,
  required = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  keep: string | null;
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <FormControl required={required && keep === null}>
      <FormControl.Label>{label}</FormControl.Label>
      <TextInput
        block
        type="password"
        monospace
        autoComplete="off"
        spellCheck={false}
        value={value}
        placeholder={placeholder}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
      <FormControl.Caption>
        {keep === null
          ? "Stored encrypted by the worker. Jendock never shows it again."
          : `Leave blank to keep the stored value${keep ? ` (ends in ${keep})` : ""}.`}
      </FormControl.Caption>
    </FormControl>
  );
}

function ChannelDialog({
  existing,
  onClose,
}: {
  existing: NotificationChannel | null;
  onClose: () => void;
}) {
  const editing = existing !== null;
  const [name, setName] = useState(existing?.name ?? "");
  const [kind, setKind] = useState<ChannelKind>(existing?.kind ?? "slack");
  const [fields, setFields] = useState<Fields>(() => ({
    ...EMPTY,
    host: existing?.summary.host ?? "",
    port: String(existing?.summary.port ?? 587),
    security: existing?.summary.security ?? "starttls",
    from: existing?.summary.from_address ?? "",
    to: existing?.summary.to_addresses?.join(", ") ?? "",
  }));
  const [problem, setProblem] = useState<string | null>(null);
  const create = useCreateChannel();
  const update = useUpdateChannel(existing?.id ?? 0);
  const mutation = editing ? update : create;
  const set = (key: keyof Fields) => (value: string) => {
    setFields((current) => ({ ...current, [key]: value }));
  };
  const save = () => {
    if (name.trim() === "") {
      setProblem("Give the channel a name, for example #deploys.");
      return;
    }
    const invalid = validate(kind, fields, editing);
    if (invalid) {
      setProblem(invalid);
      return;
    }
    setProblem(null);
    const config = configOf(kind, fields, editing);
    if (existing) {
      update.mutate(
        { name: name.trim(), ...(Object.keys(config).length > 0 ? { config } : {}) },
        { onSuccess: onClose },
      );
    } else {
      create.mutate({ name: name.trim(), kind, config }, { onSuccess: onClose });
    }
  };
  const keep = existing ? (existing.hint ?? "") : null;
  return (
    <Dialog
      title={editing ? `Edit ${existing.name}` : "New notification channel"}
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: mutation.isPending ? "Saving…" : editing ? "Save changes" : "Add channel",
          disabled: mutation.isPending,
          onClick: save,
        },
      ]}
    >
      <div className={box.stack}>
        <FormControl required>
          <FormControl.Label>Name</FormControl.Label>
          <TextInput
            block
            value={name}
            placeholder="#deploys"
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </FormControl>
        <FormControl>
          <FormControl.Label>Type</FormControl.Label>
          <Select
            block
            value={kind}
            disabled={editing}
            onChange={(event) => {
              setKind(event.target.value as ChannelKind);
            }}
          >
            {(Object.keys(KIND_LABEL) as ChannelKind[]).map((option) => (
              <Select.Option key={option} value={option}>
                {KIND_LABEL[option]}
              </Select.Option>
            ))}
          </Select>
          <FormControl.Caption>{KIND_HELP[kind]}</FormControl.Caption>
        </FormControl>
        {kind === "slack" ? (
          <SecretField
            label="Webhook URL"
            required
            value={fields.webhookUrl}
            onChange={set("webhookUrl")}
            keep={keep}
            placeholder="https://hooks.slack.com/services/…"
          />
        ) : null}
        {kind === "webhook" ? (
          <>
            <SecretField
              label="Webhook URL"
              required
              value={fields.url}
              onChange={set("url")}
              keep={keep}
              placeholder="https://example.com/hooks/jendock"
            />
            <SecretField
              label="Signing secret (optional)"
              value={fields.signingSecret}
              onChange={set("signingSecret")}
              keep={existing?.summary.signed ? "" : null}
            />
            <FormControl.Caption>
              With a signing secret every request carries an{" "}
              <code>X-Jendock-Signature: sha256=…</code> header, the HMAC-SHA256 of the body.
            </FormControl.Caption>
          </>
        ) : null}
        {kind === "email" ? (
          <>
            <FormControl required>
              <FormControl.Label>SMTP host</FormControl.Label>
              <TextInput
                block
                value={fields.host}
                placeholder="smtp.example.com"
                onChange={(event) => {
                  set("host")(event.target.value);
                }}
              />
            </FormControl>
            <div className={box.cluster}>
              <FormControl>
                <FormControl.Label>Port</FormControl.Label>
                <TextInput
                  inputMode="numeric"
                  value={fields.port}
                  onChange={(event) => {
                    set("port")(event.target.value);
                  }}
                />
              </FormControl>
              <FormControl>
                <FormControl.Label>Security</FormControl.Label>
                <Select
                  value={fields.security}
                  onChange={(event) => {
                    set("security")(event.target.value);
                  }}
                >
                  <Select.Option value="starttls">STARTTLS</Select.Option>
                  <Select.Option value="tls">TLS</Select.Option>
                  <Select.Option value="none">None</Select.Option>
                </Select>
              </FormControl>
            </div>
            <FormControl>
              <FormControl.Label>SMTP username (optional)</FormControl.Label>
              <TextInput
                block
                autoComplete="off"
                value={fields.username}
                placeholder={existing?.summary.authenticated ? "Leave blank to keep it" : ""}
                onChange={(event) => {
                  set("username")(event.target.value);
                }}
              />
            </FormControl>
            <SecretField
              label="SMTP password"
              value={fields.password}
              onChange={set("password")}
              keep={existing?.summary.authenticated ? "" : null}
            />
            <FormControl required>
              <FormControl.Label>From</FormControl.Label>
              <TextInput
                block
                value={fields.from}
                placeholder="jendock@example.com"
                onChange={(event) => {
                  set("from")(event.target.value);
                }}
              />
            </FormControl>
            <FormControl required>
              <FormControl.Label>To</FormControl.Label>
              <TextInput
                block
                value={fields.to}
                placeholder="ops@example.com, dev@example.com"
                onChange={(event) => {
                  set("to")(event.target.value);
                }}
              />
              <FormControl.Caption>Separate several addresses with commas.</FormControl.Caption>
            </FormControl>
          </>
        ) : null}
        {problem ? (
          <Callout variant="danger" role="alert">
            {problem}
          </Callout>
        ) : null}
        {mutation.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(mutation.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function channelDetail(channel: NotificationChannel): string {
  if (channel.kind === "email") {
    const { host, port, to_addresses: to } = channel.summary;
    return `${host ?? ""}:${String(port ?? "")} to ${(to ?? []).join(", ")}`;
  }
  const signed = channel.kind === "webhook" && channel.summary.signed ? " · signed" : "";
  return `Webhook URL configured, ends in ${channel.hint ?? "····"}${signed}`;
}

function ChannelRow({
  channel,
  onEdit,
  onDelete,
  onTested,
}: {
  channel: NotificationChannel;
  onEdit: () => void;
  onDelete: () => void;
  onTested: (channel: NotificationChannel, result: TestResult) => void;
}) {
  const test = useTestChannel();
  const update = useUpdateChannel(channel.id);
  return (
    <SettingsRow
      title={channel.name}
      description={
        <div className={box.stack}>
          <span>{channelDetail(channel)}</span>
          <span className={box.cluster}>
            <Label>{KIND_LABEL[channel.kind]}</Label>
            <Label variant={channel.enabled ? "success" : "secondary"}>
              {channel.enabled ? "Enabled" : "Disabled"}
            </Label>
            {channel.last_test_at ? (
              <span>
                Last test {channel.last_test_ok ? "succeeded" : "failed"}{" "}
                <RelativeTime iso={channel.last_test_at} />
              </span>
            ) : (
              <span>Never tested</span>
            )}
          </span>
        </div>
      }
      action={
        <span className={box.cluster}>
          <Button
            size="small"
            aria-label={`Send test to ${channel.name}`}
            disabled={test.isPending}
            onClick={() => {
              test.mutate(channel.id, {
                onSuccess: (result) => {
                  onTested(channel, result);
                },
              });
            }}
          >
            {test.isPending ? "Sending…" : "Send test"}
          </Button>
          <Button
            size="small"
            aria-label={`${channel.enabled ? "Disable" : "Enable"} ${channel.name}`}
            disabled={update.isPending}
            onClick={() => {
              update.mutate({ enabled: !channel.enabled });
            }}
          >
            {channel.enabled ? "Disable" : "Enable"}
          </Button>
          <Button size="small" aria-label={`Edit ${channel.name}`} onClick={onEdit}>
            Edit
          </Button>
          <Button
            size="small"
            variant="danger"
            aria-label={`Delete ${channel.name}`}
            onClick={onDelete}
          >
            Delete
          </Button>
        </span>
      }
    />
  );
}

export function ChannelsSection() {
  const channels = useChannels();
  const remove = useDeleteChannel();
  const [dialog, setDialog] = useState<NotificationChannel | "new" | null>(null);
  const [deleting, setDeleting] = useState<NotificationChannel | null>(null);
  const [tested, setTested] = useState<{ channel: string; result: TestResult } | null>(null);
  return (
    <SettingsSection
      title="Channels"
      intro="Where Jendock sends alerts. Webhook URLs and SMTP credentials are stored encrypted by the build worker and are never shown again; the list only tells you a channel is configured."
      actions={
        <Button
          variant="primary"
          size="small"
          leadingVisual={PlusIcon}
          onClick={() => {
            setDialog("new");
          }}
        >
          New channel
        </Button>
      }
    >
      {channels.isPending ? <Loading label="Loading channels" /> : null}
      {channels.isError ? (
        <ErrorCallout
          title="Could not load the channels"
          error={channels.error}
          onRetry={() => void channels.refetch()}
        />
      ) : null}
      {channels.data?.length === 0 ? (
        <EmptyState icon={<BellIcon size={32} aria-hidden="true" />} title="No channels yet">
          Add a Slack webhook, a generic webhook or an SMTP mailbox, then choose which events go to
          it.
        </EmptyState>
      ) : null}
      {channels.data && channels.data.length > 0 ? (
        <div className={box.box}>
          <ul className={box.list} aria-label="Notification channels">
            {channels.data.map((channel) => (
              <li key={channel.id}>
                <ChannelRow
                  channel={channel}
                  onEdit={() => {
                    setDialog(channel);
                  }}
                  onDelete={() => {
                    setDeleting(channel);
                  }}
                  onTested={(item, result) => {
                    setTested({ channel: item.name, result });
                  }}
                />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {tested ? (
        <Callout variant={tested.result.ok ? "success" : "danger"} role="status">
          {tested.result.ok
            ? `Test message sent to ${tested.channel}.`
            : `The test to ${tested.channel} failed: ${tested.result.error ?? "unknown error"}.`}
        </Callout>
      ) : null}
      {dialog ? (
        <ChannelDialog
          existing={dialog === "new" ? null : dialog}
          onClose={() => {
            setDialog(null);
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmationDialog
          title={`Delete ${deleting.name}?`}
          confirmButtonContent="Delete channel"
          cancelButtonContent="Keep it"
          confirmButtonType="danger"
          confirmButtonLoading={remove.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") {
              remove.mutate(deleting.id, {
                onSuccess: () => {
                  setDeleting(null);
                },
              });
            } else setDeleting(null);
          }}
        >
          <p>
            Its rules and delivery history are removed too, and the stored webhook URL or SMTP
            credentials are deleted. This cannot be undone.
          </p>
          {remove.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(remove.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </SettingsSection>
  );
}

function DefaultRules() {
  const channels = useChannels();
  const policy = useNotificationPolicy();
  const rules = useRules(null, null);
  const failed = channels.error ?? policy.error ?? rules.error;
  return (
    <SettingsSection
      title="Default rules"
      intro="Which events go to which channel unless a project or environment chooses its own. Disk and webhook alerts only exist here."
    >
      {failed ? (
        <ErrorCallout title="Could not load the rules" error={failed} />
      ) : channels.data && policy.data && rules.data ? (
        <RuleEditor
          slug={null}
          env={null}
          channels={channels.data}
          events={policy.data.events}
          ruleSet={rules.data}
        />
      ) : (
        <Loading label="Loading rules" />
      )}
      {policy.data ? (
        <p className="muted">
          Messages about the same event and resource within {policy.data.dedupe_minutes} minutes are
          collapsed into one. A failed delivery is retried up to {policy.data.max_attempts} times
          with growing delays and never slows a run down. Disk alerts fire above{" "}
          {policy.data.disk_threshold_percent}% and webhook alerts after more than{" "}
          {policy.data.webhook_failure_threshold} invalid signatures in{" "}
          {policy.data.webhook_failure_window_minutes} minutes.
        </p>
      ) : null}
    </SettingsSection>
  );
}

export function NotificationsPage() {
  return (
    <>
      <ChannelsSection />
      <DefaultRules />
      <SettingsSection
        title="Delivery history"
        intro="The latest messages with their status. Errors never include the webhook URL or credentials."
      >
        <DeliveryHistory slug={null} />
      </SettingsSection>
    </>
  );
}
