"""``post`` sections with Jenkins semantics."""

from __future__ import annotations

from collections.abc import Callable

from shiftcodes.domain.enums import RunStatus
from shiftcodes.execution.errors import RunCancelled, StepFailed
from shiftcodes.execution.executor import StepExecutor
from shiftcodes.execution.scope import Scope
from shiftcodes.execution.state import RunState
from shiftcodes.pipeline.ast import Post, PostCondition


class PostRunner:
    """Runs the ``post`` blocks whose condition matches, in Jenkins' order (cleanup last)."""

    def __init__(self, executor: StepExecutor, state: RunState) -> None:
        self._executor = executor
        self._state = state

    def run(
        self,
        post: Post | None,
        scope: Scope,
        stage_id: int,
        outcome: RunStatus,
        changed: Callable[[], bool],
    ) -> bool:
        """Run the matching blocks; return False when one of their steps failed.

        ``outcome`` is SUCCEEDED, FAILED or CANCELLED. ``unstable`` never matches because no
        step can mark a run unstable. A failing block does not stop the following ones.
        """
        if post is None:
            return True
        order = list(PostCondition)
        blocks = sorted(post.blocks, key=lambda block: order.index(block.condition))
        ok = True
        for block in blocks:
            if not _wanted(block.condition, outcome, changed):
                continue
            try:
                self._executor.run_all(block.steps, scope.for_post(), stage_id)
            except StepFailed as error:
                ok = False
                self._state.fail(f"post {block.condition.value} failed: {error}")
            except RunCancelled:  # post steps are not cancellable; kept for completeness
                ok = False
        return ok


def _wanted(condition: PostCondition, outcome: RunStatus, changed: Callable[[], bool]) -> bool:
    match condition:
        case PostCondition.ALWAYS | PostCondition.CLEANUP:
            return True
        case PostCondition.CHANGED:
            return changed()
        case PostCondition.ABORTED:
            return outcome is RunStatus.CANCELLED
        case PostCondition.FAILURE:
            return outcome is RunStatus.FAILED
        case PostCondition.SUCCESS:
            return outcome is RunStatus.SUCCEEDED
        case PostCondition.UNSTABLE:
            return False
