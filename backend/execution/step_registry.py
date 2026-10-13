"""Registry of step implementations keyed by step name."""

from __future__ import annotations

from collections.abc import Mapping

from backend.execution.step_base import Step
from backend.execution.steps_basic import (
    DirStep,
    EchoStep,
    ErrorStep,
    RetryStep,
    TimeoutStep,
    WithEnvStep,
)
from backend.execution.steps_deploy import DeployStep, HealthCheckStep
from backend.execution.steps_docker import DockerBuildStep, ShStep

STEP_CLASSES: Mapping[str, type[Step]] = {
    step.name: step
    for step in (
        ShStep,
        EchoStep,
        ErrorStep,
        TimeoutStep,
        RetryStep,
        DirStep,
        WithEnvStep,
        DockerBuildStep,
        DeployStep,
        HealthCheckStep,
    )
}
