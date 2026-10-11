# Shiftcodes interface

The user-selected reference is GitHub Actions, implemented in React with official
Octicons and customized Material UI control primitives. `frontend/src/main.jsx`
owns the application theme and shared layout, with `style.css` for the product's
layout rules. `github.css`, `Field.jsx` and `icons.jsx` apply the Primer-inspired
visual and control conventions. Container management and interactive terminal code live in dedicated
components; host telemetry has its own component. The former `web/` design is
superseded and is not copied to the receiver image.

## Visual system

A light sidebar and subtle gray header (#f6f8fa) anchor a white workspace. Inter is
self-hosted. Green (#1f883d) identifies primary execution actions; blue (#0969da)
identifies links and selection, with GitHub-style Octicons for runtime states. Use
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

## Execution records

Run detail follows GitHub Actions hierarchy: outcome/title, compact recorded facts,
execution sidebar and expandable stage diagnostics. Stages derive only from actual
events, not an invented successful pipeline. Earlier building/testing observations
remain Recorded. Historical records without events say that stage history is
unavailable. Validation retries preserve their mode; validated runs explicitly
offer Deploy from branch. Historical missing branches are not inferred from the
current tracked branch.

## Shared operational spacing

`frontend/src/spacing.css` owns the shared spacing scale: 8px between controls,
12px within compact rows, 16px for panel padding and form groups, and 24px between
sections. The workspace header and page share a 24px horizontal gutter on desktop
and 16px on mobile. Empty states use 16px padding and an explicit 8px gap between title and description; absent descriptions render no gap.
Tabs separate their content by 16px without an extra top gap. Dialog titles and
content use 16px vertical padding; action footers use 12px and the same responsive
horizontal gutter. Shared field labels remain 6px above inputs. Preserve 40px
icon-button touch targets while making the surrounding layout more compact.

Shared spacing exceptions: environment tabs use the same 24px desktop / 16px
mobile separation as workspace tabs, with no trailing tab margin. Dialog consumers
inherit title/content/footer gutters from the theme. Wrapping action Stacks use
flex gap instead of combining sibling margins with gap. Field/action rows align
the action with the input after the shared 20px label line and 6px label gap;
helper errors remain below the input. Mobile directory details align to the
20px icon plus the shared 8px row gap.

Spacing follow-through: form groups use 16px across dialogs. Container detail
dividers use 12px on each side; section boundaries do not combine 32px margins
with nested padding. Version headings group title, description and reload action
in one row, with 12px before the list. Settings section headings use the shared
16px heading style and keep their hint alongside the title on narrow screens.

Use 4px for closely related version metadata and helper text, 6px for label-to-input spacing, and the existing 8/12/16/24px scale for controls, rows, panels and sections. Version headings retain a dedicated reload column at narrow widths; descriptions wrap within their own column. Form helper messages align with the input edge. Compact run event spacing uses these same tokens.

The empty containers group and Versions use a 16px section margin (32px from the empty content edge including its inset). Loading groups use the same 16px vertical panel padding across deferred screens and settings. Variables action dividers use 12px on each side. Release metadata uses flex gap without extra child margins; desktop dividers keep a 16px inner gutter, removed when stacked on mobile. Version description margins are scoped to the section so MUI injection order cannot reset the 4px gap.

## Operational spacing

Use the shared 4/8/12/16/24px scale in `spacing.css`: tight metadata,
12px row insets, 16px component padding, and 24px section separation.
Mobile page gutters are 16px. Contextual hints are 32px on desktop and
40px on mobile; explicit dimensions prevent the global icon-button size
from increasing every metadata line. Group a section hint with its title,
leaving the description on its own normal text line. Version source actions
use 24px desktop line height and preserve 40px mobile touch targets.
Project tabs scroll with visible navigation controls at narrow widths.

Shared MUI Stacks use flex gap, so wrapping does not retain a leading sibling margin. Environment save/discard actions use an 8px wrapping row. Navigation uses 16px shell insets, 12px row insets, 40px desktop rows and at least 44px mobile rows.
