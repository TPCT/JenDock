"""RPC methods about the pipeline of a repository (the part of pipelines that needs git)."""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.adapters.errors import SourceError
from backend.adapters.git_remote import BranchFileReader
from backend.domain.enums import EnvironmentName, GitTransport
from backend.repositories.database import UnitOfWork
from backend.services.lookup import require_environment, require_project
from backend.worker.rpc import BadRequestError, Handler, RpcCall

logger = logging.getLogger(__name__)
JENKINSFILE_PATH = "Jenkinsfile"


class _EnvParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(max_length=64)
    env: EnvironmentName


class PipelineHandlers:
    """Adapts a ``BranchFileReader`` and the project data to RPC calls."""

    def __init__(self, uow_factory: Callable[[], UnitOfWork], reader: BranchFileReader) -> None:
        self._uow_factory = uow_factory
        self._reader = reader

    def repository_file(self, call: RpcCall) -> object:
        """``pipeline.repository_file``: the ``Jenkinsfile`` at the head of the environment branch.

        Answers ``{"reachable": bool, "text": str | None}``. ``text`` is ``None`` when the branch
        or the file does not exist; ``reachable`` is false when git could not read the repository
        at all (the reason goes to the log, never to the client).
        """
        try:
            params = _EnvParams.model_validate(call.params)
        except PydanticValidationError as error:
            raise BadRequestError("invalid params") from error
        call.require_actor()
        repository, transport, branch = self._locate(params)
        try:
            text = self._reader.read_file(repository, transport, branch, JENKINSFILE_PATH)
        except SourceError as error:
            logger.warning(
                "cannot read the Jenkinsfile of a repository",
                extra={"slug": params.slug, "reason": str(error)},
            )
            return {"reachable": False, "text": None}
        return {"reachable": True, "text": text}

    def _locate(self, params: _EnvParams) -> tuple[str, GitTransport, str]:
        uow = self._uow_factory()
        with uow:
            project = require_project(uow, params.slug)
            environment = require_environment(uow, params.slug, params.env)
            return project.repository, project.git_transport, environment.branch

    def handlers(self) -> dict[str, Handler]:
        """The methods to register with the RPC server."""
        return {"pipeline.repository_file": self.repository_file}
