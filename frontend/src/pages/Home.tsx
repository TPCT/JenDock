import {
  CloudIcon,
  GearIcon,
  GitCommitIcon,
  HistoryIcon,
  RepoIcon,
  PlusIcon,
  RepoPushIcon,
  ShieldLockIcon,
} from "@primer/octicons-react";
import { Button, Heading, Label, LinkButton, ProgressBar } from "@primer/react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useDashboard } from "../api/accountHooks";
import { useAllRuns } from "../api/hooks";
import type {
  Dashboard,
  DashboardCertificate,
  DashboardDeployment,
  DashboardHost,
  DashboardManualDeploy,
  DashboardRun,
  DeploymentStatus,
} from "../api/accountTypes";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import { RunList } from "../components/RunList";
import { StatusBadge } from "../components/Status";
import { Duration, RelativeTime } from "../components/Time";
import { TRIGGER_TEXT, runsPath } from "../components/EnvironmentCard";
import { capitalize, formatBytes, formatDuration, shortSha } from "../lib/format";
import box from "../styles/box.module.css";
import styles from "./Home.module.css";

type LabelVariant = "success" | "danger" | "attention" | "severe" | "secondary";

const DEPLOYMENT_LABEL: Record<DeploymentStatus, { text: string; variant: LabelVariant }> = {
  pending: { text: "Queued", variant: "attention" },
  deploying: { text: "Deploying", variant: "attention" },
  succeeded: { text: "Deployed", variant: "success" },
  failed: { text: "Failed", variant: "danger" },
  rolled_back: { text: "Rolled back", variant: "severe" },
  rollback_failed: { text: "Rollback failed", variant: "danger" },
};

function Card({
  title,
  headingId,
  actions,
  children,
}: {
  title: string;
  headingId: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className={box.box} aria-labelledby={headingId}>
      <div className={box.header}>
        <Heading as="h2" id={headingId} className={styles.cardTitle}>
          {title}
        </Heading>
        {actions}
      </div>
      {children}
    </section>
  );
}

function Hint({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`${box.row} ${styles.hint}`}>
      <p className="muted">{children}</p>
      {action}
    </div>
  );
}

const envLabel = (project: string, environment: string) => `${project} / ${environment}`;

function AttentionCard({ runs, hasProjects }: { runs: DashboardRun[]; hasProjects: boolean }) {
  return (
    <Card title="Needs attention" headingId="home-attention">
      {runs.length === 0 ? (
        <Hint
          action={
            hasProjects ? (
              <LinkButton as={Link} to="/runs" size="small">
                View all runs
              </LinkButton>
            ) : null
          }
        >
          Nothing needs you right now. Runs that are in progress, and environments whose latest run
          failed, appear here.
        </Hint>
      ) : (
        <ul className={box.list} aria-label="Runs needing attention">
          {runs.map((run) => (
            <li key={run.run_id} className={`${box.row} ${styles.item}`}>
              <div className={styles.itemMain}>
                <span className={box.cluster}>
                  <StatusBadge status={run.status} />
                  <Link to={`/runs/${String(run.run_id)}`}>Run #{run.number}</Link>
                  <span>{envLabel(run.project_name, run.environment)}</span>
                </span>
                <span className={`muted ${styles.meta}`}>
                  <span>{run.branch}</span>
                  <span className="mono ltr">{shortSha(run.commit_sha)}</span>
                  <span>{TRIGGER_TEXT[run.trigger]}</span>
                  <RelativeTime iso={run.finished_at ?? run.started_at ?? run.created_at} />
                </span>
                {run.message ? <span className="muted">{run.message}</span> : null}
              </div>
              {run.status === "running" || run.status === "queued" ? (
                <Duration startedAt={run.started_at} finishedAt={run.finished_at} />
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function DeploymentsCard({
  items,
  hasProjects,
}: {
  items: DashboardDeployment[];
  hasProjects: boolean;
}) {
  return (
    <Card title="Recent deployments" headingId="home-deployments">
      {items.length === 0 ? (
        <Hint
          action={
            <LinkButton as={Link} to={hasProjects ? "/projects" : "/projects/new"} size="small">
              {hasProjects ? "Open a project" : "Create a project"}
            </LinkButton>
          }
        >
          {hasProjects
            ? "Nothing has been deployed yet. Push to a project's branch, or start a run, to deploy for the first time."
            : "Deployments appear here once a project's pipeline ships something."}
        </Hint>
      ) : (
        <ul className={box.list} aria-label="Recent deployments">
          {items.map((item) => {
            const label = DEPLOYMENT_LABEL[item.status];
            return (
              <li key={item.id} className={`${box.row} ${styles.item}`}>
                <div className={styles.itemMain}>
                  <span className={box.cluster}>
                    <Label variant={label.variant}>{label.text}</Label>
                    <Link to={`/runs/${String(item.run_id)}`}>
                      {item.project_name} / {item.environment}
                    </Link>
                  </span>
                  <span className={`muted ${styles.meta}`}>
                    <span className={styles.commit}>
                      <GitCommitIcon size={14} aria-hidden="true" />
                      <span className="mono ltr">{shortSha(item.commit_sha)}</span>
                    </span>
                    <span>
                      {TRIGGER_TEXT[item.trigger]}
                      {item.triggered_by ? ` by ${item.triggered_by}` : ""}
                    </span>
                    {item.duration_seconds === null ? null : (
                      <span>took {formatDuration(item.duration_seconds * 1000)}</span>
                    )}
                    <RelativeTime iso={item.finished_at ?? item.created_at} />
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function ManualDeploysCard({ items }: { items: DashboardManualDeploy[] }) {
  return (
    <Card title="Pushes waiting for a manual deploy" headingId="home-manual">
      {items.length === 0 ? (
        <Hint>
          No pushes are waiting. When someone pushes to a branch whose environment has auto-deploy
          off, it is listed here so it does not go unnoticed.
        </Hint>
      ) : (
        <ul className={box.list} aria-label="Pushes waiting for a manual deploy">
          {items.map((item) => (
            <li key={`${item.project}/${item.environment}`} className={`${box.row} ${styles.item}`}>
              <div className={styles.itemMain}>
                <span className={box.cluster}>
                  <RepoPushIcon size={16} aria-hidden="true" />
                  <strong>{envLabel(item.project_name, item.environment)}</strong>
                </span>
                <span className={`muted ${styles.meta}`}>
                  <span>
                    {String(item.pushes)} {item.pushes === 1 ? "push" : "pushes"} to {item.branch}
                  </span>
                  <RelativeTime iso={item.last_push_at} />
                </span>
              </div>
              <span className={box.cluster}>
                <LinkButton as={Link} to={runsPath(item.project, item.environment)} size="small">
                  Start a run
                </LinkButton>
                <LinkButton
                  as={Link}
                  to={`/projects/${encodeURIComponent(item.project)}/settings/environments`}
                  size="small"
                >
                  Turn on auto-deploy
                </LinkButton>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function daysLeft(iso: string): number {
  return Math.ceil((Date.parse(iso) - Date.now()) / 86_400_000);
}

function CertificatesCard({
  items,
  domainCount,
}: {
  items: DashboardCertificate[];
  domainCount: number;
}) {
  return (
    <Card title="Certificates expiring soon" headingId="home-certificates">
      {items.length === 0 ? (
        <Hint>
          {domainCount === 0
            ? "No domains yet. Add one on a project's Domains page and Jendock issues and renews its certificate."
            : "No certificate expires in the next 30 days."}
        </Hint>
      ) : (
        <ul className={box.list} aria-label="Certificates expiring soon">
          {items.map((item) => {
            const left = daysLeft(item.not_after);
            return (
              <li key={item.hostname} className={`${box.row} ${styles.item}`}>
                <div className={styles.itemMain}>
                  <span className={box.cluster}>
                    <ShieldLockIcon size={16} aria-hidden="true" />
                    <Link
                      to={`/projects/${encodeURIComponent(item.project)}/environments/${item.environment}/domains`}
                    >
                      {item.hostname}
                    </Link>
                  </span>
                  <span className="muted">
                    {capitalize(item.environment)} of {item.project}
                  </span>
                </div>
                {item.expired ? (
                  <Label variant="danger">Expired</Label>
                ) : (
                  <Label variant={left <= 7 ? "danger" : "attention"}>
                    {left <= 1 ? "Expires today" : `Expires in ${String(left)} days`}
                  </Label>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function Meter({ label, percent, caption }: { label: string; percent: number; caption: string }) {
  return (
    <div className={styles.meter}>
      <div className={box.spread}>
        <span>{label}</span>
        <span className="muted">{caption}</span>
      </div>
      <ProgressBar
        aria-label={`${label} used`}
        progress={Math.round(percent)}
        barSize="small"
        bg={
          percent > 90 ? "danger.emphasis" : percent > 75 ? "attention.emphasis" : "accent.emphasis"
        }
      />
    </div>
  );
}

function HostCard({ host, error }: { host: DashboardHost | null; error: string | null }) {
  return (
    <Card title="Host" headingId="home-host">
      {host === null ? (
        <Hint>{error ?? "Host telemetry is not available yet."}</Hint>
      ) : (
        <div className={`${box.row} ${styles.meters}`}>
          <Meter
            label={`CPU (${String(host.cpu_count)} cores)`}
            percent={host.cpu_percent ?? 0}
            caption={host.cpu_percent === null ? "measuring…" : `${host.cpu_percent.toFixed(0)}%`}
          />
          <Meter
            label="Memory"
            percent={host.memory_percent}
            caption={`${formatBytes(host.memory_used)} of ${formatBytes(host.memory_total)} (${host.memory_percent.toFixed(0)}%)`}
          />
          <Meter
            label="Disk"
            percent={host.disk_percent}
            caption={`${formatBytes(host.disk_used)} of ${formatBytes(host.disk_total)} (${host.disk_percent.toFixed(0)}%)`}
          />
        </div>
      )}
    </Card>
  );
}

/** The newest runs of everything the user may read (the same list as the Runs page). */
function RecentRunsCard() {
  const runs = useAllRuns("per_page=8");
  return (
    <Card
      title="Recent runs"
      headingId="home-runs"
      actions={
        <LinkButton as={Link} to="/runs" size="small">
          View all runs
        </LinkButton>
      }
    >
      {runs.isPending ? <Loading label="Loading runs" /> : null}
      {runs.isError ? (
        <ErrorCallout
          title="Could not load runs"
          error={runs.error}
          onRetry={() => void runs.refetch()}
        />
      ) : null}
      {runs.data && runs.data.items.length === 0 ? (
        <Hint>
          No runs yet. Push to a tracked branch, or open a project and press Run pipeline.
        </Hint>
      ) : null}
      {runs.data && runs.data.items.length > 0 ? <RunList runs={runs.data.items} showProject /> : null}
    </Card>
  );
}

function QuickLinksCard() {
  return (
    <Card title="Quick links" headingId="home-links">
      <ul className={box.list} aria-label="Quick links">
        <li className={box.row}>
          <RepoIcon size={16} className="muted" aria-hidden="true" />{" "}
          <Link to="/projects">All projects</Link>
        </li>
        <li className={box.row}>
          <HistoryIcon size={16} className="muted" aria-hidden="true" />{" "}
          <Link to="/runs">All runs</Link>
        </li>
        <li className={box.row}>
          <GearIcon size={16} className="muted" aria-hidden="true" />{" "}
          <Link to="/settings">Settings and API tokens</Link>
        </li>
      </ul>
    </Card>
  );
}

function Welcome() {
  return (
    <EmptyState
      icon={<CloudIcon size={32} aria-hidden="true" />}
      title="Welcome to Jendock"
      action={
        <Button as={Link} to="/projects/new" variant="primary" leadingVisual={PlusIcon}>
          Create your first project
        </Button>
      }
    >
      Connect a GitHub repository, give it a pipeline, and Jendock builds and deploys it on every
      push. Deployments, failing runs and certificates that need you will show up on this page.
    </EmptyState>
  );
}

function Content({ data }: { data: Dashboard }) {
  const hasProjects = data.project_count > 0;
  return (
    <div className={box.stack}>
      {!hasProjects ? <Welcome /> : null}
      {hasProjects ? (
        <div className={styles.grid}>
          <AttentionCard runs={data.attention} hasProjects={hasProjects} />
          <RecentRunsCard />
          <DeploymentsCard items={data.deployments} hasProjects={hasProjects} />
          <ManualDeploysCard items={data.manual_deploys} />
          <CertificatesCard items={data.certificates} domainCount={data.domain_count} />
          <HostCard host={data.host} error={data.host_error} />
          <QuickLinksCard />
        </div>
      ) : (
        <div className={styles.grid}>
          <HostCard host={data.host} error={data.host_error} />
        </div>
      )}
    </div>
  );
}

export function HomePage() {
  const dashboard = useDashboard();
  return (
    <>
      <PageHead
        title="Home"
        description="What changed, what needs you, and how the host is doing."
        actions={
          <LinkButton as={Link} to="/projects" size="medium">
            All projects
          </LinkButton>
        }
      />
      <div className={box.section}>
        {dashboard.isPending ? <Loading label="Loading the dashboard" /> : null}
        {dashboard.isError ? (
          <ErrorCallout
            title="Could not load the dashboard"
            error={dashboard.error}
            onRetry={() => void dashboard.refetch()}
          />
        ) : null}
        {dashboard.data ? <Content data={dashboard.data} /> : null}
      </div>
    </>
  );
}
