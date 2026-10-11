# Version history and shared spacing acceptance — 2026-10-11

Local implementation on the existing main checkout. No production deployment in this slice.

## Version history

Authenticated `service-versions` is scoped to project and source environment with services:read authorization. Stable cursor pages expose immutable source run/commit/image identities and current recorded service associations. They do not assert live image availability or runtime state. Corrupt or missing artifact/source/definition evidence produces an explicit integrity error, not an empty history. Independent review findings for orphan pagination, malformed scope and missing definitions were repaired and regression-tested.

The local preview's existing `deploy-monitor-ui` test project contains 27 explicitly synthetic persisted records; these were not actual builds. Browser verification used the real HTTP action and SQLite records: initial 25 rows, Load more to 27 without duplication, source run 27 opened with its synthetic-fixture diagnostic, Production showed a separate empty history. Actual managed build/rollout acceptance remains documented in earlier Docker fixture reports.

## Shared spacing

Shared CSS tokens align desktop gutters at 24px and mobile gutters at 16px. Sections use 24px, panels/form groups 16px, compact rows 12px and controls 8px. Shared empty states reduced from 40px vertical to 16px. Dialog title/content/footer, field labels, environment settings, resources, project/service creation, run details and list rows use the same spacing roles. Mobile header actions stay beside the title. Removed the accidental literal zero from the empty existing-workload conditional.

Rendered desktop inspection: project Containers and Versions, Settings, project directory, overview, run detail. Mobile 390x844: project Containers/Versions, Settings navigation and source/build dialog. Dialog content padding 16px; footer visible; dialog scrollWidth equalled clientWidth (358px). Final project document scrollWidth equalled clientWidth (383px excluding scrollbar). Close/navigation remained functional. No form configuration or production data was changed during this spacing review. Other overlay combinations were source-reviewed through shared styles but not individually opened.

Screenshots:
- `/tmp/monitor-spacing-desktop-20261011.png`
- `/tmp/monitor-spacing-mobile-20261011.png`
- `/tmp/monitor-spacing-dialog-mobile-20261011.png`

Validation: 414 backend tests, 52 frontend tests passed; Vite build passed. Existing SQLite ResourceWarnings and the main JS bundle size warning remain. These results do not close broader platform requirements or constitute production acceptance.
