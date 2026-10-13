"""Failures of external systems (Docker, git, deployment, disk)."""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for failures reported by an adapter with a user-presentable message."""


class ContainerRuntimeError(AdapterError):
    """The container runtime could not do what was asked (not: a container exited non-zero)."""


class ContainerNotFoundError(ContainerRuntimeError):
    """The container (or a path inside it) does not exist."""


class SourceError(AdapterError):
    """The source code could not be fetched or is not acceptable."""


class WorkspaceError(AdapterError):
    """A workspace could not be prepared or exceeds its bounds."""


class DeployError(AdapterError):
    """A deployment failed."""
