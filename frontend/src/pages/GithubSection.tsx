import { Button, Label } from "@primer/react";
import { Link } from "react-router-dom";
import { useCreateDeployKey, useGithubIntegration } from "../api/integrationHooks";
import type { DeployKeyStatus, GithubIntegration } from "../api/integrationTypes";
import { CopyField } from "../components/CopyField";
import { Callout, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { SettingsRow, SettingsSection } from "../components/SettingsSection";
import { RelativeTime } from "../components/Time";
import box from "../styles/box.module.css";
import styles from "./Integrations.module.css";

const OUTCOME_VARIANT: Record<string, "success" | "secondary" | "attention" | "danger"> = {
  queued: "success",
  pong: "success",
  recorded: "secondary",
  ignored: "secondary",
  duplicate: "secondary",
};

const OUTCOME_TEXT: Record<string, string> = {
  queued: "Run queued",
  recorded: "Recorded, auto-deploy off",
  ignored: "Ignored",
  pong: "Ping answered",
  duplicate: "Repeat delivery",
};

function WebhookBox({ webhook }: { webhook: GithubIntegration["webhook"] }) {
  const counts = Object.entries(webhook.counts_7d);
  return (
    <div className={`${box.box} ${box.row} ${box.stack}`}>
      <h3 className={styles.h}>Webhook</h3>
      <p className="muted">
        One webhook per repository tells Jendock about pushes. Each project&apos;s GitHub connection
        page has the steps; the values are the same for every repository.
      </p>
      <CopyField label="Payload URL" value={webhook.payload_url} />
      <div className={styles.statusRow}>
        <span>Content type</span>
        <code>application/json</code>
      </div>
      <div className={styles.statusRow}>
        <span>Webhook secret</span>
        {webhook.secret_configured === null ? (
          <Label>Unknown: the worker did not answer</Label>
        ) : (
          <Label variant={webhook.secret_configured ? "success" : "danger"}>
            {webhook.secret_configured ? "Configured" : "Not configured"}
          </Label>
        )}
        <span className="muted">The value is never shown.</span>
      </div>
      {webhook.secret_configured === false ? (
        <Callout variant="warning" title="Deliveries will be rejected">
          The worker has no webhook secret file, so every signature check fails. Set{" "}
          <code>JENDOCK_GITHUB_WEBHOOK_SECRET_FILE</code> to a file with the same secret you enter
          in GitHub.
        </Callout>
      ) : null}
      <div>
        <strong>Last 7 days</strong>
        {counts.length === 0 ? (
          <p className="muted">No deliveries yet. GitHub sends a ping when you add the webhook.</p>
        ) : (
          <div className={styles.statusRow} aria-label="Deliveries by outcome">
            {counts.map(([outcome, count]) => (
              <Label key={outcome} variant={OUTCOME_VARIANT[outcome] ?? "secondary"}>
                {OUTCOME_TEXT[outcome] ?? outcome}: {count}
              </Label>
            ))}
          </div>
        )}
      </div>
      {webhook.recent.length > 0 ? (
        <div className={styles.tableWrap}>
          <table className={styles.table} aria-label="Recent webhook deliveries">
            <thead>
              <tr>
                <th scope="col">Received</th>
                <th scope="col">Event</th>
                <th scope="col">Repository</th>
                <th scope="col">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {webhook.recent.map((delivery) => (
                <tr key={delivery.delivery_id}>
                  <td>
                    <RelativeTime iso={delivery.received_at} />
                  </td>
                  <td>{delivery.event}</td>
                  <td className="ltr">{delivery.repository ?? "-"}</td>
                  <td>{OUTCOME_TEXT[delivery.outcome] ?? delivery.outcome}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function DeployKeyRow({ item }: { item: DeployKeyStatus }) {
  const create = useCreateDeployKey();
  return (
    <li className={box.row} aria-label={item.name}>
      <SettingsRow
        title={item.name}
        description={
          <div className={box.stack}>
            <span className="ltr">{item.repository}</span>
            <span className={box.cluster}>
              <Label variant={item.exists ? "success" : "secondary"}>
                {item.exists ? "Deploy key exists" : "No deploy key"}
              </Label>
              <Label>
                {item.git_transport === "ssh" ? "Fetches over SSH" : "Fetches over HTTPS"}
              </Label>
            </span>
          </div>
        }
        action={
          item.exists ? null : (
            <Button
              size="small"
              variant="primary"
              disabled={create.isPending}
              aria-label={`Create deploy key for ${item.name}`}
              onClick={() => {
                create.mutate(item.slug);
              }}
            >
              {create.isPending ? "Creating…" : "Create deploy key"}
            </Button>
          )
        }
      />
      {item.exists && item.public_key ? (
        <div className={styles.keyRow}>
          <CopyField label={`Public key of ${item.name}`} value={item.public_key} multiline />
          <p className="muted">
            Add it in GitHub under the repository&apos;s Settings, Deploy keys (read access is
            enough). The private half stays on the worker and is never shown.
          </p>
        </div>
      ) : null}
      {create.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(create.error)}
        </Callout>
      ) : null}
    </li>
  );
}

export function GithubSection() {
  const github = useGithubIntegration();
  return (
    <SettingsSection
      title="GitHub"
      intro="How Jendock hears about pushes and how it reads private repositories."
    >
      {github.isPending ? <Loading label="Loading GitHub status" /> : null}
      {github.isError ? (
        <ErrorCallout
          title="Could not load the GitHub status"
          error={github.error}
          onRetry={() => void github.refetch()}
        />
      ) : null}
      {github.data ? (
        <>
          <WebhookBox webhook={github.data.webhook} />
          <div className={box.box}>
            <div className={box.header}>Deploy keys</div>
            {github.data.deploy_keys.length === 0 ? (
              <p className={box.row}>
                No projects yet. <Link to="/projects/new">Create one</Link> to set up its key.
              </p>
            ) : (
              <ul className={box.list} aria-label="Deploy keys">
                {github.data.deploy_keys.map((item) => (
                  <DeployKeyRow key={item.slug} item={item} />
                ))}
              </ul>
            )}
          </div>
        </>
      ) : null}
    </SettingsSection>
  );
}
