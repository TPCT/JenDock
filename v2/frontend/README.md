# Shiftcodes frontend

React 19 + Vite + TypeScript (strict, `noUncheckedIndexedAccess`), React Router, TanStack Query,
GitHub's design system (`@primer/react` 38.40.1, `@primer/primitives` CSS variables for light, dark and dark dimmed, `@primer/octicons-react`). Tests: Vitest + Testing Library + MSW.
Dependencies are pinned to exact versions; use `npm ci`.

The API contract (what exists, what is proposed) is in `../docs/api-contract.md`.

## Scripts

| Script                                                            | What it does                                                        |
| ----------------------------------------------------------------- | ------------------------------------------------------------------- |
| `npm run dev`                                                     | Vite dev server on :5173, proxies `/api` to `http://127.0.0.1:8000` |
| `npm run dev:mock`                                                | Same, but every API call is answered by MSW (no backend needed)     |
| `npm run build`                                                   | `tsc -b` then `vite build` into `dist/`                             |
| `npm run check`                                                   | typecheck + ESLint + Prettier check + Vitest (must pass)            |
| `npm run typecheck` / `lint` / `format` / `format:check` / `test` | the individual steps                                                |

### Real backend in development

Set `SHIFTCODES_PUBLIC_ORIGIN=http://localhost:5173` for the API: it requires `Origin` to equal
that value exactly on login and every mutation (the Vite proxy forwards the browser's Origin).
Use an `http://` origin so the plain (non-`__Host-`) cookie name is used.

### Mock mode

`npm run dev:mock` registers a service worker (`public/mockServiceWorker.js`, copied from MSW;
it is also copied into `dist/` and is harmless there). Fake credentials: user `admin`, password
`mock-password-12345`; user `locked` triggers the 429 message. The data covers every state:
running run with parallel stages and a streaming log, succeeded, failed, cancelled, queued and
interrupted runs, a project without runs, 42 runs for paging, an unknown branch (`missing`) for
the manual-run dialog. Service workers are unavailable in some embedded browsers; the page then
shows a message instead of a blank screen.

The same handlers back the Vitest suite (`src/mocks/server.ts`); `EventSource` is replaced by a
test double (`src/test/fakeEventSource.ts`) because jsdom has none.

## Pipeline editors

`/projects/:slug/environments/:env/pipeline` has a Visual and a Text tab over one buffer (the
Jenkinsfile text). The browser never parses Jenkinsfiles: parse/render/validate and the step
catalogue come from the server (`../docs/api-contract.md`, "Pipelines").

- Visual: stage cards (reorder with the arrow buttons or Alt+Up/Down in the name field), parallel
  branches, steps and forms generated from the catalogue, `when`/`post` presets, agent picker,
  variables and secret-name picker. Every edit is rendered by the server into canonical text.
- Text: CodeMirror 6 with Groovy highlighting, debounced server diagnostics as gutter markers,
  underline and a clickable problem list.
- Save sends `base_version`; a 409 offers loading the other version, keeping your edits on top
  of it, or comparing. Unsaved changes are guarded (router blocker + `beforeunload`).
- `source: "repository"` makes both tabs read-only with a banner; history stays visible.
- CodeMirror injects `<style>` tags, which the API's CSP (`default-src 'self'`) blocks, so the
  editor is mounted inside a shadow root where it adopts constructed stylesheets instead. Theme
  colours are CSS variables (`--syn-*`, `--log-*`) and cross the shadow boundary.
- The editors are code-split (`React.lazy`), so the rest of the app does not pay for CodeMirror.

Mock mode: the mock server "renders" models with a small serializer and remembers the text it
produced, so `parse` only understands texts it rendered (plus fixtures). Markers in the text
for trying states: `FIXME` (error diagnostic with position), `TODO` (warning), `SYNTAX ERROR`
(parse returns no model), `INVALID` (422 on save). `marketing-site` development is a
repository pipeline (read-only), `billing-worker` has none (starter), `shop-api` is editable.

## Layout

- `src/api`: typed fetch client (credentials, `X-CSRF-Token`, error envelope, 401 redirect),
  TanStack Query hooks, `useRunStream` (SSE).
- `src/lib`: formatting, ANSI colours, theme, `LogStore` (offset-safe log buffers).
- `src/components`, `src/pages`, `src/styles` (`global.css` page glue, `box.module.css` = GitHub's Box look).
  Primer CSS variables come from `@primer/primitives` (imported in `main.tsx`); the colour mode is the
  three `data-color-mode` / `data-light-theme` / `data-dark-theme` attributes on `<html>`
  (`public/theme-init.js` applies the saved choice before first paint, `src/lib/theme.ts` changes it).
- `src/mocks`, `src/test`.

## Serving the production build (what the backend team must add)

`npm run build` produces static files in `dist/` (`index.html`, hashed `assets/*`,
`theme-init.js`, `favicon.svg`). Nothing is inline, so the existing CSP
(`default-src 'self'`) works as is; fonts are system fonts and no external hosts are used.

1. The frontend Docker build stage (multi-stage; not edited here) runs `npm ci && npm run build`
   and copies `dist/` into the api image, e.g. `/app/frontend`.
2. The API serves it: `GET /assets/*` and the other files from that directory with long
   cache headers for hashed assets, and **an SPA fallback**: any `GET` that is not under `/api`
   or `/healthz` returns `index.html` (the router owns `/login`, `/projects/...`, `/runs/...`).
   Unknown `/api/*` paths must keep returning the JSON error envelope, never `index.html`.
3. The current `SecurityHeadersMiddleware` sends `Cache-Control: no-store` on everything; for
   `/assets/*` it should send `public, max-age=31536000, immutable` instead.
4. Everything is same-origin, so no CORS is needed.

## Content-Security-Policy

Production sends `default-src 'self'` (no `unsafe-inline`). Primer React 38 is CSS Modules only (no
styled-components, no runtime `<style>`); Vite emits all of it as `assets/*.css`. React's `style` prop
and CodeMirror's `style.cssText` are CSSOM writes, which the policy allows.
`src/test/csp.test.ts` builds the app and fails if `index.html` gets an inline style/script, or if a
bundle gains a `<style>`-creating code path not on its documented list. Known, guarded paths:
React hoistable `<style>` (unused), CodeMirror's `<style>` fallback (editor lives in a shadow root,
which uses constructed stylesheets), `@oddbird/popover-polyfill` (only where the Popover API is
missing; uses constructed stylesheets, `<style>` only in browsers with neither), and
`@primer/live-region-element`, whose shadow template contains a `<style>`: it is created only by
`announce()`, which nothing the app renders calls (`vitest.setup.ts` fails any test that creates a
`<live-region>` or a document `<style>`). Do not use `Button loading`, `Textarea characterLimit`,
`TextInput characterLimit`, `ToggleSwitch`, `SelectPanel`, `TreeView` or `Autocomplete` without
re-checking that.

## Log timestamps

The log viewer shows timestamps on demand when lines start with an RFC 3339 timestamp (as
`docker logs -t` writes). Logs without them leave the toggle disabled.

## Run actions

"Re-run" starts a new manual run of the same branch and commit (`POST .../runs`); the server may refuse
with 409 if the commit left the branch.
