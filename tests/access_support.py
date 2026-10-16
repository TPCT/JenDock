"""Helpers for the access-control tests: users with roles, logged-in clients, a fake gateway."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from httpx2 import Response

from backend.api.container import AppContainer
from backend.containers.logs import LogCursor
from backend.containers.wire import (
    ActionPlanView,
    ContainerView,
    EnvironmentPowerView,
    HostCapacityView,
    LogLineView,
    LogPageView,
    ResourceChangeView,
    ResourceLimitsView,
    ResourceOverviewView,
    ResourceUsageView,
    ServiceResourcesView,
)
from backend.domain.access import AccessRole, ContainerPermission, GlobalRole
from backend.domain.actor import Actor
from backend.domain.containers import LifecycleAction
from backend.domain.enums import EnvironmentName
from backend.domain.errors import NotFoundError
from backend.domain.resources import ResourceLimits
from tests.api_support import PASSWORD, login, make_client, mutation_headers

WEB = "a" * 64
WORKER = "b" * 64
SYSTEM = Actor.system("test")


def add_user(
    container: AppContainer,
    username: str,
    role: GlobalRole = GlobalRole.MEMBER,
    *,
    must_change: bool = False,
    password: str = PASSWORD,
) -> int:
    """Create a user with ``role`` through the real service."""
    user = container.user_service().create(
        username, password, SYSTEM, role=role, must_change_password=must_change
    )
    return user.id


@dataclass
class Session:
    """A logged-in browser: its client, CSRF token and user id."""

    client: TestClient
    csrf: str
    user_id: int

    def headers(self) -> dict[str, str]:
        """Origin and CSRF header for mutations."""
        return mutation_headers(self.client, self.csrf)

    def get(self, path: str) -> Response:
        """GET a path."""
        return self.client.get(path)

    def post(self, path: str, body: object = None) -> Response:
        """POST JSON."""
        return self.client.post(path, json=body, headers=self.headers())

    def put(self, path: str, body: object = None) -> Response:
        """PUT JSON."""
        return self.client.put(path, json=body, headers=self.headers())

    def patch(self, path: str, body: object = None) -> Response:
        """PATCH JSON."""
        return self.client.patch(path, json=body, headers=self.headers())

    def delete(self, path: str) -> Response:
        """DELETE."""
        return self.client.delete(path, headers=self.headers())


def sign_in(container: AppContainer, username: str, password: str = PASSWORD) -> Session:
    """A new logged-in browser for an existing user."""
    client = make_client(container)
    csrf = login(client, username, password)
    me = client.get("/api/auth/session").json()["user"]["id"]
    return Session(client, csrf, me)


def grant(
    container: AppContainer,
    slug: str,
    user_id: int,
    *,
    role: AccessRole | None = None,
    environment: EnvironmentName | None = None,
    service: str | None = None,
    permissions: Sequence[ContainerPermission] = (),
) -> None:
    """Add a grant through the real service."""
    container.access_service().set_grant(
        slug,
        user_id,
        SYSTEM,
        environment=environment,
        service=service,
        role=role,
        permissions=permissions,
    )


def _view(container_id: str, service: str) -> ContainerView:
    return ContainerView(
        id=container_id,
        name=f"demo-{service}-1",
        service=service,
        image=f"demo/{service}:1",
        image_id="sha256:" + "0" * 64,
        revision=None,
        commit=None,
        state="running",
        health=None,
        restart_count=0,
        started_at=datetime(2026, 10, 13, tzinfo=UTC),
        exit_code=None,
        cpus=None,
        memory_bytes=None,
        ports=(),
        mounts=(),
    )


_HOST = HostCapacityView(cpus=4, memory_bytes=8 * 1024**3)


class _FakeResources:
    """The resource-limits half of the fake gateway (records calls like the rest)."""

    calls: list[str]

    def resources_list(self, slug: str, env: EnvironmentName, actor: Actor) -> ResourceOverviewView:
        self.calls.append("resources_list")
        return ResourceOverviewView(
            host=_HOST,
            services=tuple(
                ServiceResourcesView(service=name, limits=None, updated_at=None)
                for name in ("web", "worker")
            ),
        )

    def resources_usage(self, slug: str, env: EnvironmentName, actor: Actor) -> ResourceUsageView:
        self.calls.append("resources_usage")
        return ResourceUsageView(host=_HOST, containers=())

    def resources_save(
        self,
        slug: str,
        env: EnvironmentName,
        service: str,
        limits: ResourceLimits,
        apply_now: bool,
        actor: Actor,
    ) -> ResourceChangeView:
        self.calls.append("resources_save")
        return ResourceChangeView(
            service=service,
            limits=ResourceLimitsView(
                cpus=limits.cpus,
                memory_bytes=limits.memory_bytes,
                memory_reservation_bytes=limits.memory_reservation_bytes,
                cpu_shares=limits.cpu_shares,
            ),
            applied=(),
            notes=(),
        )

    def resources_remove(
        self, slug: str, env: EnvironmentName, service: str, apply_now: bool, actor: Actor
    ) -> ResourceChangeView:
        self.calls.append("resources_remove")
        return ResourceChangeView(service=service, limits=None, applied=(), notes=())

    def resources_apply(
        self, slug: str, env: EnvironmentName, service: str, actor: Actor
    ) -> ResourceChangeView:
        self.calls.append("resources_apply")
        return ResourceChangeView(service=service, limits=None, applied=(), notes=())


class FakeContainers(_FakeResources):
    """Just enough of the worker's container gateway for the policy tests."""

    def __init__(self) -> None:
        self.views = (_view(WEB, "web"), _view(WORKER, "worker"))
        self.calls: list[str] = []
        self.power_calls: list[tuple[str, str, Actor]] = []

    def environment_power(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> EnvironmentPowerView:
        self.power_calls.append(("read", env.value, actor))
        return _power_view()

    def stop_environment(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> EnvironmentPowerView:
        self.power_calls.append(("stop", env.value, actor))
        return _power_view("STOPPED")

    def start_environment(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> EnvironmentPowerView:
        self.power_calls.append(("start", env.value, actor))
        return _power_view("RUNNING")

    def list_containers(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> Sequence[ContainerView]:
        self.calls.append("list")
        return self.views

    def get_container(
        self, slug: str, env: EnvironmentName, container_id: str, actor: Actor
    ) -> ContainerView:
        self.calls.append("get")
        for view in self.views:
            if view.id == container_id:
                return view
        raise NotFoundError("container not found")

    def logs(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        actor: Actor,
        *,
        tail: int,
        cursor: LogCursor | None,
    ) -> LogPageView:
        self.calls.append("logs")
        now = "2026-10-13T00:00:00Z"
        return LogPageView(
            lines=(LogLineView(timestamp=now, text="hello"),), cursor=None, dropped=0
        )

    def plan_action(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        action: LifecycleAction,
        actor: Actor,
    ) -> ActionPlanView:
        self.calls.append("plan")
        view = self.get_container(slug, env, container_id, actor)
        return ActionPlanView(
            action=action.value,
            container=view,
            effects=("restarts it",),
            confirmation="c" * 20,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )


def _power_view(state: str = "RUNNING") -> EnvironmentPowerView:
    return EnvironmentPowerView(
        state=state,
        running=1 if state == "RUNNING" else 0,
        total=2,
        auto_stop_hours=None,
        last_deployed_at=None,
        started_at=None,
        auto_stop_due_at=None,
    )


def with_fake_containers(container: AppContainer) -> tuple[AppContainer, FakeContainers]:
    """The container wired to a fake container gateway (the app must be built from the copy)."""
    fake = FakeContainers()
    return dataclasses.replace(container, containers=fake), fake  # type: ignore[arg-type]
