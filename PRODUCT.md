# Product
<!-- impeccable:product-schema 1 -->
## Platform
web
## Stack
Python service and plain HTML/CSS/JavaScript, chosen to implement the user's approved lightweight deployment service with minimal VPS overhead.
## Users
The owner of the TPCT production VPS, managing existing Docker Compose applications and approved Git branches.
## Product Purpose
Operate deployment through an English management dashboard on monitoring.shiftcodes.net: session login, real GitHub branch selection, test-only builds, approved production deployment, retry, queued cancellation, persisted audit history, and timestamped runtime evidence.
## Operating Context
GitHub push webhooks, one build at a time, production VPS with 6 CPUs and 8 GB RAM, 88 existing containers. User approved proceeding with the previously described monitoring and deployment flow.
## Capabilities and Constraints
Signed webhooks, allowlisted repositories and branches, tests before deployment, health checks, rollback on failure, targeted cleanup afterwards. Preserve current branches, databases, project Compose groups and S3 settings. No GitHub Actions, Jenkins, worktrees or release-directory layouts. No fabricated successful deployments. External uptime monitoring and outbound alert channel are not configured yet.
## Product Principles
Show live evidence and timestamps. Distinguish published code from running code. Keep failed deployments and recovery visible. Restrict powerful actions to authenticated operators. Bound log retention and build resources.
## Accessibility & Inclusion
English LTR, keyboard navigation, responsive desktop and mobile layouts; readable repository names and hashes in LTR.

## Accepted management workflow
The user rejected a read-only monitor and asked for a complete control surface. Manual branch selection is distinct from the saved automatic deployment branch. Every launch previews the resolved commit and distinguishes test-only from production changes. Sessions use secure HTTP-only cookies and CSRF protection. Projects without a validated deployment recipe must state that limitation rather than expose a misleading deploy control.

Projects appear once with their tracked branch. Additional branches are available only in the build dialog or project settings. Every project owns one workspace grouping all its containers. Live server events update observations without manual refresh.

## Container management expansion
Each project exposes guarded start/stop/restart, real image versions and commits,
and a masked environment editor with persistent per-service overrides and recovery.
Mobile navigation is a sidebar with breadcrumbs. Select components are searchable
custom listboxes. Runtime changes arrive automatically; forms retain edits.
Container management is not host, database account or filesystem administration.
