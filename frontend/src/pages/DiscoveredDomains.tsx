import { GlobeIcon, LinkIcon, SyncIcon } from "@primer/octicons-react";
import {
  Button,
  ConfirmationDialog,
  Dialog,
  FormControl,
  Label,
  Select,
  TextInput,
} from "@primer/react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useCloudflare } from "../api/integrationHooks";
import {
  useAdoptDomain,
  useAdoptSuggested,
  useDiscovered,
  useDiscoveryState,
  useRefreshDiscovery,
} from "../api/discoveryHooks";
import type {
  AdoptSuggestedResult,
  Confidence,
  DiscoveredDomain,
  DiscoveryState,
} from "../api/discoveryTypes";
import { useAllProjects } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName, type Project } from "../api/types";
import { Callout, EmptyState, ErrorCallout, errorMessage } from "../components/Feedback";
import { PagedList } from "../components/PagedList";
import { PageHead } from "../components/PageHead";
import { RelativeTime } from "../components/Time";
import { capitalize } from "../lib/format";
import { useListParams, type FilterDef, type ListSpec, type SortOption } from "../lib/listParams";
import { useCan } from "../lib/permissions";
import box from "../styles/box.module.css";
import styles from "./DiscoveredDomains.module.css";

const SORTS: readonly SortOption[] = [
  { value: "hostname", label: "Name", sort: "hostname" },
  { value: "zone", label: "Zone", sort: "zone" },
];

const LINK_FILTER: FilterDef = {
  param: "link",
  label: "Status",
  allLabel: "All domains",
  options: [
    { value: "linked", label: "Linked to a project" },
    { value: "suggested", label: "Suggested project" },
    { value: "unlinked", label: "No match" },
  ],
};

const CONFIDENCE_LABEL: Record<Confidence, string> = {
  high: "certain",
  medium: "likely",
  low: "possible",
};

function projectPath(project: string, env: EnvironmentName): string {
  return `/projects/${encodeURIComponent(project)}/environments/${env}/domains`;
}

function describeDns(item: DiscoveredDomain): string {
  if (!item.dns) return "No Cloudflare record";
  const mode = item.dns.proxied ? "proxied" : "DNS only";
  return `${item.dns.record_type} ${item.dns.content}, ${mode}`;
}

function describeApache(item: DiscoveredDomain, helper: DiscoveryState["helper"]): string {
  if (item.vhost) {
    const target = item.vhost.upstream_port
      ? `proxies to ${item.vhost.upstream_host ?? "127.0.0.1"}:${String(item.vhost.upstream_port)}`
      : "no proxy";
    return `Apache ${target}${item.vhost.ssl ? ", HTTPS" : ""}${item.vhost.file ? ` (${item.vhost.file})` : ""}`;
  }
  if (helper.state === "ok") return "No Apache site";
  return "Apache not read";
}

export interface AdoptTarget {
  hostname: string;
  project: string;
  environment: EnvironmentName;
  service: string;
  containerPort: string;
  publishedPort: string;
}

function targetFor(item: DiscoveredDomain, projects: Project[]): AdoptTarget {
  const hint = item.suggestion;
  const project = hint?.project ?? projects[0]?.slug ?? "";
  const environment = hint?.environment ?? "production";
  const found = projects.find((entry) => entry.slug === project);
  const services = found?.environments.find((env) => env.name === environment)?.services ?? [];
  return {
    hostname: item.hostname,
    project,
    environment,
    service: hint?.service ?? services[0] ?? "web",
    containerPort: hint?.container_port ? String(hint.container_port) : "",
    publishedPort: String(hint?.published_port ?? item.vhost?.upstream_port ?? ""),
  };
}

function validPort(text: string): boolean {
  const value = Number(text);
  return Number.isInteger(value) && value >= 1 && value <= 65535;
}

/** Adopt one discovered domain: pick the project, the service and the ports. */
export function AdoptDialog({
  item,
  projects,
  onClose,
}: {
  item: DiscoveredDomain;
  projects: Project[];
  onClose: () => void;
}) {
  const adopt = useAdoptDomain();
  const [target, setTarget] = useState<AdoptTarget>(() => targetFor(item, projects));
  const [problem, setProblem] = useState<string | null>(null);
  const project = projects.find((entry) => entry.slug === target.project);
  const services = project?.environments.find((env) => env.name === target.environment)?.services;
  const change = (patch: Partial<AdoptTarget>) => {
    setTarget((current) => ({ ...current, ...patch }));
  };

  const submit = () => {
    const found =
      target.project === ""
        ? "Choose the project this domain belongs to."
        : target.service.trim() === ""
          ? "Enter the service that answers."
          : !validPort(target.containerPort) || !validPort(target.publishedPort)
            ? "Both ports must be numbers from 1 to 65535."
            : null;
    setProblem(found);
    if (found !== null) return;
    adopt.mutate(
      {
        slug: target.project,
        env: target.environment,
        body: {
          hostname: item.hostname,
          service: target.service.trim(),
          container_port: Number(target.containerPort),
          published_port: Number(target.publishedPort),
        },
      },
      { onSuccess: onClose },
    );
  };

  return (
    <Dialog
      title={`Adopt ${item.hostname}`}
      subtitle="Adopting only links this domain to a project in Jendock. Its DNS record and Apache site stay exactly as they are."
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: adopt.isPending ? "Adopting…" : "Adopt domain",
          disabled: adopt.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        {item.suggestion ? (
          <Callout title={`Suggested: ${item.suggestion.project}`}>
            {item.suggestion.reason}.
          </Callout>
        ) : null}
        <div className={styles.two}>
          <FormControl required>
            <FormControl.Label>Project</FormControl.Label>
            <Select
              block
              value={target.project}
              onChange={(event) => {
                change({ project: event.target.value });
              }}
            >
              {projects.map((entry) => (
                <Select.Option key={entry.slug} value={entry.slug}>
                  {entry.name}
                </Select.Option>
              ))}
            </Select>
          </FormControl>
          <FormControl required>
            <FormControl.Label>Environment</FormControl.Label>
            <Select
              block
              value={target.environment}
              onChange={(event) => {
                change({ environment: event.target.value as EnvironmentName });
              }}
            >
              {ENVIRONMENTS.map((name) => (
                <Select.Option key={name} value={name}>
                  {capitalize(name)}
                </Select.Option>
              ))}
            </Select>
          </FormControl>
        </div>
        <FormControl required>
          <FormControl.Label>Service</FormControl.Label>
          {services && services.length > 0 ? (
            <Select
              block
              value={services.includes(target.service) ? target.service : (services[0] ?? "")}
              onChange={(event) => {
                change({ service: event.target.value });
              }}
            >
              {services.map((name) => (
                <Select.Option key={name} value={name}>
                  {name}
                </Select.Option>
              ))}
            </Select>
          ) : (
            <TextInput
              block
              monospace
              value={target.service}
              autoCapitalize="none"
              spellCheck={false}
              onChange={(event) => {
                change({ service: event.target.value });
              }}
            />
          )}
        </FormControl>
        <div className={styles.two}>
          <FormControl required>
            <FormControl.Label>Container port</FormControl.Label>
            <TextInput
              block
              type="number"
              value={target.containerPort}
              onChange={(event) => {
                change({ containerPort: event.target.value });
              }}
            />
            <FormControl.Caption>The port the application listens on.</FormControl.Caption>
          </FormControl>
          <FormControl required>
            <FormControl.Label>Published port</FormControl.Label>
            <TextInput
              block
              type="number"
              value={target.publishedPort}
              onChange={(event) => {
                change({ publishedPort: event.target.value });
              }}
            />
            <FormControl.Caption>The port on this server Apache forwards to.</FormControl.Caption>
          </FormControl>
        </div>
        {problem ? (
          <Callout variant="danger" role="alert">
            {problem}
          </Callout>
        ) : null}
        {adopt.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(adopt.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function SourceBanners({ state }: { state: DiscoveryState }) {
  return (
    <>
      {state.cloudflare.state === "error" ? (
        <Callout variant="danger" role="alert" title="Cloudflare could not be read">
          {state.cloudflare.message} The list below is from the last successful refresh.
        </Callout>
      ) : null}
      {state.helper.state === "missing" || state.helper.state === "outdated" ? (
        <Callout variant="warning" title="Install the host helper to see Apache routing">
          {state.helper.state === "outdated"
            ? "The installed host helper is too old to list Apache sites. "
            : ""}
          Without it Jendock can still list the DNS records and match them to projects by name, but
          it cannot see which port Apache forwards to. See docs/ingress-host-helper.md.
        </Callout>
      ) : null}
      {state.ip_source === "unknown" && state.cloudflare.state === "ok" ? (
        <Callout variant="warning" title="The server address is not set">
          Every A, AAAA and CNAME record is listed because Jendock does not know which ones point at
          this server. Set the public IPv4 address in{" "}
          <Link to="/settings/integrations">Settings, Integrations</Link>.
        </Callout>
      ) : null}
    </>
  );
}

function DomainItem({
  item,
  state,
  canAdopt,
  onAdopt,
}: {
  item: DiscoveredDomain;
  state: DiscoveryState;
  canAdopt: boolean;
  onAdopt: (item: DiscoveredDomain) => void;
}) {
  const hint = item.suggestion;
  return (
    <li className={box.row} aria-label={item.hostname}>
      <div className={styles.head}>
        <div className={styles.title}>
          <strong className="ltr">{item.hostname}</strong>
          {item.link ? (
            <>
              <Label variant="success">Linked</Label>
              <Label variant={item.link.origin === "adopted" ? "accent" : "secondary"}>
                {item.link.origin === "adopted" ? "Adopted" : "Managed"}
              </Label>
            </>
          ) : hint ? (
            <Label variant="attention">Suggested</Label>
          ) : (
            <Label variant="secondary">No match</Label>
          )}
          {item.zone ? <Label variant="secondary">{item.zone}</Label> : null}
        </div>
        {!item.link && canAdopt ? (
          <Button
            size="small"
            variant={hint ? "primary" : "default"}
            leadingVisual={LinkIcon}
            aria-label={`Adopt ${item.hostname}`}
            onClick={() => {
              onAdopt(item);
            }}
          >
            Adopt…
          </Button>
        ) : null}
      </div>
      <p className={`muted ${styles.meta}`}>
        {describeDns(item)} · {describeApache(item, state.helper)}
      </p>
      {item.link ? (
        <p className={styles.meta}>
          <Link to={projectPath(item.link.project, item.link.environment)}>
            {item.link.project} / {capitalize(item.link.environment)}
          </Link>
        </p>
      ) : hint ? (
        <p className={styles.meta}>
          Suggested:{" "}
          <strong>
            {hint.project} / {capitalize(hint.environment)}
          </strong>
          {hint.service ? ` · ${hint.service}` : ""} ({CONFIDENCE_LABEL[hint.confidence]}).{" "}
          <span className="muted">{hint.reason}.</span>
        </p>
      ) : null}
    </li>
  );
}

function Summary({ state }: { state: DiscoveryState }) {
  return (
    <p className="muted" role="status">
      {state.refreshed_at ? (
        <>
          Refreshed <RelativeTime iso={state.refreshed_at} />.{" "}
        </>
      ) : null}
      {state.hosts} found, {state.linked} linked, {state.suggested} with a suggested project.
      {state.server_ip ? ` Server address ${state.server_ip}.` : ""}
    </p>
  );
}

function zoneFilter(zones: readonly string[]): FilterDef {
  return {
    param: "zone",
    label: "Zone",
    allLabel: "All zones",
    options: zones.map((zone) => ({ value: zone, label: zone })),
  };
}

export function DiscoveredDomainsPage() {
  const state = useDiscoveryState();
  const zones = state.data?.zones;
  const spec = useMemo<ListSpec>(
    () => ({ filters: [LINK_FILTER, zoneFilter(zones ?? [])], sorts: SORTS }),
    [zones],
  );
  const list = useListParams(spec);
  const domains = useDiscovered(list.apiQuery);
  const cloudflare = useCloudflare();
  const refresh = useRefreshDiscovery();
  const projects = useAllProjects();
  const can = useCan();
  const [adopting, setAdopting] = useState<DiscoveredDomain | null>(null);
  const [bulk, setBulk] = useState(false);
  const [bulkResult, setBulkResult] = useState<AdoptSuggestedResult | null>(null);

  const tokenConfigured = cloudflare.data?.token_configured ?? true;
  const data = state.data;
  const canRefresh = can("domain.discover_refresh");
  const canAdopt = can("domain.adopt_all");

  return (
    <>
      <PageHead
        title="Domains"
        leading={<GlobeIcon size={24} aria-hidden="true" />}
        description="Every host name that already points at this server through Cloudflare or Apache, and the project it belongs to. Discovery only reads: nothing changes in DNS or on the web server."
      />
      <div className={`${box.section} ${box.stack}`}>
        <div className={box.cluster}>
          {canAdopt && data && data.adoptable > 0 ? (
            <Button
              onClick={() => {
                setBulk(true);
              }}
            >
              Adopt all suggested ({data.adoptable})
            </Button>
          ) : null}
          {canRefresh ? (
            <Button
              variant="primary"
              leadingVisual={SyncIcon}
              disabled={refresh.isPending || !tokenConfigured}
              onClick={() => {
                refresh.mutate();
              }}
            >
              {refresh.isPending ? "Refreshing…" : "Refresh from Cloudflare"}
            </Button>
          ) : null}
        </div>
        {refresh.isError ? <ErrorCallout title="Could not refresh" error={refresh.error} /> : null}
        {bulkResult ? <BulkOutcome result={bulkResult} /> : null}
        {state.isError ? (
          <ErrorCallout
            title="Could not load the discovery state"
            error={state.error}
            onRetry={() => void state.refetch()}
          />
        ) : null}
        {!tokenConfigured ? (
          <EmptyState
            icon={<GlobeIcon size={32} aria-hidden="true" />}
            title="Connect Cloudflare to find your domains"
            action={
              <Button as={Link} to="/settings/integrations" variant="primary">
                Open Settings, Integrations
              </Button>
            }
          >
            Jendock reads your Cloudflare zones and DNS records to show which domains already point
            at this server. Store an API token (Zone: Read and DNS: Read are enough for this page)
            and come back here.
          </EmptyState>
        ) : null}
        {tokenConfigured && data ? <SourceBanners state={data} /> : null}
        {tokenConfigured && data && data.refreshed_at === null && data.hosts === 0 ? (
          <EmptyState
            icon={<GlobeIcon size={32} aria-hidden="true" />}
            title="Nothing discovered yet"
            action={
              canRefresh ? (
                <Button
                  variant="primary"
                  leadingVisual={SyncIcon}
                  disabled={refresh.isPending}
                  onClick={() => {
                    refresh.mutate();
                  }}
                >
                  Refresh from Cloudflare
                </Button>
              ) : undefined
            }
          >
            Press Refresh to read your Cloudflare zones and the Apache sites on this server.
          </EmptyState>
        ) : null}
        {tokenConfigured && data && (data.refreshed_at !== null || data.hosts > 0) ? (
          <>
            <Summary state={data} />
            <PagedList
              noun="domains"
              query={domains}
              list={list}
              filters={spec.filters ?? []}
              sorts={SORTS}
              placeholder="Find a domain, address or project…"
              empty={
                <EmptyState
                  icon={<GlobeIcon size={32} aria-hidden="true" />}
                  title="No domains found"
                >
                  No A, AAAA or CNAME record in your Cloudflare zones points at this server.
                </EmptyState>
              }
            >
              {(items) => (
                <ul className={box.list} aria-label="Discovered domains">
                  {items.map((item) => (
                    <DomainItem
                      key={item.hostname}
                      item={item}
                      state={data}
                      canAdopt={canAdopt}
                      onAdopt={setAdopting}
                    />
                  ))}
                </ul>
              )}
            </PagedList>
          </>
        ) : null}
      </div>
      {adopting ? (
        <AdoptDialog
          key={adopting.hostname}
          item={adopting}
          projects={projects.data ?? []}
          onClose={() => {
            setAdopting(null);
          }}
        />
      ) : null}
      {bulk ? (
        <BulkDialogWithResult
          count={data?.adoptable ?? 0}
          onDone={(result) => {
            setBulkResult(result);
            setBulk(false);
          }}
          onClose={() => {
            setBulk(false);
          }}
        />
      ) : null}
    </>
  );
}

function BulkOutcome({ result }: { result: AdoptSuggestedResult }) {
  return (
    <Callout
      variant={result.adopted.length > 0 ? "success" : "warning"}
      role="status"
      title={`Adopted ${String(result.adopted.length)} ${result.adopted.length === 1 ? "domain" : "domains"}`}
    >
      {result.skipped.length > 0
        ? `Skipped: ${result.skipped.map((item) => `${item.hostname} (${item.reason})`).join("; ")}.`
        : "Nothing was skipped."}
    </Callout>
  );
}

function BulkDialogWithResult({
  count,
  onDone,
  onClose,
}: {
  count: number;
  onDone: (result: AdoptSuggestedResult) => void;
  onClose: () => void;
}) {
  const adopt = useAdoptSuggested();
  return (
    <ConfirmationDialog
      title={`Adopt ${String(count)} suggested ${count === 1 ? "domain" : "domains"}?`}
      confirmButtonContent="Adopt all suggested"
      cancelButtonContent="Cancel"
      confirmButtonLoading={adopt.isPending}
      onClose={(gesture) => {
        if (gesture === "confirm") adopt.mutate(undefined, { onSuccess: onDone });
        else onClose();
      }}
    >
      <p>
        Only the certain matches are taken: domains where Apache forwards to the published port of
        exactly one project container. Nothing changes in DNS or on the web server; the domains are
        only linked to their projects here.
      </p>
      {adopt.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(adopt.error)}
        </Callout>
      ) : null}
    </ConfirmationDialog>
  );
}
