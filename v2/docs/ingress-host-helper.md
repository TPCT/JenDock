# Host helper: installation (opt-in)

The host helper is the one small privileged process of the ingress feature (design and
protocol: `ingress.md`). Nothing in this repository installs, starts or reconfigures it; the
steps below are for the administrator, who must verify every path against the actual host
(Apache binary, how it is started, include layout) first. v1's helper design
(`docs/operations/ingress-host-helper-setup.md` in the repository root) is the model: a Unix
socket, a root-owned configuration file, an allowlist and nothing free-form.

## What it may touch (and nothing else)

- one **managed vhost directory** (files `sc-<hostname>.conf`, each starting with the marker
  `# jendock-managed:`; files without it are never modified),
- one **certificate directory** (`<dir>/<hostname>/fullchain.pem` 0644, `privkey.pem` 0600),
- one **ACME webroot** (`<dir>/.well-known/acme-challenge/<token>`),
- `apachectl configtest` and `apachectl graceful` with the argv from the config file.

It runs `apachectl` with an empty environment (`PATH` only, `LANG=C`); it never starts Apache.

## 1. Apache preparation (administrator)

- Modules enabled: `proxy proxy_http proxy_wstunnel rewrite headers alias ssl`
  (`a2enmod ...` on Debian) and `setifempty` support (Apache >= 2.4.7).
- Create the three directories. Example (adjust to your layout):

  ```
  install -d -m 0755 /etc/apache2/jendock-managed /etc/jendock/certs /var/lib/jendock/acme-webroot
  ```
- Include the managed directory **from the main configuration, outside any `<VirtualHost>`**,
  e.g. a file `conf-available/jendock.conf`: `IncludeOptional /etc/apache2/jendock-managed/sc-*.conf`
  and `a2enconf jendock`. The helper cannot add the include and never edits foreign files.
  Make sure Apache listens on 80 and 443 (`Listen`).
- `apachectl configtest` must pass **before** the helper starts. Debian's `apache2ctl` sources
  `/etc/apache2/envvars`; if your Apache is managed by systemd, point `apachectl` at a tiny
  root-owned wrapper that maps `configtest` to `apache2ctl configtest` and `graceful` to
  `systemctl reload apache2` (the helper passes exactly one of those two words).

## 2. Install the helper code

The helper needs only Python >= 3.13 and `cryptography` (already pinned in
`requirements.lock`); it imports nothing else from the platform. For example:

```
python3 -m venv /opt/jendock-host-helper/venv
/opt/jendock-host-helper/venv/bin/pip install 'cryptography==<version from requirements.lock>'
install -d -m 0755 /opt/jendock-host-helper/lib/jendock
cp -r v2/jendock/__init__.py v2/backend/hosthelper /opt/jendock-host-helper/lib/jendock/
chown -R root:root /opt/jendock-host-helper && chmod -R go-w /opt/jendock-host-helper
```

(Alternatively `pip install` the whole `v2/` project into that venv.) Copy a release, do not
point the unit at a working tree a non-root user can modify.

## 3. Configuration

`/etc/jendock/host-helper.json`, owned by root, not group/other writable (the helper
refuses a symlink, a writable file or a file owned by another user). See
`deployment/host-helper.example.json`:

| key | meaning |
| --- | --- |
| `socket_path` | e.g. `/run/jendock-ingress/host.sock` |
| `socket_mode` | octal string, never world access (default `0660`) |
| `socket_group` | host group that may connect (optional) |
| `allowed_uids` | **required**; kernel-verified peer uid(s), e.g. `[10001]` (the worker user) |
| `vhost_dir`, `cert_dir`, `webroot_dir` | the three directories above (absolute, no `..`) |
| `apachectl` | argv prefix, absolute path, e.g. `["/usr/sbin/apachectl"]` |
| `allowed_hostname_suffixes` | zones the platform may serve, e.g. `["example.com"]` (label-boundary match) |
| `allowed_upstream_ports` | `[[first, last], ...]`, each within 1024-65535 |
| `allow_wildcards` | default false |
| `max_vhosts`, `command_timeout_seconds` | limits (defaults 200 / 30) |

The zone allowlist and port ranges are the blast radius of a compromised worker: keep them
as small as the deployment allows (use the same port range your compose files publish on).

Check without serving: `python -m backend.hosthelper --config /etc/jendock/host-helper.json --check`
(validates the file and runs `apachectl configtest`; exit 0 only if both pass).

## 4. Service

`deployment/jendock-host-helper.service` is a hardened systemd unit (root, but
`ProtectSystem=strict` with write access only to the three directories and the socket
directory, `NoNewPrivileges`, private `/tmp`). Review its `ReadWritePaths` against your
configuration, then `systemctl enable --now jendock-host-helper`.

Socket access: the worker container runs as uid/gid 10001. Create a host group with gid 10001
(or use `allowed_uids` with the uid you observe on the host) and set `socket_group` to it; the
runtime directory is `0750 root:<group>` and the socket `0660`. **Never make the socket
world-accessible.** The peer uid is additionally checked against `allowed_uids`.

## 5. Connect the platform

Use `compose.ingress.yml` as an overlay (`docker compose -f compose.yml -f compose.ingress.yml
up -d`): it bind-mounts the socket directory into the worker and sets the
`JENDOCK_INGRESS_*` variables (set `INGRESS_PUBLIC_IPV4`, `INGRESS_ACME_EMAIL`, and use the
Let's Encrypt **staging** directory first). Then, per environment:

1. Store the Cloudflare token as secret `CLOUDFLARE_API_TOKEN` of the environment (token scoped
   to Zone:DNS:Edit + Zone:Zone:Read for that zone only).
2. Create the domain, preview, review the DNS/vhost/TLS plan, apply.

## Operations

- A failed `configtest` or reload is reverted automatically by the helper and reported as
  `configtest_failed` / `reload_failed` with Apache's own message (the API shows it).
- A crash leaves, at worst, a stale socket file; the helper removes a socket nobody is
  listening on at startup and refuses to start if another helper is listening or if the path
  is a regular file.
- Certificates: renewals are installed with the same atomic replace, tested and reloaded; the
  previous files are restored when the test fails.
- Uninstall: remove the include, then `rm` the `sc-*.conf` files and the certificate directory.
  DNS records tagged `managed-by=jendock` can be removed by the platform (`remove`).
