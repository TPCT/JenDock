"""Query-string filters shared by the run lists."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query

from backend.domain.enums import EnvironmentName, RunStatus, RunTrigger
from backend.repositories.runs import RunFilter


class RunFilterParams:
    """``status`` and ``trigger`` filters of one environment's runs."""

    def __init__(
        self,
        status: Annotated[RunStatus | None, Query()] = None,
        trigger: Annotated[RunTrigger | None, Query()] = None,
    ) -> None:
        self.status = status
        self.trigger = trigger

    def filter(self) -> RunFilter:
        """The repository filter."""
        return RunFilter(status=self.status, trigger=self.trigger)


class GlobalRunFilterParams(RunFilterParams):
    """The filters of the all-projects run list: also ``project`` (slug) and ``environment``."""

    def __init__(
        self,
        status: Annotated[RunStatus | None, Query()] = None,
        trigger: Annotated[RunTrigger | None, Query()] = None,
        project: Annotated[str | None, Query(max_length=64)] = None,
        environment: Annotated[EnvironmentName | None, Query()] = None,
    ) -> None:
        super().__init__(status, trigger)
        self.project = project
        self.environment = environment

    def filter(self) -> RunFilter:
        """The repository filter."""
        return RunFilter(
            project=self.project,
            environment=self.environment,
            status=self.status,
            trigger=self.trigger,
        )


RunFilters = Annotated[RunFilterParams, Depends()]
GlobalRunFilters = Annotated[GlobalRunFilterParams, Depends()]
