"""Crash recovery at worker start."""

from __future__ import annotations

import logging

from shiftcodes.execution.docker_agent import RUN_ID_LABEL
from shiftcodes.execution.environment import ExecutionEnv

logger = logging.getLogger(__name__)
INTERRUPTED_MESSAGE = "interrupted: the worker stopped or restarted while this run was in progress"


def recover_interrupted_runs(env: ExecutionEnv) -> list[int]:
    """Clean up after a worker that died mid-run; safe to call repeatedly.

    Containers of runs still marked running are killed and every workspace left on disk is
    removed (nothing can legitimately be running at start), then those runs are marked
    ``interrupted``. The status change comes last, so a crash during recovery is repeated by
    the next start. Returns the interrupted run ids.
    """
    lifecycle = env.lifecycle()
    running = lifecycle.running_run_ids()
    for run_id in running:
        env.runtime.kill_by_label(f"{RUN_ID_LABEL}={run_id}")
    for run_id in env.workspaces.existing_run_ids():
        env.workspaces.remove(run_id)
    interrupted = lifecycle.interrupt_running(INTERRUPTED_MESSAGE)
    if interrupted:
        logger.warning("interrupted runs recovered", extra={"run_ids": interrupted})
    return interrupted
