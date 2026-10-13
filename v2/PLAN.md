# Jendock Platform v2 — Plan

Jenkins-like CI/CD on the same VPS. GitHub push → pipeline (Declarative Jenkinsfile) →
Docker images → deploy. Full container control and per-container terminal. Development
and Production environments, database inspector, Apache/Cloudflare ingress, MCP for agents.

## Decisions

- **Pipeline format:** Jenkins Declarative Pipeline (`Jenkinsfile`) subset. No free Groovy
  (`script {}`), no Jenkins plugins. Platform steps (`dockerBuild`, `deploy`, `healthCheck`)
  are written as Jenkins shared-library-style calls, so the syntax stays valid Jenkins.
- **Pipeline source:** `Jenkinsfile` in the repository at the built commit wins. Otherwise
  the pipeline stored in the platform (edited in the GUI, visual or text). Every run records
  which source and the exact pipeline text it used.
- **No framework knowledge:** the platform knows nothing about migrations, Laravel, Alembic,
  etc. Users add `sh 'php artisan migrate --force'` as a step if they want it.
- **Execution safety:** every `sh` runs inside a Docker container, never on the host.
  Built images run with their own WORKDIR; external images (e.g. `node:22`) get the checkout
  mounted at `/workspace` (Jenkins docker-agent semantics). Docker `args` are allowlisted.
- **Secrets:** per-environment secret store, exposed only through Jenkins
  `credentials('NAME')`, masked in logs.
- **Architecture:** Python 3.13, package `jendock/`, layered (domain / services /
  repositories / adapters / api). FastAPI + Pydantic + SQLAlchemy 2 + Alembic. React 19 +
  Vite frontend. ruff + mypy (strict) + pytest enforced in CI-like `make check`.
- **Rollout:** v2 lives in `v2/`. v1 keeps running in production until v2 reaches parity,
  then cutover per project.
- **Working model:** implementation by Sonnet agents, review by Opus before each task closes.

## Main tasks and sub-tasks

Jira: Epic [PWI-956](https://monglish1.atlassian.net/browse/PWI-956). Main tasks PWI-957…PWI-971, sub-tasks PWI-972…PWI-1068 (numbered `N.M` in their titles).

### 1. Foundation and tooling — PWI-957
1. Package layout `jendock/{domain,services,repositories,adapters,api,worker,mcp}`
2. `pyproject.toml`: dependencies, ruff (lint + format), mypy strict, pytest config
3. `Makefile` / scripts: `make check` = ruff + mypy + pytest
4. Settings via Pydantic Settings (env vars, paths, Docker socket, domain)
5. Structured logging with secret redaction
6. Dockerfiles for api, worker, frontend build (multi-stage, pinned bases)
7. `compose.yml` for v2 (api, worker, shared Unix socket, volumes)

### 2. Core domain and persistence — PWI-958
1. SQLAlchemy models: Project, Environment (development/production), PipelineDefinition,
   Run, StageRun, StepRun, Secret, AuditEvent, User, Session
2. Alembic migrations for the platform's own database
3. Repository classes per aggregate
4. Project service: create/update/archive, repository + branches per environment
5. Environment service: branch, auto-deploy toggle, compose project/services mapping
6. Secret store: encrypted at rest (Fernet key file), per environment, never returned in clear
   except explicit reveal endpoint with audit
7. Audit log service (secret-free details)

### 3. Authentication and security — PWI-959
1. Admin user with PBKDF2/argon2 password hash, CLI to create/reset
2. Server-side sessions, `__Host-` Secure HttpOnly SameSite=Strict cookie, idle + absolute expiry
3. CSRF token + exact Origin check on mutations
4. Login rate limiting
5. Receiver/worker split: API has no Docker socket; worker reached via group-restricted Unix socket
6. API tokens for MCP/agents with scopes (read, run, deploy, containers, terminal)

### 4. Jenkinsfile engine (parse and validate) — PWI-960
1. Lexer: identifiers, single/double/triple-quoted strings, numbers, booleans, lists, maps, comments
2. Parser → typed AST: `pipeline`, `agent`, `environment`, `options`, `parameters`,
   `triggers`, `stages`, `stage`, `steps`, `when`, `parallel`, `post`
3. Steps: `sh`, `echo`, `timeout`, `retry`, `dir`, `withEnv`, `error`, `dockerBuild`,
   `deploy`, `healthCheck`
4. `when`: `branch`, `environment`, `not`, `allOf`, `anyOf`
5. `environment` assignments and `credentials('NAME')`
6. String interpolation for double-quoted strings (`${VAR}`, `${env.VAR}`), errors on unknown vars
7. Validation with line/column errors; reject `script {}` and unknown steps with clear messages
8. Serializer AST → Jenkinsfile (needed by the visual editor round-trip)
9. Golden tests with real-world Jenkinsfiles

### 5. Pipeline execution and Docker runtime — PWI-961
1. `ContainerRuntime` interface + Docker CLI/SDK adapter (build, run, logs, stop, inspect)
2. Workspace manager: clone pinned commit, cleanup, disk bounds
3. `PipelineRunner`: stage ordering, `when`, `parallel`, `post` (always/success/failure/cleanup)
4. Step classes (`ShStep`, `EchoStep`, `DockerBuildStep`, `DeployStep`, `HealthCheckStep`,
   `TimeoutStep`, `RetryStep`, `DirStep`, `WithEnvStep`)
5. Docker agent semantics: built vs external images, `/workspace` mount, environment network,
   allowlisted `args` (`--network none` etc.)
6. Live log streaming (per step, bounded, secret masking) via SSE
7. Run queue: one build at a time (configurable), cancel pending, abort running
8. Crash recovery: resume/mark interrupted runs after worker restart
9. CPU/RAM limits and nice priority for build containers

### 6. GitHub integration — PWI-962
1. Webhook endpoint: HMAC-SHA256 signature, allowlisted repositories, ping/push events
2. Branch → environment mapping, auto-deploy toggle per environment
3. Branch listing and manual run (choose branch, preview commit)
4. Clone via deploy key over SSH (port 443)
5. Pipeline source resolver: repo `Jenkinsfile` at commit vs stored pipeline
6. Optional commit status reporting back to GitHub

### 7. Deployment and rollback — PWI-963
1. Compose adapter: override service image, `up -d --no-deps` for selected services only
2. Record previous image per service before deploy
3. Health verification (container health + optional HTTP)
4. Automatic rollback to previous images on failure
5. Image tagging `<name>:<sha12>-<run>` and retention/cleanup of old images (protect running)
6. Deployment history per environment with redeploy of a previous version

### 8. Container management — PWI-964
1. Inventory: containers per project/environment with image, commit label, health, restarts, mounts
2. Start / stop / restart with confirmation and audit
3. Logs viewer (tail + follow, bounded)
4. Environment variables editor (masked, per-service override, recreate service, restore on failure)
5. Read-only file browser
6. CPU/RAM limits per container
7. Host telemetry (CPU, RAM, disk) and per-container stats

### 9. Container terminal — PWI-965
1. WebSocket terminal backed by `docker exec -it /bin/sh` in the selected running container
2. Bound to session and exact container ID; idle and absolute timeouts
3. xterm.js frontend with mobile key bar
4. No command/output storage in audit; only open/close events

### 10. Database inspector (port from v1) — PWI-966
1. Database connection config per environment (encrypted)
2. PostgreSQL adapter: schemas, tables, columns, rows (read-only, paginated)
3. MySQL/MariaDB adapter: same
4. Read-only SQL console with limits and timeout
5. UI pages and MCP read tools

### 11. Ingress: Apache, Cloudflare, TLS (port from v1) — PWI-967
1. Domain model per environment (host → service:port)
2. Cloudflare DNS adapter (create/update/delete records, proxied flag)
3. Apache vhost generation + config test + graceful reload via host helper
4. TLS issuance/renewal (ACME) and expiry monitoring
5. Domain status checks (DNS resolves, HTTPS responds)
6. UI and MCP tools

### 12. MCP server for agents — PWI-968
1. MCP server (official SDK) with token auth and scopes
2. Tools: list projects/environments, read/update pipeline, validate Jenkinsfile, trigger run,
   read run logs, deploy/rollback, container actions, logs, DB read queries, domains
3. Audit every agent action with token identity
4. Token management UI

### 13. Frontend (React 19 + Vite) — PWI-969
1. App shell: sidebar, breadcrumbs, responsive mobile, light/dark (GitHub Primer-like)
2. Login page
3. Projects list and project overview (environments, last runs, health)
4. Runs: list, run detail with stage graph and live logs, cancel/retry
5. Pipeline editor — visual (step cards, add/reorder/configure)
6. Pipeline editor — text (Jenkinsfile with syntax highlighting + live validation), synced with visual
7. Read-only mode when the pipeline comes from the repo
8. Containers pages: list, detail, actions, logs, env editor, files
9. Terminal page
10. Database inspector pages
11. Domains pages
12. Settings: GitHub, secrets, API tokens, users

### 14. Templates and onboarding (zero-knowledge users) — PWI-970
1. Pipeline templates: Laravel, Next.js, Node API, Python/FastAPI, static site, generic Dockerfile
2. Project creation wizard: repo → detect Dockerfile/framework → choose template → environments
3. Inline help for every step type and field
4. "Test pipeline" dry-run validation before saving

### 15. v1 migration and production cutover — PWI-971
1. Import projects/recipes from v1 (`projects.json` + DB) and convert recipes to Jenkinsfiles
2. Run v2 alongside v1 on the server (different port/subdomain)
3. Per-project cutover: switch webhook to v2, verify one real deploy
4. Decommission v1 after all projects move
5. Backup/restore of the platform database

## Execution order

1 → 2 → 3 → 4 → 5 → 6 → 7 (first end-to-end deploy) → 13.1–13.7 → 8 → 9 → 14 →
12 → 10 → 11 → 13.8–13.12 → 15.
