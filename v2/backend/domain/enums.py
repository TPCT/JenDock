"""Domain enumerations. Values are the strings persisted in the database."""

from __future__ import annotations

from enum import StrEnum


class EnvironmentName(StrEnum):
    """The two fixed environments every project has."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class RunStatus(StrEnum):
    """Lifecycle state of a run, stage or step."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"

    @property
    def is_finished(self) -> bool:
        """Whether no further transition is expected (queued and running are not final)."""
        return self not in {RunStatus.QUEUED, RunStatus.RUNNING}


class RunTrigger(StrEnum):
    """What started a run."""

    PUSH = "push"
    MANUAL = "manual"
    REDEPLOY = "redeploy"


class DeploymentStatus(StrEnum):
    """Outcome of one deployment of images to an environment."""

    PENDING = "pending"  # a redeploy that is queued but has not started
    DEPLOYING = "deploying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # nothing was changed
    ROLLED_BACK = "rolled_back"  # the new images failed; the previous ones run again
    ROLLBACK_FAILED = "rollback_failed"  # the new images failed and so did the rollback


class PipelineSource(StrEnum):
    """Where the pipeline text of a run came from."""

    REPOSITORY = "repository"
    PLATFORM = "platform"


class ActorType(StrEnum):
    """Kind of principal that performed an audited action."""

    USER = "user"
    TOKEN = "token"  # noqa: S105 - actor kind, not a credential
    SYSTEM = "system"


class TokenScope(StrEnum):
    """Permission an API token can carry. ``admin`` implies every other scope."""

    READ = "read"
    RUN = "run"
    DEPLOY = "deploy"
    CONTAINERS = "containers"
    TERMINAL = "terminal"
    PIPELINES = "pipelines"
    ADMIN = "admin"


class GitTransport(StrEnum):
    """How a project's repository is cloned."""

    SSH = "ssh"  # deploy key over ssh.github.com:443
    HTTPS = "https"  # public repositories only
