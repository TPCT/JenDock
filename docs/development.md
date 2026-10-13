# Development guide

## Layout

- `backend/domain` - pure enums, validated value objects, errors, audit rules; no I/O
- `backend/services` - use cases (project, environment, pipeline, run, secret, audit);
  each receives a `UnitOfWork`, no globals
- `backend/repositories` - SQLAlchemy models, `database.py` (engine, `UnitOfWork`), one
  query-only repository per aggregate, Alembic migrations in `migrations/`
- `backend/adapters` - external systems: `ContainerRuntime` port with the docker CLI
  implementation, git source, workspaces, deployer port, HTTP probe (GitHub, Cloudflare later)
- `backend/execution` - worker-side pipeline execution (runner, steps, docker agent rules,
  logs, run queue, crash recovery)
- `backend/api` - FastAPI HTTP layer (`create_app`, `AppContainer`, routers, schemas, error
  mapping, security headers)
- `backend/worker` - worker process (`python -m backend.worker`): RPC server, the run
  queue and the only holder of the secret key
- `backend/pipeline` - Jenkinsfile engine (lexer, parser, typed AST, step catalogue, validator,
  serializer); pure code, never executes anything
- `backend/cli.py` - admin CLI (`python -m backend.cli users ...`)
- `backend/mcp` - MCP server for AI agents
- `backend/config.py`, `backend/logging.py` - settings (`JENDOCK_*`) and JSON logging
- `backend/hosthelper` - the host helper (stdlib + cryptography): Apache vhosts, certificates;
  ingress design in `docs/ingress.md`, install in `docs/ingress-host-helper.md`
- `docker/` - `api.Dockerfile`, `worker.Dockerfile`; `compose.yml` wires them together

## Make targets

- `make install` - editable install with dev extras into `.venv`
- `make lint` - ruff check and ruff format --check
- `make format` - apply ruff fixes and formatting
- `make typecheck` - mypy (strict) on `backend` and `tests`
- `make test` - pytest (tests marked `docker` are deselected)
- `make test-docker` - the real-Docker integration tests (`pytest -m docker`); they are skipped
  when no Docker daemon or git is available. They build and run containers in the local daemon,
  label everything `jendock.test=1` plus a per-session `jendock.test.session=<uuid>`
  (`tests/docker_support.py`: `session_labels()`), and the teardown removes and checks only that
  session's containers, images, networks and volumes, so parallel suites on one daemon do not
  interfere
- `make migrate` - `python -m backend.repositories.migrate` (upgrade head on
  `JENDOCK_DATABASE_URL`); the same command works inside the api/worker images
- `make lock` - regenerate `requirements.lock` (needs Docker)
- `make test-py313` - run tests on Python 3.13 in Docker (installs git in the throwaway container)
- `make check` - lint, typecheck, test
- `make test-e2e` - the whole platform with real containers, driven only through its public
  interfaces (builds the images from the tree; see `docs/e2e.md`)
- `make preflight` - read-only host check before the production installation (`PREFLIGHT_ARGS`
  passes flags, e.g. `--env-file .env`); see `docs/deployment.md`
- `make lint-jenkins` - compare the Jenkinsfile engine with a real Jenkins linter in a throwaway
  container (needs Docker and network); rewrites `docs/jenkins-compatibility.md`
