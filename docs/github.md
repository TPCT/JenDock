# GitHub integration

## Trust split

GitHub secrets never reach the API. The **worker** holds the webhook secret
(`JENDOCK_GITHUB_WEBHOOK_SECRET_FILE`) and the deploy keys (`JENDOCK_GIT_SSH_KEY_DIR`), both
in the `keys` volume, and runs every `git` command (`ls-remote`, fetch, clone). The API
forwards the raw webhook body and the `X-Hub-Signature-256` header over the worker socket
(`github.verify_webhook`) and gets back a boolean; it also asks `github.branch_head` for branch
heads. Chosen over giving the API its own HMAC key because the API is the process that parses
internet input: with this split a compromised API cannot read the secret or the key, and the
worker's constant-time comparison (`hmac.compare_digest`) is the only place the secret is used.
Trade-off: while the worker is down the webhook answers 503 (GitHub shows a failed delivery; the
runs could not execute then anyway) and a body goes over the socket base64-encoded, which is why
the RPC limit is 2 MiB.

## Webhook `POST /api/github/webhook`

No session, token or CSRF: the HMAC signature is the authentication.

- Order of checks: signature header format (401), `Content-Type: application/json` (415), body
  at most 1 MiB, also when chunked (413), signature verified by the worker (401), delivery id
  `X-GitHub-Delivery` (422), JSON object (422). Requests that fail signature verification
  (missing, malformed or wrong) are counted per client IP; 30 per minute answers 429 with
  `Retry-After` before the worker is called (the same in-memory limiter as logins, so a restart
  resets it). Valid deliveries are never counted. Nothing is stored or audited for a rejected
  request (a warning is logged).
- Events: `ping` answers 200 `{"outcome": "pong"}`. `push` is routed; any other signed event is
  acknowledged with 202 `ignored`.
- Allowlist: the repository (`repository.full_name`, case-insensitive) must belong to a
  non-archived project. A project's environments follow the branch they track (`branch` of the
  environment). A push to that branch with `auto_deploy` on queues a run (trigger `push`, branch,
  the pushed `after` sha pinned); with `auto_deploy` off the delivery is recorded (`recorded`, no
  run). Branch deletes (`deleted` or an all-zero `after`), tags, other branches and unknown
  repositories are `ignored` (202 with a `reason`). Several projects on one repository all run.
- Idempotency: every handled delivery id is stored in `webhook_deliveries` in the same transaction
  as its runs, so a replay (GitHub redelivery, a retry) answers 200 `duplicate` and changes
  nothing. A delivery that failed with a 5xx stored nothing and may be retried. Rows older than 30
  days are removed on the next delivery.
- Audit: `github.webhook` (delivery id, event, repository, branch, outcome, run ids) by the
  `system:github` actor and `run.create` for each run. A malformed push payload is 422 and is not
  remembered. Repositories are matched by name, not by GitHub's numeric id:
  a renamed or transferred repository stops matching until the project is updated.

Responses: `{"outcome": "pong|queued|recorded|ignored|duplicate", "runs": [ids], "reason": ...}`.

## Setup

1. Webhook secret: `openssl rand -hex 32 > github_webhook_secret` into the `keys` volume (mode
   0600, owner uid 10001, e.g. `docker compose run --rm -T worker sh -c 'cat > /run/jendock-keys/github_webhook_secret'`),
   the same value in the GitHub webhook (content type `application/json`, events: push; URL
   `https://<host>/api/github/webhook`). Surrounding whitespace in the file is ignored; the file is
   read at each delivery, so rotating it needs no restart.
2. Deploy keys, one per repository (GitHub allows a deploy key on a single repository only):
   `$DC exec worker python -m backend.cli deploy-key create <owner>/<name>` generates an ed25519
   key at `/run/jendock-keys/deploy_keys/<owner>__<name>` (mode 0600, directory 0700) and prints
   only the **public** key. Add that line as a **read-only** deploy key under the repository's
   *Settings -> Deploy keys* (leave "Allow write access" off). The command refuses to replace an
   existing key; `--rotate` generates a new one (replace it on GitHub too). `deploy-key show
   <owner>/<name>` prints the public key again and `deploy-key list` names the repositories that
   have a key; creating and rotating are audited (`deploy_key.create`, `deploy_key.rotate`, no
   key material). A repository without its own key falls back to the optional shared
   `JENDOCK_GIT_SSH_KEY_FILE`, which is only for installations that already have one. Projects
   with `git_transport: "ssh"` (the default for new projects) then clone as
   `ssh://git@github.com/<owner>/<repo>.git`. Public repositories can use `"https"` per project
   (`PATCH /api/projects/{slug}`); existing projects were migrated to `https`.
3. Commit statuses (optional, off by default): set `GITHUB_STATUS_ENABLED=true` and store a token
   with the `repo:status` permission as the environment secret `GITHUB_STATUS_TOKEN`. The worker
   posts `pending` when a run starts and `success`, `failure` or `error` (cancelled or interrupted)
   when it ends, context `jendock/<environment>`, linking to `/runs/<id>`. A missing secret, a
   GitHub error or a timeout is logged (type only, never the token) and never affects the run.
   Reading the token is audited as `secret.resolve`.

## SSH details

`GitSshSource` runs `git` with an argv list (never a shell) and `GIT_SSH_COMMAND` set to
`ssh -F /dev/null -i <key of the repository> -p 443 -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes
-o PasswordAuthentication=no -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<pinned>
-o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 -o HostKeyAlias=github.com
-o Hostname=ssh.github.com ...` (git itself passes the string to `sh -c`, so every token is
shell-quoted by `shlex.join`; a test checks that paths with shell metacharacters round-trip). It
connects to `ssh.github.com:443` (port 22 is blocked on the production VPS) and verifies the host
key against `docker/github_known_hosts`, which the worker image installs at
`/etc/jendock/github_known_hosts`. Only the ed25519 host key is pinned (fingerprint
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`); to refresh it after a GitHub key
rotation run `scripts/refresh_github_known_hosts.py`, compare the fingerprint with GitHub's
documentation and commit the file. The worker image includes `openssh-client` (which provides `ssh-keygen` for the
`deploy-key` commands).

## Which pipeline a run executes

The run row is created with the environment's latest stored pipeline as a provisional snapshot.
When the worker starts the run it reads `Jenkinsfile` at the repository root of the **pinned
commit** (a depth-1 fetch of that commit, no clone): if present it is the pipeline (source
`repository`), otherwise the latest stored platform pipeline (source `platform`; the snapshot taken
at queue time is the fallback when nothing is stored), and with neither the run fails with a
clear message. The pipeline editor's repository detection (`pipeline.repository_file`) uses the same code path
(branch head via `git ls-remote`, then the file at that commit through the project's transport).
A repository that cannot be read fails the run; it never silently falls back to
the stored pipeline. The chosen source and the exact text are written back to the run, then the
text is validated like any save. A missing pipeline or any diagnostic of an invalid one is written
to the step log of a failed `Pipeline` stage (the run's other stages are not created); a valid run
shows `pipeline source: repository|platform` in the Checkout log. A symlink, a directory or a file
larger than 256 KiB named `Jenkinsfile` is an error.

## Endpoints

See `docs/api-contract.md` (Projects, Runs): project list, create and settings, environment
runs, branch heads (`git ls-remote` in the worker), manual runs and retries.
