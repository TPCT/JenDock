# Restricted ingress host helper: administrative setup

This package is opt-in. Adding files to the repository does not install, start,
restart, or reconfigure a production service. Verify the actual deployment's
Apache binary, parent PID/configuration, Docker Unix socket, state database,
console-owner credentials, include roots, and listener before installation.
The supplied unit uses the existing `deploy-monitor` project checkout at
`/opt/docker-engine/projects/deploy-monitor`; adjust its Python environment to
the administrator-verified existing installation, not a second product copy.

The helper assembles `ApacheExecutor`, `HostAdapter`, `HostGrantIssuer`,
`HostService`, and `HostServer`. A single serving thread owns SQLite and executes
all requests. `HostService` installs the issuer's fresh authorization callback;
there is no static-grant fallback in this assembly. Original console operations
resolve the current configured owner on every boundary. Only that matched console
owner receives the existing console `domains:create` capability. MCP operations
retain their original issued scopes, expiry, revocation, and owner checks; this
helper does not widen or reissue credentials.

## Administrator configuration

Install a root-owned, non-group/other-writable JSON file. All fields below are
required; unknown fields and request-supplied commands are rejected. The paths
are examples requiring verification against the actual installation.

```json
{
  "schema_version": 1,
  "database": "/var/lib/deploy-monitor/state.sqlite",
  "database_uid": 0,
  "credentials_file": "/etc/deploy-monitor/credentials.json",
  "policy_file": "/etc/deploy-monitor/ingress-policy.json",
  "socket_path": "/run/deploy-monitor-ingress/host.sock",
  "allowed_uids": [0],
  "socket_group_gid": null,
  "docker_executable": "/opt/docker-engine/29.8.2/docker/docker",
  "docker_socket": "/run/docker-isolation.sock",
  "gateway_ports": {"first": 31000, "last": 31999},
  "timeout_seconds": 10,
  "apache": {
    "executable": "/usr/sbin/apache2",
    "config": "/etc/apache2/apache2.conf",
    "pid_file": "/run/apache2/apache2.pid",
    "listener": {"address": "127.0.0.1", "port": 80},
    "expected_argv": ["/usr/sbin/apache2", "-f", "/etc/apache2/apache2.conf", "-DFOREGROUND"],
    "source_roots": ["/etc/apache2", "/etc/letsencrypt", "/opt/docker-engine/projects/certificates"],
    "server_root": "/etc/apache2"
  }
}
```

`expected_argv` must match the actual Apache parent exactly, including its explicit
`-f` configuration. The helper never starts Apache. Supported administrator argv
flags are `-f`, `-d`, and `-D`; no shell, command snippets, hooks, or caller-selected
paths are supported. Observation requires an explicitly configured loopback
listener matching the host policy. Do not assume an existing public-IP-only
listener also accepts loopback traffic. Debian installations depending on shell
`envvars` must first provide an explicitly verified configuration usable with the
executor's minimal environment; this package does not source arbitrary scripts.

The database must already exist with the configured exact `database_uid` and
initialized domain/registry schema. It is opened read-write without permission
to create a missing database. The database owner may differ from root, but its
ancestors must remain trusted and not group/other writable. Credentials and host
policy remain owned by the helper's administrator UID. Symlink paths are rejected
for these writable/security-sensitive resources. The explicitly configured
Docker endpoint must be a trusted Unix socket; it is also used by gateway proof,
so port observation cannot silently inspect a different Docker daemon.

A minimal host policy, with separately prepared trusted directories, is:

```json
{
  "managed_dir": "/etc/apache2/deploy-monitor",
  "state_dir": "/var/lib/deploy-monitor/ingress-host",
  "listener": {"address": "127.0.0.1", "port": 80},
  "routes": {}
}
```

Apache's complete configuration must explicitly include the managed directory.
The helper cannot add that include or change listener configuration. Existing
foreign vhosts remain untouched. Alias inventory expands only bounded trusted
literal includes; unsupported macros/conditionals fail closed.

## Validation and service lifecycle

After preparing and verifying the configured files/directories, run the installed
Python with `ingress_host_daemon.py --config /etc/deploy-monitor/ingress-host.json
--check`. This validates assembly without creating a socket or signalling Apache.
It does not establish Docker health, public DNS, TLS, or end-to-end readiness.

Review `deployment/deploy-monitor-ingress-host.service` and its writable-path
allowlist against the actual configured policy and state paths before an
explicit installation step. The unit's `RuntimeDirectory` creates a root-private
socket directory. Default socket mode is `0600`; an explicitly configured admin
group uses `0660`, and the administrator must grant directory traversal to that
group without granting directory write access. The peer's exact UID must still
appear in `allowed_uids`. Never make the socket world-accessible.

SIGTERM/SIGINT stop acceptance after the active journaled operation finishes.
Idle acceptance polls every 250ms. The unit bounds shutdown at 90 seconds;
interrupted effects retain their journals for subsequent inspection/recovery.
Socket cleanup removes only the inode created by the process. A socket left by a
crash is deliberately not unlinked on restart: establish that no helper is alive,
verify the path and ownership, then remove only that stale socket as an explicit
administrative recovery action. Never delete policy or operation journals.

## Host port observation

`available_ports({})` returns `{ "ports": [...], "observed": <Unix timestamp> }`.
The range comes only from `gateway_ports`; callers cannot choose ranges or paths.
The helper checks actual host loopback binding and configured/runtime published
Docker ports, including dynamically assigned ports. Inventory and response sizes
are bounded; an oversized/incomplete inventory fails closed. No SQL transaction
is held for this operation.

This result is fresh observation, **not an OS lease**. The worker must record its
own reservation, recheck before creation, and treat an actual Docker bind failure
as failure. It must never substitute its container-local loopback as host proof.
Lost replies never trigger automatic mutation replay.

The opt-in `compose.ingress.yml` mounts only the host socket directory into the
worker and wires the integration key and pinned gateway image. Combine it with
`compose.yml`, and optionally `compose.mcp.yml`. Set `INGRESS_ENABLED=1` in the
Compose interpolation environment as well when enabling domain execution through
MCP; receiver/worker enablement is supplied by the ingress overlay. All paths
must already exist. `docker compose config --quiet` validates assembly without
starting services or creating directories.

This administrative package does not claim live provider DNS, certificate,
public TLS, or production activation acceptance. See the domain orchestration
acceptance record for local real-runtime evidence and its synthetic DNS boundary.

## Optional managed TLS (schema 2)

Schema 1 remains HTTP-only. Schema 2 has the same fields plus a required `tls`
object with exactly `listener`, `observation_listener`, `ca_file`, and
`certificates`. Both listeners contain `address` and `port`. The observation
listener must be loopback; the Apache vhost listener is separately configured.
`ca_file` is null for system trust or an administrator-owned CA bundle for a
private deployment. Never disable chain or hostname verification.

`certificates` has exactly these administrator-supplied fields:

- `executable`, `expected_version`: trusted Certbot executable and exact version.
- `server`, `account`, `email`, `agree_tos`: HTTPS ACME directory, preinstalled
  account identifier, contact address, and administrator terms policy.
- `config_dir`, `work_dir`, `logs_dir`, `challenge_root`, `state_dir`,
  `generations_dir`: dedicated trusted directories prepared before startup.
- `ca_bundle`: trusted certificate-issuer CA bundle.
- `timeout`: integer issuance deadline between 5 and 600 seconds.

Install the repository's `ingress_certificates.py` and `ingress_host_tls.py` along
with the helper modules, and use the project's pinned `cryptography==48.0.1`.
Certbot and its account are installation prerequisites; the helper does not
install software, create an ACME account, or adopt another application's lineage.
Apache must already have SSL, headers, alias, and proxy support and its TLS
listener configured. Run assembly `--check` before exposing the socket.

Keep all writable certificate directories in the product-owned `/var/lib/deploy-monitor`
namespace or explicitly extend the unit's exact writable-path allowlist. The
managed HTTP vhost includes a fixed challenge alias. An older HTTP configuration
without that alias is rejected for issuance and needs an owned configuration
upgrade; merely enabling schema 2 does not rewrite existing vhosts.

`capabilities({})` reports configured HTTP/TLS capabilities, not readiness.
Certificate requests retain the normal short connect/frame deadline, with a
fixed 720-second response window after peer/request validation. Certbot execution
has its own maximum 600-second deadline. Operations retain issuance and reload
journals after disconnects; callers inspect/recover instead of blindly replaying
commands. Stopping the service after 90 seconds may interrupt an in-flight ACME
operation, so a shutdown is not evidence that issuance failed. Origin HTTPS
verification is distinct from externally verified application readiness.
