import {
  CheckCircleFillIcon,
  GlobeIcon,
  KebabHorizontalIcon,
  PlusIcon,
  XCircleFillIcon,
} from "@primer/octicons-react";
import {
  ActionList,
  ActionMenu,
  Banner,
  Button,
  Checkbox,
  ConfirmationDialog,
  Details,
  Dialog,
  FormControl,
  IconButton,
  Label,
  Select,
  TextInput,
} from "@primer/react";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import {
  useCheckDomain,
  useCreateDomain,
  useDeleteDomain,
  useDomains,
  useExecutePlan,
  useIssueCertificate,
  usePreviewDomain,
} from "../api/adminHooks";
import type {
  Certificate,
  DnsRecord,
  DnsRecordType,
  DomainDetail,
  Plan,
  PlanOperation,
} from "../api/adminTypes";
import { useIngressReadiness, useZonePicker } from "../api/integrationHooks";
import { isApiError } from "../api/client";
import { useProject } from "../api/hooks";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import {
  DomainFields,
  domainBody,
  emptyDomainDraft,
  validateDomainDraft,
  type DomainDraft,
} from "../components/DomainFields";
import { EnvToolbar } from "../components/EnvSwitch";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { RelativeTime } from "../components/Time";
import { useCan } from "../lib/permissions";
import { capitalize, formatDateTime } from "../lib/format";
import box from "../styles/box.module.css";
import styles from "./Domains.module.css";

type LabelVariant = "default" | "secondary" | "success" | "attention" | "severe" | "danger";

function domainsPath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/domains`;
}

function describeRecord(record: DnsRecord): string {
  const proxy = record.proxied ? ", proxied by Cloudflare" : "";
  return `${record.record_type} ${record.name} → ${record.content}${proxy}`;
}

/** The DNS part of a plan in plain English, including the foreign-record conflict. */
export function dnsSentence(plan: Plan): string {
  const { dns } = plan;
  switch (dns.action) {
    case "create":
      return dns.desired
        ? `Jendock will create this DNS record in ${dns.zone}: ${describeRecord(dns.desired)}.`
        : "Jendock will create the DNS record.";
    case "update":
      return `Jendock will change the DNS record it created earlier${dns.current ? ` (now ${describeRecord(dns.current)})` : ""} to ${dns.desired ? describeRecord(dns.desired) : "the new value"}.`;
    case "delete":
      return dns.current
        ? `Jendock will delete the DNS record it created: ${describeRecord(dns.current)}. Records made by anyone else stay.`
        : "Jendock will delete the DNS record it created.";
    case "noop":
      return "DNS is already correct. Nothing changes there.";
    case "conflict":
      return `${dns.message || "A DNS record for this name already exists and Jendock did not create it."}${dns.current ? ` It is ${describeRecord(dns.current)}.` : ""} Jendock never overwrites a record it does not own. Delete or rename that record in Cloudflare (or pick another host name), then preview again.`;
  }
}

function certLabel(certificate: Certificate): { variant: LabelVariant; text: string } {
  const days = certificate.days_remaining;
  switch (certificate.state) {
    case "ok":
      return { variant: "success", text: `Certificate valid, ${days} days left` };
    case "renewal_due":
      return { variant: "attention", text: `Certificate renews automatically (${days} days left)` };
    case "warning":
      return { variant: "severe", text: `Certificate expires in ${days} days` };
    case "critical":
      return { variant: "danger", text: `Certificate expires in ${days} days: act now` };
    case "expired":
      return { variant: "danger", text: "Certificate expired" };
  }
}

const STATUS_TEXT: Record<DomainDetail["status"], { variant: LabelVariant; text: string }> = {
  draft: { variant: "secondary", text: "Draft: not on the server yet" },
  active: { variant: "success", text: "Active" },
  error: { variant: "danger", text: "Last change failed" },
};

function CheckLine({ name, ok, detail }: { name: string; ok: boolean; detail: string }) {
  const Icon = ok ? CheckCircleFillIcon : XCircleFillIcon;
  return (
    <li className={styles.check}>
      <Icon size={16} className={ok ? styles.ok : styles.bad} aria-hidden="true" />
      <span>
        <strong>{name}</strong>{" "}
        <span className="muted">
          {ok ? "OK" : "Failed"}: {detail}
        </span>
      </span>
    </li>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <pre className={styles.diff} tabIndex={0} aria-label="Web server configuration changes">
      {lines.map((line, index) => (
        <span
          key={index}
          className={
            line.startsWith("+") ? styles.add : line.startsWith("-") ? styles.del : undefined
          }
        >
          {line}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

function PlanDialog({
  slug,
  env,
  domain,
  operation,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  domain: DomainDetail;
  operation: PlanOperation;
  onClose: () => void;
}) {
  const preview = usePreviewDomain(slug, env);
  const execute = useExecutePlan(slug, env);
  const started = useRef(false);
  const { mutate } = preview;
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    mutate({ id: domain.id, operation });
  }, [mutate, domain.id, operation]);

  const plan = preview.data;
  const done = execute.isSuccess;
  const stale = execute.isError && isApiError(execute.error) && execute.error.status === 409;
  const removing = operation === "remove";
  const run = () => {
    if (plan) execute.mutate({ id: domain.id, operation, plan_hash: plan.plan_hash });
  };
  const again = () => {
    execute.reset();
    preview.mutate({ id: domain.id, operation });
  };

  return (
    <Dialog
      title={`${removing ? "Remove" : "Apply"} ${domain.hostname}`}
      subtitle={
        removing
          ? "Review what will be removed from the server. Nothing happens until you confirm."
          : "Review what will change. Nothing happens until you confirm."
      }
      width="xlarge"
      onClose={onClose}
      footerButtons={
        done
          ? [{ buttonType: "primary", content: "Close", onClick: onClose }]
          : [
              { buttonType: "default", content: "Cancel", onClick: onClose },
              ...(stale
                ? [{ buttonType: "default" as const, content: "Preview again", onClick: again }]
                : []),
              {
                buttonType: removing ? ("danger" as const) : ("primary" as const),
                content: execute.isPending
                  ? removing
                    ? "Removing…"
                    : "Applying…"
                  : removing
                    ? "Remove from server"
                    : "Apply changes",
                disabled: !plan || plan.blocked || execute.isPending || stale,
                onClick: run,
              },
            ]
      }
    >
      <div className={box.stack}>
        {preview.isPending ? <Loading label="Preparing the preview" /> : null}
        {preview.isError ? (
          <ErrorCallout title="Could not prepare the preview" error={preview.error} />
        ) : null}
        {plan ? (
          <>
            {plan.blocked ? (
              <Callout variant="danger" role="alert" title="This cannot be applied yet">
                {dnsSentence(plan)}
              </Callout>
            ) : null}
            <section aria-labelledby="plan-steps">
              <h3 id="plan-steps" className={styles.h}>
                What will happen
              </h3>
              <ol className={styles.steps}>
                {plan.steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
            </section>
            {!plan.blocked ? (
              <section aria-labelledby="plan-dns">
                <h3 id="plan-dns" className={styles.h}>
                  DNS
                </h3>
                <p>{dnsSentence(plan)}</p>
              </section>
            ) : null}
            <section aria-labelledby="plan-vhost">
              <h3 id="plan-vhost" className={styles.h}>
                Web server (Apache) configuration
              </h3>
              {plan.vhost.action === "noop" ? (
                <p>The web server is already configured this way.</p>
              ) : (
                <>
                  <p>
                    {plan.vhost.action === "create"
                      ? "A new site is added that forwards this host name to your container."
                      : plan.vhost.action === "delete"
                        ? "The site below is removed."
                        : "The site configuration changes as shown."}{" "}
                    Lines starting with + are added and lines starting with - are removed.
                  </p>
                  <DiffView diff={plan.vhost.diff} />
                </>
              )}
            </section>
            <section aria-labelledby="plan-tls">
              <h3 id="plan-tls" className={styles.h}>
                HTTPS certificate
              </h3>
              <p>
                {plan.tls.action === "issue"
                  ? `A certificate will be requested from Let's Encrypt (${plan.tls.challenge ?? "automatic"} validation). This can take a few minutes.`
                  : plan.tls.action === "keep"
                    ? "The existing certificate is kept."
                    : plan.tls.action === "remove"
                      ? "The certificate files are removed."
                      : "HTTPS is switched off for this domain."}
              </p>
            </section>
          </>
        ) : null}
        {execute.isPending ? (
          <Callout role="status">
            Working on it. This can take up to five minutes while the certificate is issued; keep
            this window open.
          </Callout>
        ) : null}
        {stale ? (
          <Callout variant="warning" role="alert" title="Things changed since you looked">
            DNS or the server changed after the preview, so nothing was applied. Press Preview again
            and review the new plan.
          </Callout>
        ) : null}
        {execute.isError && !stale ? (
          <Callout variant="danger" role="alert" title="It did not work">
            {errorMessage(execute.error)}
            {removing ? "" : " Apache was put back to its previous configuration."}
          </Callout>
        ) : null}
        {done ? (
          <Callout variant="success" role="status">
            {removing
              ? "Removed. The domain is a draft again."
              : "Applied. Run the checks to confirm it works."}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

function AddDomainDialog({
  slug,
  env,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  onClose: () => void;
}) {
  const create = useCreateDomain(slug, env);
  const project = useProject(slug);
  const picker = useZonePicker();
  const services = project.data?.environments.find((item) => item.name === env)?.services ?? [];
  const [draft, setDraft] = useState<DomainDraft>(() => emptyDomainDraft());
  const [type, setType] = useState<DnsRecordType>("A");
  const [target, setTarget] = useState("");
  const [tls, setTls] = useState(true);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = () => {
    const found =
      validateDomainDraft(draft) ??
      (type === "CNAME" && target.trim() === ""
        ? "A CNAME record needs a target host name."
        : null);
    if (found !== null) {
      setProblem(found);
      return;
    }
    setProblem(null);
    create.mutate(
      {
        ...domainBody(draft),
        dns_record_type: type,
        dns_target: target.trim() === "" ? null : target.trim(),
        tls_enabled: tls,
      },
      { onSuccess: onClose },
    );
  };

  return (
    <Dialog
      title={`Add a domain to ${capitalize(env)}`}
      subtitle="Adding only saves a draft. Nothing is changed in DNS or on the server until you preview and apply it."
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: create.isPending ? "Adding…" : "Add domain",
          disabled: create.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        {picker.configured === false ? (
          <Callout variant="warning" title="Cloudflare is not connected yet">
            You can save the draft, but it cannot be applied until a token is stored in{" "}
            <Link to="/settings/integrations">Settings, Integrations</Link>.
          </Callout>
        ) : null}
        {picker.problem ? (
          <Callout variant="warning" title="The zone list is not available">
            {picker.problem} Type the full host name instead, or check the token in{" "}
            <Link to="/settings/integrations">Settings, Integrations</Link>.
          </Callout>
        ) : null}
        <DomainFields
          draft={draft}
          zones={picker.zones}
          services={services}
          onChange={(patch) => {
            setDraft((current) => ({ ...current, ...patch }));
          }}
        />
        <FormControl>
          <FormControl.Label>DNS record type</FormControl.Label>
          <Select
            block
            value={type}
            onChange={(event) => {
              setType(event.target.value as DnsRecordType);
            }}
          >
            <Select.Option value="A">A: points to this server&apos;s IPv4 address</Select.Option>
            <Select.Option value="AAAA">
              AAAA: points to this server&apos;s IPv6 address
            </Select.Option>
            <Select.Option value="CNAME">CNAME: an alias of another host name</Select.Option>
          </Select>
        </FormControl>
        {type === "CNAME" ? (
          <FormControl required>
            <FormControl.Label>CNAME target</FormControl.Label>
            <TextInput
              block
              monospace
              value={target}
              onChange={(event) => {
                setTarget(event.target.value);
              }}
            />
          </FormControl>
        ) : null}
        <FormControl>
          <Checkbox
            checked={tls}
            onChange={(event) => {
              setTls(event.target.checked);
            }}
          />
          <FormControl.Label>Serve over HTTPS</FormControl.Label>
          <FormControl.Caption>
            A free certificate is issued and renewed automatically.
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

type Action =
  | { kind: "add" }
  | { kind: "plan"; id: number; operation: PlanOperation }
  | { kind: "delete"; id: number };

function DomainRow({
  slug,
  env,
  domain,
  onAction,
  onNotice,
}: {
  slug: string;
  env: EnvironmentName;
  domain: DomainDetail;
  onAction: (action: Action) => void;
  onNotice: (notice: { variant: "success" | "danger"; text: string } | null) => void;
}) {
  const check = useCheckDomain(slug, env);
  const issue = useIssueCertificate(slug, env);
  const manage = useCan()("domain.manage", { project: slug, env });
  const status = STATUS_TEXT[domain.status];
  const canCert = domain.status === "active" && domain.tls_enabled;
  const results = domain.check?.results;
  return (
    <li className={box.row} aria-label={domain.hostname}>
      <div className={styles.head}>
        <div className={styles.title}>
          <strong className="ltr">{domain.hostname}</strong>
          <Label variant={status.variant}>{status.text}</Label>
          {domain.certificate ? (
            <Label variant={certLabel(domain.certificate).variant}>
              {certLabel(domain.certificate).text}
            </Label>
          ) : null}
        </div>
        <div className={box.cluster}>
          {manage ? (
            <>
              <Button
                size="small"
                variant={domain.status === "active" ? "default" : "primary"}
                onClick={() => {
                  onAction({ kind: "plan", id: domain.id, operation: "apply" });
                }}
                aria-label={`Preview and apply ${domain.hostname}`}
              >
                {domain.status === "active" ? "Preview" : "Preview and apply"}
              </Button>
              <Button
                size="small"
                disabled={check.isPending || domain.status === "draft"}
                aria-label={`Run checks for ${domain.hostname}`}
                onClick={() => {
                  onNotice(null);
                  check.mutate(domain.id);
                }}
              >
                {check.isPending ? "Checking…" : "Run checks"}
              </Button>
              <ActionMenu>
                <ActionMenu.Anchor>
                  <IconButton
                    size="small"
                    icon={KebabHorizontalIcon}
                    aria-label={`More actions for ${domain.hostname}`}
                  />
                </ActionMenu.Anchor>
                <ActionMenu.Overlay width="medium">
                  <ActionList>
                    <ActionList.Item
                      disabled={!canCert || issue.isPending}
                      onSelect={() => {
                        onNotice(null);
                        issue.mutate(domain.id, {
                          onSuccess: () => {
                            onNotice({
                              variant: "success",
                              text: `Certificate for ${domain.hostname} issued.`,
                            });
                          },
                          onError: (error) => {
                            onNotice({ variant: "danger", text: errorMessage(error) });
                          },
                        });
                      }}
                    >
                      Issue or renew certificate
                      <ActionList.Description variant="block">
                        {canCert
                          ? "Requests a new certificate now."
                          : "Needs an active domain with HTTPS."}
                      </ActionList.Description>
                    </ActionList.Item>
                    <ActionList.Item
                      variant="danger"
                      disabled={domain.status === "draft"}
                      onSelect={() => {
                        onAction({ kind: "plan", id: domain.id, operation: "remove" });
                      }}
                    >
                      Remove from server…
                    </ActionList.Item>
                    <ActionList.Item
                      variant="danger"
                      disabled={domain.status !== "draft"}
                      onSelect={() => {
                        onAction({ kind: "delete", id: domain.id });
                      }}
                    >
                      Delete draft…
                    </ActionList.Item>
                  </ActionList>
                </ActionMenu.Overlay>
              </ActionMenu>
            </>
          ) : null}
        </div>
      </div>
      <p className={`muted ${styles.meta}`}>
        {domain.dns_record_type} record
        {domain.dns_target ? ` to ${domain.dns_target}` : " to this server"} · service{" "}
        <code>{domain.service}</code> port {domain.container_port} (published on{" "}
        {domain.published_port}) {domain.dns_proxied ? "· proxied · " : "· "}
        {domain.tls_enabled ? "HTTPS" : "HTTP only"}
      </p>
      {domain.status_message ? (
        <Callout variant="danger" title="The last change did not work">
          {domain.status_message}
        </Callout>
      ) : null}
      {domain.certificate ? (
        <p className="muted">
          Certificate valid until {formatDateTime(domain.certificate.not_after)}.
          {domain.certificate.last_error
            ? ` Last renewal problem: ${domain.certificate.last_error}`
            : ""}
        </p>
      ) : null}
      {check.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(check.error)}
        </Callout>
      ) : null}
      {results && domain.check ? (
        <Details className={styles.details} open={!domain.check.ok}>
          <Details.Summary>
            Checks {domain.check.ok ? "passed" : "found a problem"},{" "}
            <RelativeTime iso={domain.check.checked_at} />
          </Details.Summary>
          <ul className={styles.checks}>
            <CheckLine name="DNS" ok={results.dns.ok} detail={results.dns.detail} />
            <CheckLine name="HTTPS" ok={results.https.ok} detail={results.https.detail} />
            <CheckLine name="Backend" ok={results.backend.ok} detail={results.backend.detail} />
          </ul>
        </Details>
      ) : domain.status !== "draft" ? (
        <p className="muted">Not checked yet. Press Run checks.</p>
      ) : null}
    </li>
  );
}

/** Handed over by the project wizard through the router state. */
interface CreatedState {
  created?: string[];
  failed?: { hostname: string; message: string }[];
}

/** Why domains cannot be applied yet, with the way out: a Banner linking to Integrations. */
function ReadinessBanner({ slug, env }: { slug: string; env: EnvironmentName }) {
  const readiness = useIngressReadiness(slug, env);
  const blocking = readiness.data?.reasons.filter((reason) => reason.blocking) ?? [];
  if (!readiness.data || blocking.length === 0) return null;
  return (
    <Banner
      variant="warning"
      title={
        readiness.data.token_configured
          ? "Domains cannot be applied yet"
          : "Cloudflare is not set up yet"
      }
      description={
        <>
          {blocking.map((reason) => (
            <span key={reason.code} className={styles.reason}>
              {reason.message}
            </span>
          ))}{" "}
          You can still add drafts; applying them needs this fixed in{" "}
          <Link to="/settings/integrations">Settings, Integrations</Link>.
        </>
      }
      primaryAction={
        <Button as={Link} to="/settings/integrations">
          Open Integrations
        </Button>
      }
    />
  );
}

function CreatedBanner({ state, env }: { state: CreatedState; env: EnvironmentName }) {
  const hostnames = state.created ?? [];
  return (
    <>
      {hostnames.length > 0 ? (
        <Banner
          variant="success"
          title="Project created. Next step: put your domain online"
          description={
            <>
              {hostnames.join(", ")} {hostnames.length === 1 ? "is" : "are"} saved as{" "}
              {hostnames.length === 1 ? "a draft" : "drafts"} for {capitalize(env)}. Press Preview
              and apply below: Jendock shows the DNS record, the Apache site and the HTTPS
              certificate before it changes anything.
            </>
          }
        />
      ) : null}
      {state.failed?.map((item) => (
        <Banner
          key={item.hostname}
          variant="critical"
          title={`${item.hostname} was not saved`}
          description={`${item.message} The project itself was created; add the domain again with Add domain.`}
        />
      ))}
    </>
  );
}

export function DomainsPage() {
  const { slug = "", env: rawEnv } = useParams();
  const env = ENVIRONMENTS.find((name) => name === rawEnv) ?? null;
  const domains = useDomains(slug, env ?? "development");
  const remove = useDeleteDomain(slug, env ?? "development");
  const canManage = useCan()("domain.manage", { project: slug, env: env ?? "development" });
  const [search] = useSearchParams();
  const created = useLocation().state as CreatedState | null;
  const [action, setAction] = useState<Action | null>(
    search.get("add") === "1" ? { kind: "add" } : null,
  );
  const [notice, setNotice] = useState<{ variant: "success" | "danger"; text: string } | null>(
    null,
  );

  if (env === null) {
    return (
      <EmptyState
        icon={<GlobeIcon size={32} aria-hidden="true" />}
        title="Unknown environment"
        action={
          <Button as={Link} to={domainsPath(slug, "development")}>
            Go to Development domains
          </Button>
        }
      >
        Every project has exactly two environments: development and production.
      </EmptyState>
    );
  }
  const list = domains.data ?? [];
  const target = action && "id" in action ? list.find((item) => item.id === action.id) : undefined;
  const close = () => {
    setAction(null);
  };

  return (
    <div className={box.stack}>
      <EnvToolbar
        title={`${capitalize(env)} domains`}
        subtitle="Public host names that reach this environment: a DNS record, a web server site and an HTTPS certificate each."
        env={env}
        pathFor={(next) => domainsPath(slug, next)}
        actions={
          canManage ? (
            <Button
              variant="primary"
              leadingVisual={PlusIcon}
              onClick={() => {
                setAction({ kind: "add" });
              }}
            >
              Add domain
            </Button>
          ) : null
        }
      />
      {created ? <CreatedBanner state={created} env={env} /> : null}
      <ReadinessBanner slug={slug} env={env} />
      {notice ? (
        <Callout variant={notice.variant} role={notice.variant === "danger" ? "alert" : "status"}>
          {notice.text}
        </Callout>
      ) : null}
      {domains.isPending ? <Loading label="Loading domains" /> : null}
      {domains.isError ? (
        <ErrorCallout
          title="Could not load the domains"
          error={domains.error}
          onRetry={() => void domains.refetch()}
        />
      ) : null}
      {domains.data?.length === 0 ? (
        <EmptyState icon={<GlobeIcon size={32} aria-hidden="true" />} title="No domains yet">
          Add a host name to make this environment reachable from the internet. You will see exactly
          what changes in DNS and on the web server before anything is applied.
        </EmptyState>
      ) : null}
      {list.length > 0 ? (
        <ul className={`${box.box} ${box.list}`} aria-label="Domains">
          {list.map((domain) => (
            <DomainRow
              key={domain.id}
              slug={slug}
              env={env}
              domain={domain}
              onAction={setAction}
              onNotice={setNotice}
            />
          ))}
        </ul>
      ) : null}

      {action?.kind === "add" ? <AddDomainDialog slug={slug} env={env} onClose={close} /> : null}
      {action?.kind === "plan" && target ? (
        <PlanDialog
          key={`${String(target.id)}-${action.operation}`}
          slug={slug}
          env={env}
          domain={target}
          operation={action.operation}
          onClose={close}
        />
      ) : null}
      {action?.kind === "delete" && target ? (
        <ConfirmationDialog
          title={`Delete ${target.hostname}?`}
          confirmButtonContent="Delete draft"
          cancelButtonContent="Keep it"
          confirmButtonType="danger"
          confirmButtonLoading={remove.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") remove.mutate(target.id, { onSuccess: close });
            else close();
          }}
        >
          <p>The draft is deleted. Nothing exists on the server for it, so nothing else changes.</p>
          {remove.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(remove.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </div>
  );
}
