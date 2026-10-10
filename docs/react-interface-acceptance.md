# React interface verification — 2026-10-10

## Implemented

React and Material UI replace the served legacy frontend. Source remains on main;
no worktree, new branch or release layout was created. The Python authentication,
control and deployment APIs remain authoritative. Node runs only in the image
build stage. Branch refresh belongs to the shared autocomplete control; all
confirmation dialogs have fixed title/action sections and scrollable content.
Required asterisks are hidden while field validation remains active.

## Local browser evidence

The product's existing local Compose runtime at port 19191 uses captured server
observations and real local session/settings persistence. It explicitly disconnects
production operations; these checks are not a production deployment test.

- Empty login submission: per-field validation, first invalid field focused, no
  asterisks; subsequent valid local login succeeds. Desktop and 390px rendering
  inspected after replacing the split-panel login with a centered form.
- Automatic deployment: toggled off, changed tabs, saved, reloaded and verified;
  restored to on. Draft persists between tabs and saved state persists on reload.
- Project search: one result per project and understandable no-results recovery.
- Branch autocomplete and inline reload: real local refresh handler exercised;
  commit preview retained and validation-only mode changes the submit action.
- Build rejection: disconnected runtime returns an inline recoverable error;
  no production job was queued.
- Mobile Build dialog: separate header/footer, reload inside the field, action
  buttons fit and content is contained in the dialog.
- Container details: stale captured observations disable mutations and terminals.
  Missing/invalid timestamps render as Not recorded, not Invalid Date.
- Host graphs: keyboard inspection shows sample timestamp/value, missing current
  readings stay unavailable, retained history is shown without horizontal overflow.
- Failed run detail: preserves diagnosis, renders known historical messages in
  English, and does not infer a missing historical branch from current settings.

## Automated verification

56 Python tests and 14 frontend tests pass. The Vite production build passes.
The frontend suite covers service grouping, branch freshness, build request
identity, environment drafts and validation, replacement/stale service guards,
ambiguous retries and Unicode terminal input. The current bundle emits a size
warning (approximately 1 MB uncompressed); this is not a failed build.

## Boundaries

The local preview does not establish successful production container start/stop,
environment recreation, a terminal connection or a business deployment. Do not
restart healthy applications solely to manufacture acceptance. Existing failed
business runs, external uptime checks and alert configuration are independent
of this interface migration. Production verification is recorded separately.

## GitHub Actions visual direction

The user's later correction superseded the initial visual approval. Official
Octicons now replace the Material icon package, and shared external form labels
preserve both input associations and Autocomplete popup label IDs. A light sidebar,
neutral surfaces, green execution actions and blue links apply throughout the app.
Run detail uses the actual event history with no inferred successful steps. Mobile
and dark rendering, branch reload, recovery dialog, missing history and keyboard
labels were reviewed. Three additional tests cover recorded stage grouping and
English rendering of historical failures.
