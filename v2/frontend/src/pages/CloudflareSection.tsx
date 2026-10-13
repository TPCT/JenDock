import { CheckCircleFillIcon, XCircleFillIcon } from "@primer/octicons-react";
import {
  Button,
  ConfirmationDialog,
  Details,
  FormControl,
  Label,
  Select,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import {
  useClearCloudflareToken,
  useCloudflare,
  useSaveIngressSettings,
  useSetCloudflareToken,
  useTestCloudflare,
} from "../api/integrationHooks";
import type { CloudflareStatus, IngressSettingsValues } from "../api/integrationTypes";
import { Callout, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import box from "../styles/box.module.css";
import styles from "./Integrations.module.css";

/** The exact steps of docs/ingress-host-helper.md, for a host where the helper is missing. */
export const HOST_HELPER_COMMANDS = `# 1. Apache: directories and the include (as root on the server)
install -d -m 0755 /etc/apache2/jendock-managed /etc/jendock/certs /var/lib/jendock/acme-webroot
a2enmod proxy proxy_http proxy_wstunnel rewrite headers alias ssl
echo 'IncludeOptional /etc/apache2/jendock-managed/jendock-*.conf' > /etc/apache2/conf-available/jendock.conf
a2enconf jendock && apachectl configtest

# 2. The helper code (from a checkout of this repository, run in its v2/ directory)
python3 -m venv /opt/jendock-host-helper/venv
/opt/jendock-host-helper/venv/bin/pip install 'cryptography==50.0.2'
install -d -m 0755 /opt/jendock-host-helper/lib/backend
cp -r backend/__init__.py backend/hosthelper /opt/jendock-host-helper/lib/backend/
chown -R root:root /opt/jendock-host-helper && chmod -R go-w /opt/jendock-host-helper

# 3. Its configuration: edit allowed_uids, allowed_hostname_suffixes (your zones), ports
install -m 0644 deployment/host-helper.example.json /etc/jendock/host-helper.json
/opt/jendock-host-helper/venv/bin/python -m backend.hosthelper --config /etc/jendock/host-helper.json --check

# 4. Run it
install -m 0644 deployment/jendock-host-helper.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now jendock-host-helper

# 5. Connect Jendock to it (use the same INGRESS_ACME_EMAIL and public address as below)
docker compose -f compose.yml -f compose.ingress.yml up -d`;

function HostHelperHelp() {
  return (
    <Details>
      <Details.Summary>How to install the host helper</Details.Summary>
      <div className={box.stack}>
        <p className="muted">
          The host helper is a small service on the server that writes Apache sites and certificates
          for Jendock. Run these as root and read <code>docs/ingress-host-helper.md</code> first;
          check every path against your server.
        </p>
        <pre
          className={styles.commands}
          tabIndex={0}
          aria-label="Host helper installation commands"
        >
          {HOST_HELPER_COMMANDS}
        </pre>
      </div>
    </Details>
  );
}

function Status({ ok, children }: { ok: boolean; children: string }) {
  const Icon = ok ? CheckCircleFillIcon : XCircleFillIcon;
  return (
    <Label variant={ok ? "success" : "attention"}>
      <Icon size={12} aria-hidden="true" /> {children}
    </Label>
  );
}

function TokenForm({ status }: { status: CloudflareStatus }) {
  const save = useSetCloudflareToken();
  const clear = useClearCloudflareToken();
  const test = useTestCloudflare();
  const [value, setValue] = useState("");
  const [removing, setRemoving] = useState(false);
  const result = test.data;
  return (
    <div className={`${box.box} ${box.row} ${box.stack}`}>
      <h3 className={styles.h}>API token</h3>
      <div className={styles.statusRow}>
        <Status ok={status.token_configured}>
          {status.token_configured ? "Token stored" : "No token stored"}
        </Status>
        {status.token_updated_at ? (
          <span className="muted">
            saved <RelativeTime iso={status.token_updated_at} />
          </span>
        ) : null}
      </div>
      <form
        className={box.stack}
        aria-label="Cloudflare API token"
        onSubmit={(event) => {
          event.preventDefault();
          const token = value.trim();
          if (token === "") return;
          save.mutate(token, {
            onSuccess: () => {
              setValue("");
              test.reset();
            },
          });
        }}
      >
        <FormControl>
          <FormControl.Label>
            {status.token_configured ? "Replace the token" : "Cloudflare API token"}
          </FormControl.Label>
          <TextInput
            block
            monospace
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={value}
            placeholder={status.token_configured ? "•••••••• (stored, hidden)" : "Paste the token"}
            onChange={(event) => {
              setValue(event.target.value);
            }}
          />
          <FormControl.Caption>
            Create it in Cloudflare under My Profile, API Tokens, with the permissions Zone: DNS:
            Edit and Zone: Zone: Read, limited to the zones Jendock may use. It is stored encrypted
            and can never be shown again, here or anywhere else.
          </FormControl.Caption>
        </FormControl>
        {save.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(save.error)}
          </Callout>
        ) : null}
        {save.isSuccess ? (
          <Callout variant="success" role="status">
            Token saved. Press Test connection to check it.
          </Callout>
        ) : null}
        <div className={box.cluster}>
          <Button type="submit" variant="primary" disabled={value.trim() === "" || save.isPending}>
            {save.isPending ? "Saving…" : "Save token"}
          </Button>
          <Button
            disabled={!status.token_configured || test.isPending}
            onClick={() => {
              test.mutate();
            }}
          >
            {test.isPending ? "Testing…" : "Test connection"}
          </Button>
          <Button
            variant="danger"
            disabled={!status.token_configured}
            onClick={() => {
              setRemoving(true);
            }}
          >
            Remove token
          </Button>
        </div>
      </form>
      {test.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(test.error)}
        </Callout>
      ) : null}
      {result ? (
        <div className={box.stack} role="status" aria-label="Connection test result">
          {result.ok ? (
            <Callout variant="success" title="Cloudflare accepted the token">
              {result.zones.length === 0
                ? "It is active but cannot see any zone yet. Give it access to the zones Jendock should manage."
                : `It can manage ${String(result.zones.length)} ${result.zones.length === 1 ? "zone" : "zones"}:`}
            </Callout>
          ) : (
            <Callout variant="danger" title="The connection test failed">
              {result.message}
            </Callout>
          )}
          {result.ok && result.zones.length > 0 ? (
            <ul className={styles.zones} aria-label="Zones the token can edit">
              {result.zones.map((zone) => (
                <li key={zone}>
                  <Label>{zone}</Label>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {removing ? (
        <ConfirmationDialog
          title="Remove the Cloudflare token?"
          confirmButtonContent="Remove token"
          cancelButtonContent="Keep it"
          confirmButtonType="danger"
          confirmButtonLoading={clear.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") {
              clear.mutate(undefined, {
                onSuccess: () => {
                  setRemoving(false);
                  test.reset();
                },
              });
            } else setRemoving(false);
          }}
        >
          <p>
            Domains stay as they are, but Jendock can no longer change DNS or issue certificates
            until you store a token again. (A <code>CLOUDFLARE_API_TOKEN</code> secret of an
            environment is still used when there is no platform token.)
          </p>
          {clear.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(clear.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </div>
  );
}

type Field = keyof IngressSettingsValues;
const FIELDS: Field[] = ["acme_email", "public_ipv4", "public_ipv6", "acme_challenge"];

function SettingsForm({ status }: { status: CloudflareStatus }) {
  const save = useSaveIngressSettings();
  const { stored, defaults } = status.settings;
  const [draft, setDraft] = useState<Record<Field, string>>(() => ({
    acme_email: stored.acme_email ?? "",
    public_ipv4: stored.public_ipv4 ?? "",
    public_ipv6: stored.public_ipv6 ?? "",
    acme_challenge: stored.acme_challenge ?? "",
  }));
  const changed = FIELDS.filter((field) => draft[field].trim() !== (stored[field] ?? ""));
  const submit = () => {
    const patch: Record<string, string | null> = {};
    for (const field of changed)
      patch[field] = draft[field].trim() === "" ? null : draft[field].trim();
    save.mutate(patch);
  };
  const text = (field: Field, label: string, placeholder: string, help: string) => (
    <FormControl>
      <FormControl.Label>{label}</FormControl.Label>
      <TextInput
        block
        monospace
        value={draft[field]}
        placeholder={defaults[field] ?? placeholder}
        autoCapitalize="none"
        spellCheck={false}
        onChange={(event) => {
          setDraft({ ...draft, [field]: event.target.value });
        }}
      />
      <FormControl.Caption>
        {help}{" "}
        {defaults[field]
          ? `Left empty, the server default ${defaults[field]} applies.`
          : "No server default is set."}
      </FormControl.Caption>
    </FormControl>
  );
  return (
    <form
      className={`${box.box} ${box.row} ${box.stack}`}
      aria-label="Certificate and DNS settings"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <h3 className={styles.h}>Certificates and DNS</h3>
      {text(
        "acme_email",
        "ACME contact email",
        "ops@example.com",
        "Let's Encrypt writes here about expiring certificates.",
      )}
      <FormControl>
        <FormControl.Label>Validation challenge</FormControl.Label>
        <Select
          block
          value={draft.acme_challenge}
          onChange={(event) => {
            setDraft({ ...draft, acme_challenge: event.target.value });
          }}
        >
          <Select.Option value="">
            Server default ({defaults.acme_challenge ?? "dns-01"})
          </Select.Option>
          <Select.Option value="dns-01">dns-01 (recommended, works behind the proxy)</Select.Option>
          <Select.Option value="http-01">http-01 (port 80 must reach this server)</Select.Option>
        </Select>
        <FormControl.Caption>
          How Let&apos;s Encrypt checks that you own the host name. dns-01 uses the Cloudflare
          token.
        </FormControl.Caption>
      </FormControl>
      <div className={styles.two}>
        {text(
          "public_ipv4",
          "Server IPv4 address",
          "203.0.113.7",
          "The address A records point to.",
        )}
        {text(
          "public_ipv6",
          "Server IPv6 address",
          "2001:db8::1",
          "The address AAAA records point to.",
        )}
      </div>
      {save.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(save.error)}
        </Callout>
      ) : null}
      {save.isSuccess && changed.length === 0 ? (
        <Callout variant="success" role="status">
          Saved.
        </Callout>
      ) : null}
      <div>
        <Button type="submit" variant="primary" disabled={changed.length === 0 || save.isPending}>
          {save.isPending ? "Saving…" : "Save settings"}
        </Button>
      </div>
    </form>
  );
}

function IngressStatus({ status }: { status: CloudflareStatus }) {
  const blocking = status.reasons.filter((reason) => reason.blocking);
  const advice = status.reasons.filter((reason) => !reason.blocking);
  const helperProblem = status.reasons.some(
    (reason) => reason.code === "helper_unreachable" || reason.code === "ingress_disabled",
  );
  return (
    <div className={`${box.box} ${box.row} ${box.stack}`}>
      <h3 className={styles.h}>Domains on this server</h3>
      <div className={styles.statusRow}>
        <Status ok={status.ready}>
          {status.ready ? "Ready to apply domains" : "Not ready yet"}
        </Status>
        {status.helper ? (
          <>
            <Status ok={status.helper.enabled}>
              {status.helper.enabled ? "Ingress enabled" : "Ingress disabled"}
            </Status>
            <Status ok={status.helper.reachable}>
              {status.helper.reachable ? "Host helper reachable" : "Host helper unreachable"}
            </Status>
          </>
        ) : null}
      </div>
      {blocking.length > 0 ? (
        <ul className={styles.reasons} aria-label="What is missing">
          {blocking.map((reason) => (
            <li key={reason.code}>{reason.message}</li>
          ))}
        </ul>
      ) : null}
      {advice.map((reason) => (
        <Callout key={reason.code} variant="warning">
          {reason.message}
        </Callout>
      ))}
      {status.helper ? (
        <p className="muted">
          Host helper socket: <code>{status.helper.socket_path}</code>
        </p>
      ) : null}
      {helperProblem ? <HostHelperHelp /> : null}
    </div>
  );
}

export function CloudflareSection() {
  const status = useCloudflare();
  return (
    <SettingsSection
      title="Cloudflare"
      intro="Jendock creates the DNS records for your domains and proves ownership for HTTPS certificates through Cloudflare. The token is kept by the worker, encrypted, and is never returned by the API."
    >
      {status.isPending ? <Loading label="Loading Cloudflare settings" /> : null}
      {status.isError ? (
        <ErrorCallout
          title="Could not load the Cloudflare settings"
          error={status.error}
          onRetry={() => void status.refetch()}
        />
      ) : null}
      {status.data ? (
        <>
          <IngressStatus status={status.data} />
          <TokenForm status={status.data} />
          <SettingsForm status={status.data} />
        </>
      ) : null}
    </SettingsSection>
  );
}
