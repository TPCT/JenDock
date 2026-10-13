# Shiftcodes frontend

React 19 + Vite + TypeScript (strict, `noUncheckedIndexedAccess`), React Router, TanStack Query,
Primer-like token CSS (light/dark), official Octicons. Tests: Vitest + Testing Library + MSW.
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
- `src/components`, `src/pages`, `src/styles` (`tokens.css` = design tokens for both themes).
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
