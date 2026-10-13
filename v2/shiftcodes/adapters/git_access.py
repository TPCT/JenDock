"""How git reaches a repository: the URL, the environment and a pre-flight check."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from shiftcodes.adapters.errors import SourceError
from shiftcodes.domain.values import GitHubRepository

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

    ``resolve`` maps ``owner/name`` to a clone URL, ``env`` is added to the git process
    environment and ``check`` raises ``SourceError`` when the transport is not usable.
    """

    resolve: Callable[[str], str]
    env: Mapping[str, str]
    check: Callable[[], None]


@dataclass(frozen=True, slots=True)
class SshSettings:
    """Deploy key and pinned host keys for GitHub over ssh.github.com:443."""

    key_file: Path
    known_hosts_file: Path
    host: str = SSH_HOST
    port: int = SSH_PORT
    host_key_alias: str = SSH_HOST_KEY_ALIAS
    connect_timeout: int = SSH_CONNECT_TIMEOUT


def ssh_argv(settings: SshSettings) -> list[str]:
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
    argv = ["ssh", "-F", SSH_NULL_FILE, "-i", str(settings.key_file), "-p", str(settings.port)]
    for name, value in options.items():
        argv.extend(["-o", f"{name}={value}"])
    return argv


def ssh_command(settings: SshSettings) -> str:
    """``GIT_SSH_COMMAND`` value: git runs it through ``sh -c``, so every token is quoted."""
    return shlex.join(ssh_argv(settings))


def ssh_url(repository: str) -> str:
    """The clone URL of a GitHub repository; the name is validated again here."""
    return f"ssh://git@{SSH_HOST_KEY_ALIAS}/{GitHubRepository(repository)}.git"


def ssh_access(settings: SshSettings) -> GitAccess:
    """Deploy-key access to GitHub; ``check`` verifies that the key and host keys exist."""

    def check() -> None:
        if not settings.key_file.is_file():
            raise SourceError(f"the deploy key file does not exist: {settings.key_file}")
        if not settings.known_hosts_file.is_file():
            raise SourceError(
                f"the pinned known_hosts file does not exist: {settings.known_hosts_file}"
            )

    return GitAccess(ssh_url, {"GIT_SSH_COMMAND": ssh_command(settings)}, check)


def unavailable_ssh_access() -> GitAccess:
    """Access for a deployment without a deploy key: every use explains what is missing."""

    def check() -> None:
        raise SourceError(
            "SSH access is not configured: set SHIFTCODES_GIT_SSH_KEY_FILE to a deploy key "
            "or switch the project to https"
        )

    return GitAccess(ssh_url, {}, check)


def https_access(template: str) -> GitAccess:
    """Plain HTTPS access (public repositories) through a URL template."""

    def resolve(repository: str) -> str:
        return template.format(repository=repository)

    return GitAccess(resolve, {}, lambda: None)
