# Notifications

Jendock tells you when something needs attention: **run failed**, **run succeeded after a failure**
(`run.fixed`), **deployment succeeded**, **deployment rolled back / rollback failed** (critical),
**run interrupted**, **certificate expires within 14 days / renewal failed**, **webhook signature
failure spike** and **disk usage above a threshold**.

- **Channels** (Settings, Notifications, admin): a Slack incoming webhook, a generic webhook (JSON,
  Microsoft Teams and Discord compatible through the `text`/`content` fields, with an optional HMAC
  signature header `X-Jendock-Signature: sha256=...`) and email over SMTP (host, port, `STARTTLS`,
  `TLS` or none, username and password). Every channel has **Send test**.
- **Rules**: a *default* rule in Settings, Notifications; each project (Project settings,
  Notifications) can follow it or choose its own channels and events, and so can each environment.
  The most specific scope with rules decides; an empty customized rule mutes a channel there.
  `webhook.signature_spike` and `disk.usage_high` only exist in the default rule.
- **Delivery history** with status, attempts and errors, in Settings and per project.
- **Quiet duplicates**: the same event for the same resource within `JENDOCK_NOTIFY_DEDUPE_MINUTES`
  (30) collapses into one message per channel.

## How it works

Emission points write rows into a small **outbox** table (`notification_deliveries`) in the same
database: the run executor (finished runs, and interrupted runs found at worker start), the
deployment ledger (every final deployment status, including crash recovery), the certificate renewal
loop, the webhook signature check and a disk monitor thread. A dispatcher thread in the **worker**
sends them, so notifications never block a run. A failed attempt is retried with exponential backoff
(30 s, 1 min, 2 min... capped at 1 h) up to `JENDOCK_NOTIFY_MAX_ATTEMPTS` (7) times; a row claimed by
a worker that died is picked up again after 5 minutes. Finished rows are pruned after
`JENDOCK_NOTIFY_HISTORY_DAYS` (30).

Messages contain a link to the run or domain page (built from `JENDOCK_PUBLIC_ORIGIN`), the commit's
short SHA, who or what triggered the run, the failing stage and step, and nothing else from the logs
than the failing step's **last 20 lines**, already masked when the run wrote them and masked again
here.

## Secrets

Webhook URLs, signing secrets and SMTP credentials are encrypted with the worker's key
(`secret.key`) in the `notification_channels` table. They are **write-only**: the API forwards them
to the worker (`notify.channel.*` RPC) and never returns, logs or audits them; the UI only shows
that a channel is configured and the last 4 characters of a webhook URL. Delivery errors are short
reasons (`the endpoint answered HTTP 404`, `SMTP authentication failed`), never a URL. Redirects of
webhook endpoints are not followed.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `JENDOCK_NOTIFY_ENABLED` | `true` | `false` stops queueing and sending (channels can still be configured) |
| `JENDOCK_NOTIFY_DEDUPE_MINUTES` | `30` | quiet period of duplicates (certificate alerts use at least 24 h, disk and renewal failures 6 h) |
| `JENDOCK_NOTIFY_MAX_ATTEMPTS` | `7` | delivery attempts before a message is marked failed |
| `JENDOCK_NOTIFY_RETRY_BASE_SECONDS` | `30` | first retry delay, doubling up to 1 h |
| `JENDOCK_NOTIFY_SEND_TIMEOUT_SECONDS` | `10` | timeout of one HTTP or SMTP delivery |
| `JENDOCK_NOTIFY_HISTORY_DAYS` | `30` | how long finished deliveries are kept |
| `JENDOCK_NOTIFY_DISK_THRESHOLD_PERCENT` / `_DISK_CHECK_SECONDS` | `90` / `300` | disk alert threshold and check interval |
| `JENDOCK_NOTIFY_WEBHOOK_FAILURE_THRESHOLD` / `_WINDOW_MINUTES` | `20` / `10` | invalid signatures that make a spike |

Endpoints and payloads: `docs/api-contract.md`, "Notifications". Per-user email subscriptions can
build on the same model later: a user is just another channel kind (`email` to that user's
address) and a rule is already "scope x channel x events".
