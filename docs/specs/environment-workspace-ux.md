# Environment-scoped project workspace

Status: implementation contract proposed on 2026-10-10; runtime ownership API and adoption verification are prerequisites. This document incorporates the owner's latest correction and supersedes the separate generic Settings/Environments layout and manual “Add service allocation” resource workflow.

## User workflow

Open a project, choose **Development** or **Production** once in its header, then work within that environment. The selected environment stays visible beside every deployment and container action. Projects themselves have no environment classification.

Tabs: **Overview**, **Containers**, **Deployments**, **Settings**. Existing detail pages, terminal, logs, image history, variables, mounts, operation recovery and run diagnostics remain available. Further tabs are added only when their actual workflows exist.

- Overview shows the selected environment's recorded state, branch, latest verified deployment and recent activity. Release summaries appear here only.
- Containers lists actual containers with verified ownership in the selected environment. Each row provides its current state, image identity, RAM/CPU usage and an **Edit limits** action. Clicking the container preserves the existing detail and terminal flow.
- Deployments shows runs with an immutable target matching this project and environment. A run without verified environment history is shown in explicitly labeled legacy history, never assigned using today's branch or project settings.
- Settings edits the selected environment's branch and automatic deployment preference. A single page owns these settings; there is no competing project-level branch form.
- Project-level identity and any combined resource budget are explicitly labeled **Project** and do not look environment-scoped. They are supplementary settings, not another competing deployment editor.

Use the established GitHub-style white/neutral surfaces, compact controls and floating branch menu. Keep essential status/errors visible. Put supplementary explanations in accessible information-icon hints. Use **Containers** for observed runtime objects; retain “Compose service” only when that technical distinction affects an operation.

## Direct container limits

Edit limits opens a centered dialog for an existing verified container. It displays the project, selected environment, container name/ID and observed limits. Inputs are **RAM (MiB)** and **CPU cores** only. No container-picker, add-allocation step, storage quota or guessed ownership is involved.

The review distinguishes current runtime values, saved desired values and unapplied changes. The primary action names the actual operation: **Save limits** for metadata only or **Apply limits** for verified recreation. If saving is a prerequisite, the UI performs it as an explicit step and retains the successful save if runtime application fails. A retry must not silently mint a duplicate runtime operation.

The confirmation includes the real impacts: container recreation, possible interruption, disabled swap, preserved running/stopped state and memory exhaustion risk. Updates use immutable container identity, observed configuration revision, environment binding revision and policy revision. A stale/replaced/moved target requires refreshed review. Successful application rereads the runtime limits; an HTTP success alone is not enforcement evidence.

Saved project/environment budgets validate totals. They do not reserve host capacity or establish an aggregate cgroup hard cap. Editing a container never creates or changes its environment ownership as a side effect. Preserve the current backend service identity mapping internally while the UI presents containers.

## Ownership and legacy workloads

The reported 15-production/5-development inventory is an investigation input, not verified binding data. Do not infer ownership from a name prefix, branch, APP_ENV or that historical count.

Adoption must inspect actual Docker labels and container IDs, Compose project/files/service membership, immutable image identity, database endpoint identity and persistent volume associations. Compare the complete observed workload to the intended environment; surface conflicts and shared resources. Persist the verified association and provenance only after the adoption workflow's explicit decision. Do not start a duplicate deployment or move data during adoption.

Unassigned existing containers remain visible through an explicit **Existing containers · environment unassigned** section/link with their existing guarded management actions. They must not appear as members of either environment. Existing history also remains reachable. Deployment actions never silently fall back from the selected environment to the legacy recipe.

An unprovisioned environment shows **Not provisioned**. Missing observations show **Status unavailable**, with retry. An unassigned workload shows **Environment unassigned**. These are distinct from an empty, successfully observed container list. Preserve functional container management; do not replace the existing detail page with a generic unavailable screen.

## Required server contract (proposal for backend agreement)

Provide one sanitized workspace read, for example `environment-workspace` with `{project, environment}`. It must return:

- Project identity and the requested environment record/revision; branch, automatic preference and recorded provisioning state.
- Binding status (`verified`, `unassigned`, or `unprovisioned`), binding revision and sanitized verification provenance/timestamp.
- Observation timestamp, freshness/error state, and verified member containers. Each includes immutable ID, name, Compose service identity, image/commit, runtime state/health, RAM/CPU observations and current limits.
- Saved desired limits and policy revision, keyed by stable binding/Compose service identity with the observed container ID attached. Names alone are not authorization.
- Server capabilities for deploy, validate, edit settings, operate container, edit limits and terminal, with actionable disabled reasons.
- Matching environment run targets, or an API/filter contract that returns them without deriving ownership client-side.
- An explicit legacy/unassigned inventory reference or summary so existing management and history remain discoverable.

Deployment requests include the chosen environment and its reviewed revision/recipe target. Container/resource mutation requests include project, environment, binding revision, immutable container ID, configuration revision and a stable request ID. The server verifies membership and capability again immediately before mutation. It must reject a legacy target or the other environment even when its name matches an existing project prefix.

Workspace responses contain no database passwords, environment variable values, recipe contents or credential material. Database identity evidence must be sanitized. Container environment-variable access retains the existing dedicated authenticated path.

## Actual source gaps at this checkpoint

- `registry.py` stores both environment preference records and an optional `legacy_binding`. Its internal adoption operation is not evidence of a verified Compose/database identity workflow. Public settings mutations intentionally cannot adopt a workload.
- `server.py` exposes project-level snapshots and registry preferences; a verified environment-scoped container-list/workspace endpoint is not present at this checkpoint.
- `control.py` already inspects actual container and Compose ownership and rejects stale IDs/configuration. Its public details have no verified environment binding/revision, and `environment` currently means process variables, not Development/Production.
- `resource_policy.py` stores environment allocations selected by the operator. That document is a budget policy, not runtime ownership evidence. The current `Resources.jsx` manually assigns observed container names to environments; it is transitional and must not be presented as the finished workflow.
- `job_target.py` now captures immutable environment/recipe targets, including `legacy-unclassified`. The workspace still needs a public list/filter contract that preserves that distinction and handles older runs without targets.
- `EnvironmentSettings.jsx` owns an internal environment switch. It must become controlled by the workspace selection rather than introduce a second switch. Its persistence and revision guards remain reusable.

## Proposed React boundary

`ProjectWorkspace({ projectId, environment, onEnvironmentChange, api, navigate, openBuild, setDirty, setOperationPending })` owns rendering and environment-keyed draft caches. `environment` is route-controlled and restricted to `development`/`production`; a refresh/direct link restores it. Switching environments preserves independent drafts and scroll context. Pending mutations prevent target changes until their outcome is resolved.

`openBuild({projectId, environment, environmentRevision, recipeRevision})` receives explicit reviewed target context. It must never accept only a project ID for this flow.

The workspace consumes server-provided member lists and capabilities. It does not filter global containers by names. Existing ContainerPage and RunDetail are reused with explicit originating project/environment context; unassigned targets retain an explicit unassigned origin.

Implement after agreeing the workspace response. First preserve existing routes and add the verified data contract, then compose the new page, move settings under the selected environment, replace manual resource allocation with per-container editing, and retire only the superseded UI paths after parity checks.

## Acceptance

Inspect desktop and mobile in both environments. Verify branch/settings isolation, draft retention across tabs and environment switches, saved reload, stale revisions, existing container limits, apply/recreation recovery, immutable identity rejection, unauthorized cross-environment requests, legacy visibility and terminal continuity. Exercise real persistence and observed runtime results where authorized. Record untested provisioning/adoption/deployment paths explicitly; a rendered new page is not environment acceptance.
