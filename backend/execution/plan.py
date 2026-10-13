"""Stage rows created up front so a run shows its whole shape from the start."""

from __future__ import annotations

from dataclasses import dataclass

from backend.pipeline.ast import ParallelBody, Pipeline, SequentialBody, Stage
from backend.services.run_progress import RunRecorder

CHECKOUT_NAME = "Checkout"
POST_NAME = "Post Actions"
SEPARATOR = " / "


def _walk(stages: tuple[Stage, ...], prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for stage in stages:
        path = (*prefix, stage.name)
        paths.append(path)
        if isinstance(stage.body, ParallelBody | SequentialBody):
            paths.extend(_walk(stage.body.stages, path))
    return paths


@dataclass(frozen=True, slots=True)
class StagePlan:
    """Ids of the stage rows of a run: checkout, the pipeline's stages, post actions."""

    checkout_id: int
    stage_ids: dict[tuple[str, ...], int]
    post_id: int | None

    @classmethod
    def create(cls, recorder: RunRecorder, run_id: int, pipeline: Pipeline) -> StagePlan:
        """Create the rows in display order."""
        paths = _walk(pipeline.stages, ())
        names = [CHECKOUT_NAME, *(SEPARATOR.join(path) for path in paths)]
        if pipeline.post is not None:
            names.append(POST_NAME)
        ids = recorder.plan_stages(run_id, names)
        mapping = dict(zip(paths, ids[1 : 1 + len(paths)], strict=True))
        post_id = ids[-1] if pipeline.post is not None else None
        return cls(ids[0], mapping, post_id)

    def id_for(self, path: tuple[str, ...]) -> int:
        """The row of the stage at ``path``."""
        return self.stage_ids[path]

    @property
    def all_stage_ids(self) -> list[int]:
        """Rows of the pipeline's own stages (not checkout or post actions)."""
        return list(self.stage_ids.values())
