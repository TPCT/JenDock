# Running Shiftcodes v2 next to v1 on the VPS

This document **prepares** the first production installation of v2. Nothing in the repository
executes any of it: every command below is run by an administrator, on purpose, step by step.
v1 keeps running untouched the whole time; the last section describes how to remove v2 again
without a trace on v1.

> The facts about v1 below (paths, ports, socket, binaries) were read from the v1 repository
> (`compose.yml`, `deploy_service.py`, `README.md`, `deployment/`, `docs/operations/*`), not from
> the machine. `make preflight` checks the host against them and says what differs; where it
> and this document disagree with the machine, the machine wins and the `.env` is adjusted.

## 1. The picture

| | v1 (running) | v2 (this document) |
|---|---|---|
| public name | `monitoring.shiftcodes.net` | `deploy.shiftcodes.net` (a new vhost) |
| compose project | `deploy-monitor` in `/opt/docker-engine/projects/deploy-monitor` | `shiftcodes-v2` in `/opt/docker-engine/projects/shiftcodes-v2` (a release checkout) |
| host port (loopback only) | `127.0.0.1:19190` (receiver), `19192` (MCP) | `127.0.0.1:8000` (API, web UI, `/mcp`) |
| Docker daemon | isolated daemon, `/run/docker-isolation.sock` | the same daemon, as a client; v2's own containers also run there |
| docker CLI / compose | `/opt/docker-engine/29.8.2/docker/docker`, `/opt/docker-engine/tools/docker-compose` | mounted read-only from the same paths |
| state | external volumes `deploy-monitor_state`, `_worker-state`, `_work` | volumes `shiftcodes-v2_data`, `_worker-socket`, `_keys` (new) |
| GitHub access | ssh over `ssh.github.com:443`, pinned `known_hosts` | the same transport; host key pinned in the image |
| TLS | host Apache, Docker-based certbot with a shared HTTP-01 webroot | host Apache, the same certbot mechanism, a new certificate |

Both stacks talk to the **same Docker daemon and see the same containers**. v2 only ever starts,
stops and recreates the compose services that an environment explicitly names (never anything
else, never with `--remove-orphans`), and builds images tagged `sc/<project>-<env>-<image>:...`,
a namespace v1 does not use. The rule that matters is operational: **a project is deployed by v1
or by v2, never both**. Environments in v2 start with auto-deploy off; a project is cut over
only after its v1 hook is disabled (section 10).

## 2. Users, groups and host directories

Inside the images the API runs as **uid 10002**, the worker as **uid 10001**, both in **gid
10001** (the shared group of the data volume and the worker socket). Host directories that the
worker writes must therefore belong to uid 10001.

| path | owner : group | mode | purpose |
|---|---|---|---|
| `/opt/docker-engine/projects/shiftcodes-v2` | `root:root` (or the admin) | 0755 | the release checkout with `compose.yml`, `deployment/`, `.env` |
| `.env` in it | admin | 0640 | non-secret settings (`deployment/env.production.example`) |
| `/var/lib/shiftcodes-workspaces` (`WORKSPACES_HOST_DIR`) | `10001:10001` | 0750 | run workspaces; the host daemon bind-mounts subdirectories into step containers, so it is mounted at a host path the daemon can resolve |
| `/var/backups/shiftcodes` (`BACKUP_HOST_DIR`) | `10001:10001` | 0750 | scheduled backups and the target of manual ones |
| `/opt/docker-engine/projects` (`COMPOSE_PROJECTS_DIR`) | unchanged | unchanged | the compose files and `--env-file` files of **deployed projects**; mounted read-only into the worker at the **identical absolute path** |
| Docker volume `shiftcodes-v2_keys` | `10001:10001` | 0700 (the image sets it) | `secret.key`, `deploy_key`, `github_webhook_secret`; mounted in the worker only |
| Docker volume `shiftcodes-v2_data` | `10001:10001` | 2770 (setgid) | SQLite database, run logs, managed compose overrides (`deployments/`) |

```
install -d -o 10001 -g 10001 -m 0750 /var/lib/shiftcodes-workspaces /var/backups/shiftcodes
```

Why the projects directory is mounted at the identical path: the compose CLI runs **inside** the
worker container, but bind-mount sources and relative paths in compose files are resolved by
the **host** daemon against the compose file's own directory. An environment's `compose_files`
and `compose_env_files` (variable interpolation files such as
`/opt/docker-engine/projects/aic/ports.env`) are absolute paths below `COMPOSE_PROJECTS_DIR`;
the deployer refuses a deployment when one of them is not readable.

## 3. Docker socket, binary and group

- `DOCKER_SOCKET_PATH=/run/docker-isolation.sock` (host path, always mounted at
  `/var/run/docker.sock` in the worker).
- `DOCKER_GID` is that socket's group id: `stat -c %g /run/docker-isolation.sock`. The worker
  gets it through `group_add`; the API has no Docker access at all.
- The worker deploys with `docker compose`. The overlay mounts **v1's pinned docker CLI** over
  `/usr/local/bin/docker` and **v1's standalone `docker-compose` binary** into
  `/usr/local/lib/docker/cli-plugins/docker-compose`, where the CLI finds it as a plugin.
  `DOCKER_BINARY_HOST_PATH` / `DOCKER_COMPOSE_HOST_PATH` change the host paths.

## 4. Settings (`.env`)

Copy `deployment/env.production.example` to `.env` (mode 0640). Everything in it is non-secret:

- `PUBLIC_ORIGIN=https://deploy.shiftcodes.net`: required; the default inside the image is v1's
  name. Login cookies, CSRF `Origin` checks and commit-status links use it.
- `TRUSTED_PROXIES`: the address the API sees for Apache. Apache reaches the loopback port, and
  traffic arrives from the compose network's gateway. After the first `up` read it with
  `docker network inspect shiftcodes-v2_default -f '{{(index .IPAM.Config 0).Gateway}}'`, put it
  in `.env` and run `up -d` again. Empty trusts nobody: every visitor then shares one login
  rate-limit bucket.
- `MCP_ALLOWED_HOSTS`: extra `Host` values accepted at `/mcp`. The vhost uses
  `ProxyPreserveHost On`, so the public name arrives unchanged and this stays empty. Set it only
  if you proxy with a rewritten Host (for example `127.0.0.1:8000`).
- `BACKUP_ENABLED`, `BACKUP_KEEP`, `BACKUP_HOUR_UTC`: scheduled backups (section 8), off by default.

## 5. Apache

`deployment/apache-deploy.shiftcodes.net.conf` is a **new** site file (v1's vhost and its
include files are not edited): a port-80 vhost that serves the ACME challenge from the shared
webroot and redirects everything else, and a port-443 vhost that proxies to `127.0.0.1:8000`
with `ProxyPreserveHost On`, a 3600 s timeout (server-sent run logs, MCP) and the client address
in `X-Forwarded-For` (client-supplied values are discarded). Required modules: `proxy`,
`proxy_http`, `headers`, `ssl`, `rewrite`. Set the two certificate paths to where the host's
Docker-based certbot stores `deploy.shiftcodes.net` (request that certificate first, with the
same mechanism and webroot v1 uses; the HTTP-01 path works as soon as the port-80 vhost is up).

## 6. Secrets and where they live

Nothing secret goes into `.env`, the repository or `docker compose` arguments.

- **`secret.key`** (encrypts all stored secrets): created automatically, 0600, in the `keys`
  volume on the first worker start. **Back it up separately** (section 8): without it every
  stored secret is lost.
- **Deploy key** (read-only access for the ssh transport) as `/run/shiftcodes-keys/deploy_key`,
  and the **webhook secret** as `/run/shiftcodes-keys/github_webhook_secret`. Generate a new
  ed25519 key for v2 (do not reuse v1's), add its public half as a read-only deploy key to each
  repository, and create the files from stdin so nothing touches a shell history or `ps`:

  ```
  ssh-keygen -t ed25519 -N '' -f /tmp/v2_deploy_key -C shiftcodes-v2   # then add /tmp/v2_deploy_key.pub on GitHub
  $DC run --rm -T --no-deps --entrypoint sh worker -c 'umask 077; cat > /run/shiftcodes-keys/deploy_key' < /tmp/v2_deploy_key
  shred -u /tmp/v2_deploy_key /tmp/v2_deploy_key.pub
  openssl rand -hex 32 | tee /dev/stderr | \
    $DC run --rm -T --no-deps --entrypoint sh worker -c 'umask 077; cat > /run/shiftcodes-keys/github_webhook_secret'
  ```

  (the webhook secret is shown once on stderr so you can paste it into GitHub: *Settings ->
  Webhooks*, payload URL `https://deploy.shiftcodes.net/api/github/webhook`, content type
  `application/json`, event *push*). Projects use `git_transport = ssh` once their deploy key is
  in place (`PATCH /api/projects/{slug}`).
- **Per-environment secrets** (for `credentials('NAME')` and `GITHUB_STATUS_TOKEN`) are created
  in the UI/API and stored encrypted in the database.
- **Admin password**: only ever typed at a prompt or piped to the CLI (section 7).

`$DC` stands for the stack command used throughout (the isolated daemon, v1's compose binary):

```
export DC='env DOCKER_HOST=unix:///run/docker-isolation.sock /opt/docker-engine/tools/docker-compose -f compose.yml -f deployment/compose.production.yml'
```

## 7. First start

```
cd /opt/docker-engine/projects/shiftcodes-v2
$DC config > /dev/null                      # validates compose.yml + overlay + .env
$DC up -d --build                           # builds both images on the isolated daemon
$DC --profile tools build inspector         # the database inspector helper image (not started by up)
$DC run --rm api python -m shiftcodes.repositories.migrate
$DC run --rm -T api python -m shiftcodes.cli users create admin --password-stdin   # first admin
```

The migration command is idempotent and is also what to run after every update. The CLI reads
the password from stdin (`--password-stdin`) or a hidden prompt (drop `-T` and the flag); it is
never an argument. Further users and password resets use the same CLI.

## 8. Backups

`python -m shiftcodes.cli backup create --to /backups` (run it in the worker container, which mounts
`BACKUP_HOST_DIR` at `/backups`) writes a timestamped archive with an online snapshot of
the SQLite database, the managed compose overrides and a checksummed manifest; `--include-logs`
adds run logs. **The secret key is not in it.** `--include-key` writes it to a separate 0600
file; store that file and the archives in different places. Set `BACKUP_ENABLED=true` for one
archive per day (03:00 UTC, newest 7 kept). `backup restore --from <archive>` restores into an
**empty** data directory only; afterwards run the migrations. Restore drill (do it once on a
scratch host before relying on it): `docker run --rm -v <scratch volume>:/var/lib/shiftcodes ...
python -m shiftcodes.cli backup restore --from /backups/<archive> --key-file <saved key>`.
Details: README "Backup and restore".

## 9. Per-project preparation in the UI

Create the project (repository, branch per environment), set `compose_project`,
`compose_files` and, when the project is deployed with an interpolation file,
`compose_env_files`, all as **absolute paths below `COMPOSE_PROJECTS_DIR`**, and the service
names to deploy. Keep **auto-deploy off**. Use a manual run or a dry run to verify the pipeline
while v1 still owns production.

## 10. Step-by-step checklist

Each step has a check. Stop at the first one that does not give the expected result.

1. **Host prerequisites.** `make preflight PREFLIGHT_ARGS="--env-file .env"` (read-only). Expect
   `READY`; every `FAIL` line says what to fix.
2. **Directories.** `ls -ldn /var/lib/shiftcodes-workspaces /var/backups/shiftcodes` shows
   `10001 10001` and `drwxr-x---`.
3. **DNS and certificate.** `getent hosts deploy.shiftcodes.net` resolves to the VPS; request the
   certificate with v1's certbot mechanism after step 4's port-80 vhost is enabled.
4. **Apache.** Install the vhost file, `a2ensite deploy.shiftcodes.net`, then
   `apachectl configtest` (expect `Syntax OK`) and a graceful reload. v1 check:
   `curl -sI https://monitoring.shiftcodes.net/ | head -1` is unchanged.
5. **Stack.** `$DC config > /dev/null`, `$DC up -d --build`, `$DC --profile tools build inspector`
   (database inspector helper image `shiftcodes-v2-inspector:latest`; override the name with
   `INSPECTOR_IMAGE` in `.env`; rebuild it after upgrades), `$DC ps` shows `api` and `worker`
   healthy/running. `curl -s http://127.0.0.1:8000/healthz` gives `{"status":"ok"}`.
6. **Proxy trust.** Set `TRUSTED_PROXIES` as in section 4 and `$DC up -d`. From a workstation:
   `curl -s https://deploy.shiftcodes.net/healthz` works through Apache.
7. **Migrations and admin.** Section 7. Log in at `https://deploy.shiftcodes.net`.
8. **Secrets files.** Section 6; `$DC exec worker ls -l /run/shiftcodes-keys` shows `secret.key`,
   `deploy_key`, `github_webhook_secret`, all `-rw-------` owned by `shiftcodes`.
9. **Worker can reach Docker.** `$DC exec worker docker version --format '{{.Server.Version}}'`
   prints the isolated daemon's version; `$DC exec worker docker compose version` works.
10. **Projects directory visible.** `$DC exec worker ls /opt/docker-engine/projects` lists the
    projects; a file from a project's `compose_files` is readable.
11. **Dry run of a project.** In the UI create a project, press *Dry run* / start a **manual run**
    of a repository whose pipeline only builds and tests (no `deploy`). The run succeeds and
    the workspace is cleaned (`ls /var/lib/shiftcodes-workspaces` is empty again).
12. **Webhook.** In GitHub add the v2 webhook to one test repository; *Recent deliveries* shows a
    `200`; a run appears in the UI.
13. **Backup.** `$DC exec worker python -m shiftcodes.cli backup create --to /backups`, then
    `ls -l /var/backups/shiftcodes` shows a 0600 archive. Copy the key out once:
    `... backup create --to /backups --include-key` and move the `.secret.key` offline.
14. **Cutover of one project** (only when everything above is green): disable that project's v1
    hook, set the v2 environment's compose settings and enable auto-deploy there, push a trivial
    commit, watch the deployment, health check and (if you break the health check on purpose
    in a scratch environment) the automatic rollback. Repeat per project.

## 11. Rolling back: stop v2, v1 untouched

v2 never edits v1's files, volumes, vhost or containers, so backing out is removing v2:

1. **Per project:** re-enable the v1 hook and disable auto-deploy in v2. If a v2 deployment left
   an environment on a v2 image, redeploy with v1 (v1 sets its own image) or use the v2 history
   (*Redeploy* of the previous version) before stopping v2. The managed override of a project is
   `<data volume>/deployments/<compose project>/override.yml`: while it exists, **any**
   `docker compose` run with that file keeps the v2 image; v1 does not use it.
2. **Stop the stack:** `$DC down` (no `-v`; the volumes keep the data, including `secret.key`).
3. **Disable the vhost:** `a2dissite deploy.shiftcodes.net`, `apachectl configtest`, reload.
   `monitoring.shiftcodes.net` is unaffected.
4. **Verify v1:** `docker ps` on the isolated daemon shows v1's containers unchanged;
   `curl -sI https://monitoring.shiftcodes.net/` as before.
5. **Remove everything** (only when abandoning v2 for good): `$DC down -v`, delete the
   checkout, `/var/lib/shiftcodes-workspaces`, the backup directory and the GitHub webhooks and
   deploy keys of v2. The images `sc/...` that v2 built can be removed with `docker rmi`.

## 12. Known limits

- The worker mounts the Docker socket of the isolated daemon: a compromised worker can act on
  that daemon. v2 narrows what it does (named services only) but does not sandbox itself from the
  daemon; keep the API without it (it has none).
- Scheduled backups support SQLite only and write into a directory the worker can write.
- Both stacks poll the same GitHub repositories with different keys; a repository added to v2
  needs its own deploy key and webhook.
- The mounts of the docker CLI and compose binary assume v1's layout; if the host binary is
  dynamically linked against libraries missing in the slim worker image, use the CLI baked
  into the image (drop the `DOCKER_BINARY_HOST_PATH` mount) and keep only the compose plugin.
