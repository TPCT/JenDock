---
name: Shiftcodes Operate
description: Arabic deployment management workbench
colors:
  ink: "#172e36"
  muted: "#586b72"
  bg: "#f3f6f7"
  surface: "#fff"
  line: "#dce4e7"
  nav: "#152b32"
  accent: "#006e65"
  accent-hover: "#00534b"
  good: "#155d45"
  bad: "#a12c32"
  amber: "#78530e"
typography:
  display:
    fontFamily: "Noto, Arial, sans-serif"
    fontSize: "48px"
    fontWeight: 700
    lineHeight: 1.7
  headline:
    fontFamily: "Noto, Arial, sans-serif"
    fontSize: "27px"
    fontWeight: 700
    lineHeight: 1.6
  title:
    fontFamily: "Noto, Arial, sans-serif"
    fontSize: "19px"
    fontWeight: 700
  body:
    fontFamily: "Noto, Arial, sans-serif"
    fontSize: "14px"
    lineHeight: 1.75
  label:
    fontFamily: "Noto, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 600
rounded:
  badge: "4px"
  control: "6px"
  panel: "8px"
  dialog: "12px"
spacing:
  control-gap: "8px"
  action-gap: "10px"
  field-gap: "12px"
  panel-inset: "22px"
  dialog-inset: "26px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-quiet:
    backgroundColor: "transparent"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "9px 12px"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
  badge:
    rounded: "{rounded.badge}"
    padding: "3px 9px"
---

# Design System: Shiftcodes Operate

## Overview

**Creative North Star: "The deployment workbench"**

An Arabic RTL deployment workbench for authenticated operators. Ink navigation anchors a pale mineral workspace; white bordered panels hold real branch choices, execution decisions and timestamped runtime evidence. Teal identifies primary actions, while restrained amber and red communicate pending work and failure.

This is a scan of the implemented Operate direction, recorded in the first body comment of web/index.html. The user requested advanced management controls and deployment to monitoring.shiftcodes.net; this document does not imply approval of a separate aesthetic composition. It replaces the obsolete read-only, warm-neutral description. PRODUCT.md remains the product authority.

**Key Characteristics:**
- Arabic reading flow with isolated LTR repository names, branches, hashes and image references.
- Compact operational tables and bounded forms, with evidence beside decisions.
- Visible state labels and timestamps; stale observations remain visibly stale.

The source of exact values is web/style.css; workflow states come from web/app.js and web/index.html. Desktop project detail and mobile build confirmation captures in .impeccable/review were inspected for this scan. Other captures in that directory provide the review trail, not a blanket claim of functional or deployment acceptance.

## Colors

### Primary

Teal (`accent`) marks the build action, links, checked controls and completed progress segments. Its deeper hover value (`accent-hover`) makes the interaction legible without adding decoration.

### Neutral

Ink (`ink`) is primary text; slate (`muted`) carries supporting copy. Pale mineral (`bg`) separates the workspace from white (`surface`) panels. Fine cool borders (`line`) structure dense information. Deep ink (`nav`) anchors navigation, the login story and transient toast feedback.

### Semantic states

Forest (`good`), red (`bad`) and amber (`amber`) are status foregrounds, paired with pale fills and explicit labels. They are not interchangeable brand accents. Neutral badges represent other states.

**The Evidence Rule.** Every freshness claim belongs to its observed source; a successful page fetch alone does not prove that every observation is current.

## Typography

The application self-hosts Noto through NotoSansArabic-Regular.ttf, with Arial and sans-serif fallbacks. Only the regular font face is declared; heavier CSS weights use the browser's available weight behavior. Do not claim that separate bold font files are loaded.

The large display role belongs to the desktop login story. Page headings, section titles and body copy use the frontmatter hierarchy; supporting hints use 12px and table headers/status badges use 11px. Paragraphs are limited to 75ch; login story text to 38ch. Technical identifiers use LTR isolation, wrapping and tabular numerals. Code retains the browser's code font rather than an invented branded monospace family.

## Layout

At desktop size, a fixed right sidebar (226px) offsets the workspace, which has 30px top and 34px horizontal padding. Project detail uses a 1.6fr/1fr grid with a 280px minimum secondary column and 22px gap. Resource readings share a single four-column strip. White panels and tables organize evidence rather than decorative metric cards.

At 1100px and below the sidebar becomes 200px, the project grid stacks and resources become two columns. At 680px and below navigation becomes a static ink header with horizontally scrollable links; main padding is 22px 16px, tables retain a 680px minimum within their own scroll container, timeline rows stack and actions wrap. At 1600px and above workspace horizontal padding becomes 48px with a maximum width of 1800px.

Login uses equal story/form columns on desktop, a form capped at 360px, and a compact brand header plus single-column form on mobile. Build confirmation is a centered native dialog, bounded to 580px or viewport width minus 30px, with a 90svh maximum height and internal scrolling.

## Elevation & Depth

The workspace relies on surface contrast and one-pixel borders, not floating cards. The toast alone carries a small shadow (`0 6px 22px #152b3226`). Modal depth comes from a dark translucent backdrop (`#0c222c80`). Focus is a visible blue outline (3px, offset 3px); this is an interaction treatment rather than a brand accent. Buttons transition background over 0.16s ease; reduced-motion preferences remove transitions.

## Shapes

Small rectangular badges, softly rounded controls, slightly broader panel corners and a larger dialog radius form the observed shape vocabulary in the frontmatter. Inputs have a 44px minimum height; the login submit has a 46px minimum. Panels clip their contents. Selected execution-mode rows use a teal border and pale green fill; disabled controls remain visibly disabled.

## Components

- **Session login:** external labels, password visibility control, inline error and a full-width submit showing both icon and localized text. Session checking has an explicit loading state.
- **Navigation:** the ink sidebar/header holds overview, projects, build/deploy operations, services and audit; current location has a lighter ink fill and white text. Account/logout controls remain distinct from content actions.
- **Buttons and fields:** teal primary, white bordered secondary and transparent quiet variants; disabled buttons reduce opacity. External field labels and the common focus outline keep dense controls understandable.
- **Source and settings panels:** show the real repository, branch selector, resolved hash and branch fetch time. Saving the automatic deployment branch is a separate explicit action; the currently saved branch remains visible. Missing build recipes or unapproved deployment tests appear as explanatory limitations.
- **Build confirmation:** repeats project, branch and resolved commit, defaults to test-only and separately offers production deployment when available. Retry warns that the latest branch commit can differ from the failed attempt. Queue submission and cancellation have separate actions, and submission errors remain inside the dialog.
- **Execution evidence:** chronological jobs, stage strips, retry/queued-cancel actions and audit history preserve operation outcomes. Status badges always contain words. Runtime images remain separate from the selected source commit.
- **Observation status:** the global status line and source-specific labels expose timestamps and stale readings. The implementation marks observations stale when missing, older than 90 seconds, when the worker is not running, or when polling fails. Existing cached freshness labels are updated in place on failure without resetting forms; branch freshness has its own fetch timestamp and a separate 600-second launch guard. Historical job outcomes remain historical outcomes.
- **Feedback:** inline errors and notices explain failed or unavailable actions; a bottom-left toast announces brief outcomes. External uptime checks and outbound alerts remain explicitly unconfigured in the footer.

## Do's and Don'ts

- Do preserve Arabic RTL layout and readable LTR technical identifiers.
- Do keep manual branch selection distinct from the saved automatic deployment branch.
- Do show the resolved commit and execution mode before queueing a build.
- Do accompany color with explicit state text and observation timestamps.
- Do keep errors, incomplete configuration and recovery actions visible.
- Don't describe a cached observation as current after its freshness check fails.
- Don't turn a test-only result into a successful production deployment label.
- Don't reset an in-progress form just to refresh observation labels.
- Don't imply that screenshots establish production acceptance or aesthetic approval.
