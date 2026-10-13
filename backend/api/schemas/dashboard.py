"""Schema of ``GET /api/dashboard``."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from backend.containers.wire import HostSampleView
from backend.domain.enums import DeploymentStatus, EnvironmentName, RunStatus, RunTrigger
from backend.services.dashboard import (
    AttentionItem,
    CertificateItem,
    DashboardData,
    DeploymentItem,
    ManualDeployItem,
)


class DashboardDeployment(BaseModel):
    """A recent deployment."""

    id: int
    project: str
    project_name: str
    environment: EnvironmentName
    status: DeploymentStatus
    commit_sha: str
    run_id: int
    run_number: int
    trigger: RunTrigger
    triggered_by: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None

    @classmethod
    def of(cls, item: DeploymentItem) -> DashboardDeployment:
        """Build from the read model; the duration is known once started and finished."""
        duration = None
        if item.started_at and item.finished_at:
            duration = max(0.0, (item.finished_at - item.started_at).total_seconds())
        return cls(**_fields(item), duration_seconds=duration)


class DashboardRun(BaseModel):
    """A run that needs attention (active, or the newest of its environment and failed)."""

    run_id: int
    project: str
    project_name: str
    environment: EnvironmentName
    number: int
    status: RunStatus
    branch: str
    commit_sha: str
    trigger: RunTrigger
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None

    @classmethod
    def of(cls, item: AttentionItem) -> DashboardRun:
        """Build from the read model."""
        return cls(**_fields(item))


class DashboardManualDeploy(BaseModel):
    """An environment with auto-deploy off that received pushes."""

    project: str
    project_name: str
    environment: EnvironmentName
    branch: str
    pushes: int
    last_push_at: datetime

    @classmethod
    def of(cls, item: ManualDeployItem) -> DashboardManualDeploy:
        """Build from the read model."""
        return cls(**_fields(item))


class DashboardCertificate(BaseModel):
    """A certificate close to (or past) its expiry."""

    hostname: str
    project: str
    environment: EnvironmentName
    not_after: datetime
    expired: bool

    @classmethod
    def of(cls, item: CertificateItem) -> DashboardCertificate:
        """Build from the read model."""
        return cls(**_fields(item))


class DashboardHost(BaseModel):
    """Latest CPU, memory and disk readings of the host."""

    timestamp: datetime
    cpu_percent: float | None
    cpu_count: int
    load_1: float
    memory_used: int
    memory_total: int
    memory_percent: float
    disk_used: int
    disk_total: int
    disk_percent: float

    @classmethod
    def of(cls, sample: HostSampleView) -> DashboardHost:
        """Summarize one host sample."""
        return cls(
            timestamp=sample.timestamp,
            cpu_percent=sample.cpu_percent,
            cpu_count=sample.cpu_count,
            load_1=sample.load_1,
            memory_used=sample.memory_used,
            memory_total=sample.memory_total,
            memory_percent=_percent(sample.memory_used, sample.memory_total),
            disk_used=sample.disk_used,
            disk_total=sample.disk_total,
            disk_percent=_percent(sample.disk_used, sample.disk_total),
        )


class DashboardResponse(BaseModel):
    """The whole home dashboard. Each list is capped (10 entries per query)."""

    project_count: int
    domain_count: int
    deployments: list[DashboardDeployment]
    attention: list[DashboardRun]
    manual_deploys: list[DashboardManualDeploy]
    certificates: list[DashboardCertificate]
    host: DashboardHost | None
    host_error: str | None

    @classmethod
    def build(
        cls, data: DashboardData, host: HostSampleView | None, host_error: str | None
    ) -> DashboardResponse:
        """Combine the database cards with the (optional) host reading."""
        return cls(
            project_count=data.project_count,
            domain_count=data.domain_count,
            deployments=[DashboardDeployment.of(i) for i in data.deployments],
            attention=[DashboardRun.of(i) for i in data.attention],
            manual_deploys=[DashboardManualDeploy.of(i) for i in data.manual_deploys],
            certificates=[DashboardCertificate.of(i) for i in data.certificates],
            host=DashboardHost.of(host) if host is not None else None,
            host_error=host_error,
        )


def _percent(used: int, total: int) -> float:
    return round(100.0 * used / total, 1) if total > 0 else 0.0


def _fields(item: Any) -> dict[str, Any]:
    return {field.name: getattr(item, field.name) for field in fields(item)}
