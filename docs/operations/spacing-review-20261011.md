# Shared interface spacing review — 2026-10-11

Scope: existing React interface, local product runtime at 127.0.0.1:19191.
No backend or production runtime change.

Removed dialog footer padding exceptions in container confirmation and run
cancellation; removed doubled Stack margin/gap in deployment settings; aligned
Add variable with the external input label; normalized environment tab spacing,
sidebar section spacing, mobile directory inset and version pagination wrapping.
Version hint aligns centrally with its description on mobile.

Validation: frontend build passed; all 65 frontend tests passed; git diff --check
passed; Impeccable layout detector returned no findings for spacing and version CSS.
Inspected rendered project directory, project containers/versions, mobile create
container dialog, and run 27. At 390px mobile viewport main padding is 16px;
dialog title/content use 16px and footer 12px 16px. Desktop main uses 24px 24px
16px, environment tab margins 24px/0px, empty-state padding 16px. No document
horizontal overflow in measured container mobile and run desktop views.

Local captured observations are not production acceptance. Not every operational
form/error state was exercised; container Add variable alignment is source-reviewed,
not authenticated live-container workflow acceptance. Screenshot evidence:
/tmp/monitor-spacing-desktop.png (temporary local artifact).

## Current shared spacing follow-through

Normalized remaining shared gaps without changing navigation or operational actions:
empty states use 12px vertical / 16px horizontal insets; the following Versions
section uses 16px separation rather than stacking another 24px. Container headers
and rows share horizontal insets and 12px column gaps, with 8px name/action gaps.
Domain empty states and rows now follow the same panel/list rhythm. Version
metadata uses the shared tight/inline tokens; environment loading uses 16px.

Current browser pass: desktop project Containers/Versions and run 27; mobile
390x844 project Containers/Versions, Settings, project directory and Add container
modal. Measured desktop page padding 24px, mobile horizontal padding 16px,
empty-state padding 12px 16px, next Versions margin 16px, version row padding
12px 16px. No document horizontal overflow in the measured project/list/run views.
Mobile dialog content/title 16px, footer 12px 16px, width and scrollWidth 358px.
Opening/closing the dialog and navigation were exercised without submitting changes.
Other screens inherit shared styles but were not individually visually accepted
in this pass. Captured preview and synthetic version records are not production
runtime or pipeline acceptance.

75 frontend tests passed; frontend build passed with the existing bundle-size
warning; layout detector returned no findings for the three changed CSS files;
git diff --check passed. Screenshot: /tmp/monitor-spacing-final-20261011.png.
This update is local only, with no production deployment.
