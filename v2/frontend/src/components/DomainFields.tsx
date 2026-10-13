import { Checkbox, FormControl, Select, TextInput } from "@primer/react";
import type { CreateDomainBody } from "../api/adminTypes";
import styles from "./DomainFields.module.css";

/** What the user types for one domain; strings so a half-typed number is not rejected early. */
export interface DomainDraft {
  /** The Cloudflare zone chosen in the picker; empty means the host name is typed in full. */
  zone: string;
  /** The part before the zone (`app` for `app.example.com`); empty means the zone itself. */
  label: string;
  /** The full host name when no zone is chosen. */
  manual: string;
  service: string;
  containerPort: string;
  publishedPort: string;
  proxied: boolean;
}

export function emptyDomainDraft(publishedPort = "20001"): DomainDraft {
  return {
    zone: "",
    label: "",
    manual: "",
    service: "web",
    containerPort: "8080",
    publishedPort,
    proxied: false,
  };
}

const LABELS = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/;
const HOSTNAME = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

/** `app` + `example.com` gives `app.example.com`; without a label the zone itself. */
export function hostnameOf(draft: DomainDraft): string {
  if (draft.zone === "") return draft.manual.trim().toLowerCase().replace(/\.$/, "");
  const label = draft.label.trim().toLowerCase().replace(/\.$/, "");
  return label === "" ? draft.zone : `${label}.${draft.zone}`;
}

/** The first thing wrong with the draft, in words for the person typing, or null. */
export function validateDomainDraft(draft: DomainDraft): string | null {
  if (
    draft.zone !== "" &&
    draft.label.trim() !== "" &&
    !LABELS.test(draft.label.trim().toLowerCase())
  ) {
    return "The part before the domain may use letters, digits and dashes, for example app.";
  }
  if (!HOSTNAME.test(hostnameOf(draft))) {
    return "Enter a host name such as app.example.com.";
  }
  if (draft.service.trim() === "") return "Enter the service that should answer.";
  const container = Number(draft.containerPort);
  const published = Number(draft.publishedPort);
  if (!Number.isInteger(container) || container < 1 || container > 65535) {
    return "The container port must be a number from 1 to 65535.";
  }
  if (!Number.isInteger(published) || published < 1024 || published > 65535) {
    return "The published port must be a number from 1024 to 65535.";
  }
  return null;
}

export function domainBody(draft: DomainDraft): CreateDomainBody {
  return {
    hostname: hostnameOf(draft),
    service: draft.service.trim(),
    container_port: Number(draft.containerPort),
    published_port: Number(draft.publishedPort),
    dns_record_type: "A",
    dns_target: null,
    dns_proxied: draft.proxied,
    tls_enabled: true,
  };
}

/**
 * Host name (zone picker or free text), service and port to expose, proxy on or off. Shared by
 * the project wizard and the "Add domain" dialog so both ask the same questions.
 */
export function DomainFields({
  draft,
  onChange,
  zones,
  services,
}: {
  draft: DomainDraft;
  onChange: (patch: Partial<DomainDraft>) => void;
  /** Zones the Cloudflare token can edit; empty means the host name is typed in full. */
  zones: string[];
  /** Compose services of the environment, when it already has some. */
  services: string[];
}) {
  const manual = zones.length === 0 || draft.zone === "";
  return (
    <div className={styles.fields}>
      {zones.length > 0 ? (
        <FormControl required>
          <FormControl.Label>Domain (Cloudflare zone)</FormControl.Label>
          <Select
            block
            value={draft.zone}
            onChange={(event) => {
              onChange({ zone: event.target.value });
            }}
          >
            <Select.Option value="">Another host name (type it in full)…</Select.Option>
            {zones.map((zone) => (
              <Select.Option key={zone} value={zone}>
                {zone}
              </Select.Option>
            ))}
          </Select>
          <FormControl.Caption>
            The zones your Cloudflare token can edit. Choose one, then type the part before it.
          </FormControl.Caption>
        </FormControl>
      ) : null}
      {manual ? (
        <FormControl required>
          <FormControl.Label>Host name</FormControl.Label>
          <TextInput
            block
            monospace
            value={draft.manual}
            placeholder="app.example.com"
            autoCapitalize="none"
            spellCheck={false}
            onChange={(event) => {
              onChange({ manual: event.target.value });
            }}
          />
          <FormControl.Caption>
            The address people type. Its domain must be in your Cloudflare account.
          </FormControl.Caption>
        </FormControl>
      ) : (
        <FormControl>
          <FormControl.Label>Subdomain</FormControl.Label>
          <TextInput
            block
            monospace
            value={draft.label}
            placeholder="app"
            autoCapitalize="none"
            spellCheck={false}
            trailingVisual={`.${draft.zone}`}
            onChange={(event) => {
              onChange({ label: event.target.value });
            }}
          />
          <FormControl.Caption>
            Result: <strong className="ltr">{hostnameOf(draft)}</strong>. Leave it empty to use the
            domain itself.
          </FormControl.Caption>
        </FormControl>
      )}
      {services.length > 0 ? (
        <FormControl required>
          <FormControl.Label>Service</FormControl.Label>
          <Select
            block
            value={services.includes(draft.service) ? draft.service : (services[0] ?? "")}
            onChange={(event) => {
              onChange({ service: event.target.value });
            }}
          >
            {services.map((service) => (
              <Select.Option key={service} value={service}>
                {service}
              </Select.Option>
            ))}
          </Select>
          <FormControl.Caption>
            The services of this environment&apos;s Docker Compose project.
          </FormControl.Caption>
        </FormControl>
      ) : (
        <FormControl required>
          <FormControl.Label>Service</FormControl.Label>
          <TextInput
            block
            monospace
            value={draft.service}
            autoCapitalize="none"
            spellCheck={false}
            onChange={(event) => {
              onChange({ service: event.target.value });
            }}
          />
          <FormControl.Caption>
            The Compose service that should answer. The environment has no services recorded yet, so
            this is checked once they are.
          </FormControl.Caption>
        </FormControl>
      )}
      <div className={styles.two}>
        <FormControl required>
          <FormControl.Label>Container port</FormControl.Label>
          <TextInput
            block
            type="number"
            value={draft.containerPort}
            onChange={(event) => {
              onChange({ containerPort: event.target.value });
            }}
          />
          <FormControl.Caption>
            The port the application listens on inside its container.
          </FormControl.Caption>
        </FormControl>
        <FormControl required>
          <FormControl.Label>Published port</FormControl.Label>
          <TextInput
            block
            type="number"
            value={draft.publishedPort}
            onChange={(event) => {
              onChange({ publishedPort: event.target.value });
            }}
          />
          <FormControl.Caption>
            The port on this server (1024 to 65535) the web server forwards to.
          </FormControl.Caption>
        </FormControl>
      </div>
      <FormControl>
        <Checkbox
          checked={draft.proxied}
          onChange={(event) => {
            onChange({ proxied: event.target.checked });
          }}
        />
        <FormControl.Label>Proxy through Cloudflare</FormControl.Label>
        <FormControl.Caption>
          Hides the server address and adds Cloudflare protection.
        </FormControl.Caption>
      </FormControl>
    </div>
  );
}
