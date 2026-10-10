# Shiftcodes interface

The approved implementation uses React and Material UI. `frontend/src/main.jsx`
owns the application theme and shared layout, with `style.css` for the product's
layout rules. Container management and interactive terminal code live in dedicated
components; host telemetry has its own component. The former `web/` design is
superseded and is not copied to the receiver image.

## Visual system

A graphite desktop sidebar (#202127) anchors a white workspace. Inter is self-hosted.
Purple (#6651d1 in light mode) identifies the primary action and selection. Use
thin neutral dividers, restrained panels, and readable textual states. Runtime
state, health-check results and deployment outcomes remain separate evidence.
Dark appearance changes the workspace and Material UI palette together.

Page titles establish identity. Breadcrumbs retain ancestors on detail routes.
The main navigation becomes a drawer below 800px; operational content stays in
pages or centered dialogs. Services appear within their project, and projects
appear once regardless of available Git branches. Technical identifiers wrap or
truncate without stretching the page. Icons have accessible names and tooltips;
primary actions and the Login action retain readable text.

## Login

A quiet neutral canvas, compact brand header and centered 440px form replace the
former split marketing panel. At mobile widths the panel becomes a plain form
with 24px gutters and 16px inputs. Username and password labels stay above fields.
Required asterisks are hidden throughout the app. Submission reveals field-level
validation and focuses the first missing field. The password visibility control,
autocomplete, pending state and server errors remain available. No unsupported
password reset or alternative authentication option is advertised.

## Project workspace

A narrow summary separates the last verified deployment, saved automatic branch
and latest run. Current service image versions remain in the service list.
Overview, Services, Deployments and Settings form one project workspace. Settings
drafts survive observation refreshes and tab changes; saved values survive reload.
Only Build and Settings expose additional branch choices through MUI autocomplete.
Build previews the resolved commit and distinguishes validation from deployment.

## Container management

Dedicated pages expose observed image identity, health, mounts, environment and
terminal. Dangerous runtime changes require confirmation of the exact container.
Environment values stay masked by default; review shows changed key names. Drafts
and request IDs survive observation refreshes. Stale observations and replaced
containers prevent new mutations. Terminal output belongs to the selected live
session and is never fabricated as a design sample in production.

## Telemetry and activity

Host graphs use real timestamped samples, correct units and visible gaps. Pointer,
touch and keyboard inspection replace horizontal scrollbars. Missing data and stale
current observations are explicitly described. Deployment pages use actual events
and diagnostics, preserve failures and expose coherent recovery actions.

## Verification boundary

Visual approval, a passing build, local captured observations and a production
operation are separate evidence. Do not label all application pipelines accepted
because the monitoring interface renders. Document tested flows and remaining gaps
in the implementation acceptance record.

## Dialog anatomy

Every dialog has a separated title header, scrollable content and a fixed action
footer. Confirm, cancel and recovery actions belong in the footer. Branch refresh
is a labeled reload icon inside the shared branch picker, available in both Build
and Settings; it is not a separate footer action.
