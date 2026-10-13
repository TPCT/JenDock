"""Apply an ``ImportPlan`` idempotently through the normal services (so everything is audited).

Rules, so re-running is safe: missing things are created; blank compose settings of an existing
environment are filled; anything that exists and differs is only reported, never overwritten
(branches, auto deploy, non-empty compose settings, a different stored pipeline).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.errors import DomainError, NotFoundError
from shiftcodes.importer.plan import DEV, PROD, EnvironmentPlan, ImportPlan, ProjectPlan
from shiftcodes.repositories.models import Project
from shiftcodes.services.environments import EnvironmentService, EnvironmentUpdate
from shiftcodes.services.pipelines import PipelineService
from shiftcodes.services.projects import ProjectService

BLANK: tuple[object, ...] = (None, [], "")


@dataclass(frozen=True, slots=True)
class ImportServices:
    """The services the import needs (each with its own unit of work)."""

    projects: ProjectService
    environments: EnvironmentService
    pipelines: PipelineService


@dataclass(slots=True)
class ProjectOutcome:
    """What happened to one project."""

    slug: str
    created: bool = False
    changes: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply_plan(plan: ImportPlan, services: ImportServices, actor: Actor) -> list[ProjectOutcome]:
    """Apply every project; a failing project is reported and does not stop the others."""
    outcomes = []
    for project in plan.projects:
        outcome = ProjectOutcome(project.slug)
        try:
            _apply_project(project, services, actor, outcome)
        except DomainError as error:
            outcome.errors.append(str(error))
        outcome.errors.extend(project.errors)
        outcomes.append(outcome)
    return outcomes


def _apply_project(
    project: ProjectPlan, services: ImportServices, actor: Actor, outcome: ProjectOutcome
) -> None:
    try:
        existing = services.projects.get(project.slug)
    except NotFoundError:
        existing = None
    if existing is None:
        branches = {env.name: env.branch for env in project.environments}
        services.projects.create(
            project.slug,
            project.name,
            project.repository,
            development_branch=branches[DEV],
            production_branch=branches[PROD],
            git_transport=project.git_transport,
            actor=actor,
        )
        outcome.created = True
        outcome.changes.append(f"created project {project.slug} ({project.repository})")
    else:
        _compare_project(project, existing.project, outcome)
    for env in project.environments:
        _apply_environment(project, env, services, actor, outcome)


def _compare_project(project: ProjectPlan, current: Project, outcome: ProjectOutcome) -> None:
    for what, have, want in (
        ("name", current.name, project.name),
        ("repository", current.repository, project.repository),
        ("git transport", current.git_transport, project.git_transport),
    ):
        if have == want:
            outcome.unchanged.append(f"project {what} matches")
        else:
            outcome.differences.append(f"project {what}: v2 has {have!r}, v1 says {want!r}")


def _apply_environment(
    project: ProjectPlan,
    plan: EnvironmentPlan,
    services: ImportServices,
    actor: Actor,
    outcome: ProjectOutcome,
) -> None:
    label = f"{project.slug}/{plan.name.value}"
    current = services.environments.get(project.slug, plan.name)
    fields: dict[str, Any] = {}
    if current.branch != plan.branch:
        outcome.differences.append(
            f"{label}: branch is {current.branch!r}, v1 says {plan.branch!r}"
        )
    if outcome.created and current.auto_deploy != plan.auto_deploy:
        fields["auto_deploy"] = plan.auto_deploy
    for name, wanted in plan.values.items():
        have = getattr(current, name)
        if have == wanted:
            outcome.unchanged.append(f"{label}: {name} matches")
        elif have in BLANK:
            fields[name] = wanted
        else:
            outcome.differences.append(f"{label}: {name} is {have!r}, v1 says {wanted!r}")
    if fields:
        services.environments.update(
            project.slug,
            plan.name,
            EnvironmentUpdate(**fields),
            actor,
        )
        outcome.changes.append(f"{label}: set {', '.join(sorted(fields))}")
    _apply_pipeline(project.slug, plan, services, actor, outcome)


def _apply_pipeline(
    slug: str,
    plan: EnvironmentPlan,
    services: ImportServices,
    actor: Actor,
    outcome: ProjectOutcome,
) -> None:
    if plan.jenkinsfile is None:
        return
    label = f"{slug}/{plan.name.value}"
    stored = services.pipelines.latest_stored(slug, plan.name)
    if stored is None:
        saved = services.pipelines.save(slug, plan.name, plan.jenkinsfile, actor=actor)
        outcome.changes.append(f"{label}: stored Jenkinsfile as version {saved.version}")
    elif stored.jenkinsfile == plan.jenkinsfile:
        outcome.unchanged.append(f"{label}: stored Jenkinsfile (version {stored.version}) matches")
    else:
        outcome.differences.append(
            f"{label}: stored Jenkinsfile (version {stored.version}) differs from the one "
            "generated from v1; not overwritten"
        )
