# End-to-end acceptance test (`make test-e2e`)

The unit and integration suites prove the parts; this one proves the whole: the real
`compose.yml` stack (api and worker images built from the tree) driven only through the public
interfaces (HTTP API with session and CSRF, SSE, the signed GitHub webhook, the MCP endpoint
with a token, the terminal WebSocket), with real containers. It needs Docker, the base images
`busybox:stable`, `alpine:3.24`, `postgres:16` and network access for the first image build
(Python and npm packages); later runs reuse Docker's layer cache. Expect 2 to 3 minutes warm
and several more for a cold build.

```
make test-e2e                      # all steps, in order
E2E_REPORT=/tmp/e2e.md make test-e2e   # also write the timing table to a file
```

Not part of `make check`, `make test` or `make test-docker` (marker `e2e`).

## What it creates, and how it cleans up

- A compose project `jdtest-e2e-<id>` of the platform (api, worker, the inspector image), a second
  compose project `jdtest-e2e-<id>-app` for the sample application (two app services and a seeded
  postgres:16) and temporary host directories for the workspaces, the sample projects (mounted
  read-only into the worker at the identical path), the git repositories and the backups.
- Every container, image, volume and network carries the session labels of
  `tests/docker_support.py` (`jendock.test=1`, `jendock.test.session=<uuid>`). Teardown
  removes exactly those (the platform stack, the sample project, the restored-data volume) and
  `assert_nothing_left()` fails the run when anything survived. Nothing else on the daemon is
  touched, and no real GitHub, Cloudflare or certificate authority is ever contacted: the
  worker clones `file:///repos/acme/app.git` through `JENDOCK_GIT_URL_TEMPLATE`, and the
  webhook is signed with a secret the test writes into the worker's keys volume.
- The stack runs in production mode (https origin, `__Host-` cookie); the test reaches it over
  loopback http and carries the cookie and `Origin` by hand, like a browser would.
- Not driven through a public interface, only inspected: the audit table (read-only sqlite in
  the api container), `docker` queries to verify which container runs which image, and the
  git commits (made in a throwaway container as the worker's uid).

## The story

| # | Steps | What is proved |
|---|---|---|
| 1 | `test_01` | first admin created with the CLI in the api container; login; API token created (value shown once, never listed) |
| 2 | `test_02`, `test_03` | project and environment (compose project, compose file, `--env-file`, services) configured through the API; branch head preview, manual run, followed over SSE to success; the deployment ledger records previous and new image; only `web` is recreated (`worker` and `db` keep their container ids); the interpolated port of the env file serves the new version |
| 3 | `test_04` - `test_06` | forged and unknown webhooks are refused or ignored; a signed push with auto-deploy off is only recorded; a push of a new commit queues a run and deploys it; a commit whose HTTP health check fails is rolled back automatically: the run fails with the rollback message, the previous image runs again, the ledger says `rolled_back` |
| 4 | `test_07` - `test_10` | container inventory (only the environment's project), two-step restart with a single-use confirmation, environment edit that recreates only that service and keeps the deployed image, logs tail |
| 5 | `test_11` | terminal ticket, WebSocket, `echo e2e-ok` read back, close; a ticket is single use; the audit holds `terminal.open` and `terminal.close` and nothing of the session |
| 6 | `test_12` - `test_14` | database connection to the postgres service with the password in the secret store (never returned, never logged), tables and rows, a SELECT, and every kind of write refused |
| 7 | `test_15`, `test_16` | MCP: initialize, tools, `list_runs`, `run_readonly_query`; a read-only token sees fewer tools and is denied the rest and production; no token, a cookie and a wrong `Host` never reach the tools |
| 8 | `test_17` | backup (key not included) created in the worker, restored into a fresh data volume, migrated, refused a second time, and an api container on it serves the same projects, deployments and tokens |
| 9 | `test_18`, `test_19` | redeploy of the first version without checkout or build; no workspace left behind |
| 10 | `test_20` | no secret value in the api or worker log or the audit trail; the expected actions are audited |

## Bugs this test found

1. `docker/worker.Dockerfile` contained merge conflict markers (the image could not be built).
   `tests/test_repo_hygiene.py` now fails on any conflict marker in the tree.
2. The compose settings of an environment (project, files, env files, services) could not be
   configured through any public interface. `PATCH .../environments/{env}` accepts and returns
   them now.
3. After every successful run the worker killed the service it had just deployed: `dockerBuild`
   labelled images `jendock.run_id`, containers inherit image labels, and the end-of-run
   cleanup kills containers by that label. Images now carry `jendock.build_run`.
