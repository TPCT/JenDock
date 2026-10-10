---
name: Shiftcodes Deployment Console
description: An English monitoring and deployment workspace with host measurements and operational evidence.
colors:
  primary: "#5746cf"
  primary-hover: "#4637ad"
  selected: "#eeebfb"
  canvas: "#f6f7f9"
  surface: "#ffffff"
  ink: "#1c202b"
  muted: "#656c7b"
  line: "#e5e7ed"
  focus: "#aba1f0"
  good: "#237252"
  good-surface: "#eaf6ef"
  bad: "#b13449"
  bad-surface: "#fceff1"
  pending: "#8b610d"
  pending-surface: "#fbf4e5"
  neutral: "#616875"
  neutral-surface: "#f3f3f6"
typography:
  headline:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 670
    lineHeight: 1.25
    letterSpacing: "-0.04em"
  title:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 650
    letterSpacing: "-0.02em"
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "14px"
    lineHeight: 1.55
    letterSpacing: "-0.01em"
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 600
  terminal:
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace"
    fontSize: "14px"
rounded:
  badge: "5px"
  input: "6px"
  control: "8px"
  inset: "10px"
  panel: "12px"
  dialog: "14px"
spacing:
  tight: "8px"
  compact: "12px"
  standard: "16px"
  roomy: "20px"
  section: "24px"
  broad: "28px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
  navigation-current:
    backgroundColor: "{colors.selected}"
    textColor: "{colors.primary}"
    rounded: "{rounded.control}"
    padding: "12px 16px"
  badge-good:
    backgroundColor: "{colors.good-surface}"
    textColor: "{colors.good}"
    rounded: "{rounded.badge}"
    padding: "4px 8px"
  catalog-card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
    padding: "22px"
  search-input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "9px 12px"
---

# Design System: Shiftcodes Deployment Console

## Overview

**Creative North Star: "The Project Workspace"**

The console is a restrained English operational workspace. White navigation and white content surfaces sit on a cool neutral canvas; indigo identifies deliberate actions and the current location. Fine borders, compact typography and generous space between groups organize dense technical evidence without turning the console into a decorative dashboard.

This is a code-led system for the monitoring and deployment tool, extracted from its current implementation. Design documentation describes interface behavior; it does not establish deployment or operational acceptance. The cascade loads `web/style.css` before `web/product.css`; the latter owns the current world. `web/index.html`, `web/app.js`, `web/monitoring.js`, `web/controls.js` and `web/terminal.js` supply the implemented component and interaction evidence. Direction provenance is FORM `710ac72e`, grounded direction 4. The prior Arabic/teal visual world is superseded by the authorized replacement.

**Key Characteristics:**
- English LTR interface with readable technical identifiers.
- White sidebar, cool neutral canvas and flat bordered surfaces.
- Indigo actions and selection; explicit text alongside status colors.
- Sampled host telemetry, responsive project workspaces and contextual execution evidence.
- SVG line icons and a distinct dark interactive terminal.

## Colors

The palette pairs a restrained indigo accent with cool neutrals and small semantic status fills.

### Primary
- **Action Indigo** (`primary`, `primary-hover`) marks primary actions, active tabs and linked actions.
- **Selected Lavender** (`selected`) fills current navigation and selected listbox options; **Focus Lavender** (`focus`) identifies keyboard focus.

### Neutral
- **Cool Canvas** (`canvas`) separates the workspace from **White Surface** (`surface`) navigation, panels and controls.
- **Graphite Ink** (`ink`) carries titles and primary reading; **Slate Text** (`muted`) carries supporting evidence. **Fine Divider** (`line`) organizes surfaces without heavy framing.
- **Neutral Status** (`neutral`, `neutral-surface`) is appropriate for descriptive runtime state that does not establish health.

Semantic pairs `good`, `bad` and `pending` with their respective surface tokens distinguish successful, failed and waiting states. They are evidence colors, not additional brand accents. Charts also reuse indigo for CPU/receive, green for memory/transmit and amber for disk; in charts these colors identify labeled series and do not indicate health thresholds by themselves.

**The Evidence Color Rule.** Every status color accompanies a readable state label; a running service and a verified deployment remain different claims.

## Typography

**Headline and Body Font:** Inter, locally loaded as a variable face, with system-ui and sans-serif fallbacks. **Terminal Font:** the platform monospace stack recorded in the frontmatter. Technical hashes also use monospace where the implementation distinguishes them from prose.

The type ramp is compact and functional. The headline role leads each screen; section titles, medium-weight project identities, body copy and quieter metadata step down beneath it. There is no separate decorative display face. The larger login statement is a surface-specific composition, not a second global type system.

On narrow screens the page headline becomes slightly smaller (27px), while body copy grows (15px), hints and notices grow (14px), and operational metadata and badges grow (13px). Search and environment inputs use 16px text on mobile. Desktop metadata remains compact; its smallest existing values are not a recommended minimum for future screens.

**The Identity Before Metadata Rule.** Project and task names carry the strongest type within their group; repository, branch, timestamps and hashes support that identity.

## Layout

The desktop shell has a fixed white sidebar (236px). Main content offsets by the same width, uses a maximum width of 1660px and starts with 34px vertical and 36px horizontal padding. Identity, observation state and the relevant action precede execution detail. Breadcrumbs retain project and service context.

The project catalog uses three columns by default, four from 1550px, two through 1180px, and one through 680px. Catalog gaps are 18px on desktop and 14px on mobile. Service cards use two columns, reducing to one at the mobile breakpoint. Content groups typically use the frontmatter spacing rhythm with 20–24px internal padding; thin dividers separate supporting facts.

At 680px and below, the sidebar becomes an off-canvas navigation drawer, with a mobile brand/menu bar and 16px content gutters. Project tabs stay horizontally accessible. Data tables become labeled vertical records. Run facts retain two columns while execution steps and major action groups stack. Dialogs scroll within the viewport; build confirmation actions remain visible in a sticky footer. The environment form's save action is in normal document flow.

Host summary metrics form four columns, reducing to two at 1000px. Monitoring charts use two columns with 18px gaps and become one column at that same breakpoint. Deployment issue cards use two columns and stack at 680px. Chart padding reduces from 22px to 16px on mobile; metric values use tabular numerals and reduce from 28px to 24px. These panels share the existing flat surface and corner vocabulary.

## Elevation & Depth

Ordinary panels and catalog cards are flat white surfaces with one-pixel borders. Depth belongs to temporary overlays: searchable option popovers use `0 12px 30px #2020301a`, and dialogs use `0 24px 80px #17152c35` with a translucent backdrop (`#17203170`). The mobile navigation backdrop is a separate translucent veil (`#1a193b66`).

**The Flat Workspace Rule.** Keep routine content flat; reserve elevation for content temporarily layered above the workspace.

Focus uses a visible outline (3px, offset 3px). Buttons transition background over 0.16s ease, while the mobile drawer slides over 180ms ease-out. Reduced-motion rules remove transitions and smooth scrolling.

## Shapes

Use softly rounded rectangular controls, broader panel corners and slightly larger dialog corners, as recorded in the frontmatter. Status chips are compact rounded rectangles rather than pills. Circular marks are native to observation dots and numbered execution steps; their shape communicates state or sequence. Inputs have a 44px minimum height. Most buttons inherit that minimum; compact mode controls are a contextual exception, not a new default touch target.

## Components

### Buttons
Indigo filled primary buttons lead explicit operations. Secondary buttons are white with a fine border; quiet actions use transparent surfaces. Destructive actions use the failure color. Buttons retain readable text, SVG icons where present, visible keyboard focus and reduced opacity when disabled. The login submit includes both icon and text. Pending operation confirmations display their progress and block conflicting actions.

### Chips
Compact text badges pair semantic foreground and background colors. Runtime descriptions may remain neutral. Run outcomes retain their own status independently of the latest container observation. Mobile badge text is enlarged with the rest of the evidence.

### Cards / Containers
Catalog cards and service cards share flat white surfaces, fine borders and panel corners. Catalog cards group identity, source, service counts, latest run and an explicit open action; their bottom action aligns consistently through flexible card content. Technical strings wrap or truncate within their own group instead of stretching the grid.

### Inputs / Fields
Search inputs and searchable select triggers use control corners; basic text inputs retain the smaller input corners. Shared field wrappers keep associated external labels above controls, with an 8px gap; toolbar and filter consumers follow the same pattern. Mobile field labels increase to 14px. Placeholders supplement labels rather than replacing them. Searchable listboxes have a separate query field, highlighted active option, selected state and an explicit no-results message. Environment values are masked until revealed, and edited values have a visible draft state. Mobile input text is enlarged without shrinking neighboring actions. Settings retain the chosen branch and automatic-deployment draft across observation refreshes, with explicit “Unsaved settings” or “All changes saved” text. A selected branch that disappears remains identified as unavailable and disabled; build submission stays disabled until an available branch resolves to a commit.

### Navigation
The white sidebar uses muted text and SVG line icons. The current destination has a lavender fill and indigo text. Hover adds a faint neutral-lavender fill. Mobile uses the same navigation in a drawer, with a close control and backdrop; breadcrumbs remain in the content. Project and service tabs use an indigo underline instead of a second navigation surface. Tab controls support arrow/Home/End navigation. Background observations preserve the focused service action and open edits; replaced or unavailable service records add a warning and an explicit reload path rather than silently swapping the active form.

### Host measurements and history
The overview shows CPU usage, memory, disk and combined network traffic alongside sample freshness, host uptime, swap and container counts. Current readings, historical samples and unavailable data have distinct presentations. Missing CPU or network deltas say “Measuring…”; missing host observations explain that collection has not started. Stale or failed observations replace current metric values with a dash and identify the last observation time; retained charts are explicitly historical and visually subdued.

History controls select 15m, 1h or 24h, with indigo indicating the pressed range. Graphs use actual timestamped samples; missing values and sampling gaps break the line rather than generating continuity. Fewer than two real samples produces an explanatory collecting state. Percentage charts use a 0–100 scale; network throughput uses a data-scaled rate axis. Each chart has labeled series, units, timestamps and a keyboard-operable sample slider. Inspecting a sample exposes “Resume live graphs” so historical scrutiny and live updates are deliberate states. Threshold alerts appear as textual notices with a service inspection link.

**The Measured History Rule.** Draw only observed samples, label missing or stale measurements, and keep historical inspection visibly distinct from current readings.

### Deployment issues
Issue cards group the project link, recorded status, failure or waiting explanation, run number, branch and update time. The “Review run” action leads to its execution evidence and recovery context. Pending and failed work stays visible alongside host health; resource charts do not substitute for deployment outcomes.

### Execution evidence and terminal
Run summaries place outcome, source facts and actions before an ordered progress sequence and chronological activity. Waiting, failed and completed states remain visible. The terminal is an intentionally dark surface containing a monospace interactive session, connection status and explicit connect/disconnect controls; mobile exposes a supplementary key row. This dark tool surface is compatible with the otherwise light console.

### Dialogs and feedback
Build dialogs show source and execution-mode choices before confirmation. Retry re-resolves the selected branch and warns that the latest commit may differ from the source run. Retrying an unsuccessful validation run retains validation mode; an unavailable original branch requires an explicit replacement choice. Selected mode rows use a lavender-tinted surface and border. Guarded operations have contextual confirmations and inline errors. Notices use a subdued amber surface. Brief outcome toasts use a dark ink surface with white text and the existing low ambient shadow (`0 6px 22px #152b3226`), distinct from inline errors. When a preview context is used, its notice distinguishes captured observations and proposed settings from connected operations; preview styling is not the identity or scope of the monitoring tool.

## Do's and Don'ts

### Do:
- **Do** preserve the white sidebar, cool canvas and indigo action hierarchy.
- **Do** pair status colors with state text and relevant observation timestamps.
- **Do** retain project identity and source context through service and run views.
- **Do** enlarge supporting evidence on mobile and keep forms within the viewport.
- **Do** preserve visible labels, focus indicators and recoverable inline errors.
- **Do** retain settings drafts and sample-inspection context during background observations.
- **Do** distinguish current measurements, historical samples and missing data with explicit text.

### Don't:
- **Don't** restore the superseded Arabic/teal identity as the default console world.
- **Don't** use a healthy-looking color to imply a deployment or health check that the evidence does not establish.
- **Don't** turn routine cards into floating layers or make the main navigation a content modal.
- **Don't** replace SVG interface icons with decorative glyphs.
- **Don't** invent history, bridge missing samples, or present stale readings as live.
- **Don't** treat visual documentation as deployment or production acceptance.

Not canonized: very small legacy desktop metadata is a legibility concern, not a minimum for future monitoring surfaces. This document records the current design direction and does not establish production acceptance.

## Operational consolidation — 2026-10-10

Each measurement appears once: its current reading is attached to its historical
chart. Sample inspection is an explicit inline action, with a keyboard slider and
Resume live; idle inspection controls no longer dominate every graph. One host
freshness warning applies to the entire chart surface. The global sidebar remains.

Overview owns host telemetry, fleet links, active work and unresolved deployment
runs. It no longer embeds a partial duplicate project catalog or an empty run table.
Projects owns the complete searchable directory. Project names are the primary
entry points; separate Open project links and decorative repeated icons are gone.
Services uses a grouped directory, and project run history lives in its Runs tab.
No build, settings, service management, history or recovery action was removed.
Root screens use their title without repeating it in a breadcrumb. Detail routes
retain ancestor breadcrumbs. Deployment summaries link to full diagnostic records.
