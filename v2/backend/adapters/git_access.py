"""How git reaches a repository: the URL, the environment and a pre-flight check."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.adapters.errors import SourceError
from backend.domain.values import GitHubRepository

SSH_HOST = "ssh.github.com"
SSH_PORT = 443
# Names the host in known_hosts; the pinned file lists github.com, the connection goes to
# ssh.github.com:443 (port 22 is blocked on some networks, v1 used the same arrangement).
SSH_HOST_KEY_ALIAS = "github.com"
SSH_CONNECT_TIMEOUT = 15
SSH_NULL_FILE = "/dev/null"


@dataclass(frozen=True, slots=True)
class GitAccess:
    """Everything git needs to reach the repositories of one transport.

    ``resolve`` maps ``owner/name`` to a clone URL, ``env_for`` gives the variables added to
    the git process for that repository (a deploy key is per repository) and ``check`` raises
    ``SourceError`` when the transport is not usable for it.
    """

    resolve: Callable[[str], str]
    env_for: Callable[[str], Mapping[str, str]]
    check: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SshSettings:
    """Deploy keys and pinned host keys for GitHub over ssh.github.com:443.

    A repository uses ``<key_dir>/<owner>__<name>`` when that file exists, else ``key_file``.
    """

    key_file: Path | None
    known_hosts_file: Path
    key_dir: Path | None = None
    host: str = SSH_HOST
    port: int = SSH_PORT
    host_key_alias: str = SSH_HOST_KEY_ALIAS
    connect_timeout: int = SSH_CONNECT_TIMEOUT


def repository_key_name(repository: str) -> str:
    """File name of a repository's deploy key: ``owner__name``; validated, never a path."""
    owner, _, name = str(GitHubRepository(repository)).partition("/")
    return f"{owner}__{name}"


def deploy_key_path(key_dir: Path, repository: str) -> Path:
    """Where the deploy key of ``owner/name`` lives inside ``key_dir``."""
    return key_dir / repository_key_name(repository)


def resolve_key_file(settings: SshSettings, repository: str) -> Path:
    """The key for one repository: its own file, else the shared one, else a clear error."""
    if settings.key_dir is not None:
        own = deploy_key_path(settings.key_dir, repository)
        if own.is_file():
            return own
    else:
        repository_key_name(repository)  # validate even without a directory
    if settings.key_file is not None:
        return settings.key_file
    raise SourceError(
        "SSH access is not configured: create a deploy key for the repository "
        f"(python -m backend.cli deploy-key create {repository}), or set "
        "JENDOCK_GIT_SSH_KEY_FILE to a shared deploy key, or switch the project to https"
    )


def ssh_argv(settings: SshSettings, key_file: Path) -> list[str]:
    """The ssh client invocation as an argv list (git appends the host and the command).

    Only the deploy key is offered (``IdentitiesOnly``, no agent), the host key must match the
    pinned file (``StrictHostKeyChecking=yes``, no other known_hosts, no user ssh config) and
    nothing can prompt (``BatchMode``). Only ed25519 host keys are accepted, the key type
    that is pinned.
    """
    options = {
        "IdentitiesOnly": "yes",
        "IdentityAgent": "none",
        "BatchMode": "yes",
        "PasswordAuthentication": "no",
        "StrictHostKeyChecking": "yes",
        "UserKnownHostsFile": str(settings.known_hosts_file),
        "GlobalKnownHostsFile": SSH_NULL_FILE,
        "HostKeyAlgorithms": "ssh-ed25519",
        "HostKeyAlias": settings.host_key_alias,
        "Hostname": settings.host,
        "ConnectTimeout": str(settings.connect_timeout),
        "ForwardAgent": "no",
        "ClearAllForwardings": "yes",
    }
    argv = ["ssh", "-F", SSH_NULL_FILE, "-i", str(key_file), "-p", str(settings.port)]
    for name, value in options.items():
        argv.extend(["-o", f"{name}={value}"])
    return argv


def ssh_command(settings: SshSettings, key_file: Path) -> str:
    """``GIT_SSH_COMMAND`` value: git runs it through ``sh -c``, so every token is quoted."""
    return shlex.join(ssh_argv(settings, key_file))


def ssh_url(repository: str) -> str:
    """The clone URL of a GitHub repository; the name is validated again here."""
    return f"ssh://git@{SSH_HOST_KEY_ALIAS}/{GitHubRepository(repository)}.git"


def ssh_access(settings: SshSettings) -> GitAccess:
    """Deploy-key access to GitHub; the key is chosen per repository (see ``SshSettings``)."""

    def check(repository: str) -> None:
        key = resolve_key_file(settings, repository)
        if not key.is_file():
            raise SourceError(f"the deploy key file does not exist: {key}")
        if not settings.known_hosts_file.is_file():
            raise SourceError(
                f"the pinned known_hosts file does not exist: {settings.known_hosts_file}"
            )

    def env_for(repository: str) -> Mapping[str, str]:
        key = resolve_key_file(settings, repository)
        return {"GIT_SSH_COMMAND": ssh_command(settings, key)}

    return GitAccess(ssh_url, env_for, check)


def https_access(template: str) -> GitAccess:
    """Plain HTTPS access (public repositories) through a URL template."""

    def resolve(repository: str) -> str:
        return template.format(repository=repository)

    return GitAccess(resolve, lambda _repository: {}, lambda _repository: None)
