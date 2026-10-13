"""Git access of the worker, built from settings: transports, sources and branch heads."""

from __future__ import annotations

from shiftcodes.adapters.git_access import (
    GitAccess,
    SshSettings,
    https_access,
    ssh_access,
    unavailable_ssh_access,
)
from shiftcodes.adapters.git_remote import GitBranchHeads
from shiftcodes.adapters.git_sources import GitAccessSource, TransportRoutingSource
from shiftcodes.config import Settings
from shiftcodes.domain.enums import GitTransport


def git_accesses(settings: Settings) -> dict[GitTransport, GitAccess]:
    """The access of each transport; ssh explains itself when no deploy key is configured."""
    if settings.git_ssh_key_file is None:
        ssh = unavailable_ssh_access()
    else:
        ssh = ssh_access(SshSettings(settings.git_ssh_key_file, settings.git_ssh_known_hosts_file))
    return {GitTransport.SSH: ssh, GitTransport.HTTPS: https_access(settings.git_url_template)}


def build_source(settings: Settings) -> TransportRoutingSource:
    """The source provider of runs: each project's transport decides how it is cloned."""
    providers = {
        transport: GitAccessSource(access, settings.git_binary)
        for transport, access in git_accesses(settings).items()
    }
    return TransportRoutingSource(providers)


def build_heads(settings: Settings) -> GitBranchHeads:
    """Branch head lookups over the same transports."""
    return GitBranchHeads(git_accesses(settings), settings.git_binary)
