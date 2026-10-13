"""Shared entity lookups that raise ``NotFoundError`` instead of returning ``None``."""

from __future__ import annotations

from backend.domain.enums import EnvironmentName
from backend.domain.errors import NotFoundError
from backend.domain.values import ProjectSlug
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Environment, Project


def require_project(uow: UnitOfWork, slug: str) -> Project:
    """Return the project with ``slug``; the unit of work must be active."""
    project = uow.projects.get_by_slug(str(ProjectSlug(slug)))
    if project is None:
        raise NotFoundError(f"project not found: {slug}")
    return project


def require_environment(uow: UnitOfWork, slug: str, name: EnvironmentName) -> Environment:
    """Return an environment by project slug and name; the unit of work must be active."""
    environment = uow.environments.get_by_project_slug(str(ProjectSlug(slug)), name)
    if environment is None:
        raise NotFoundError(f"environment not found: {slug}/{name.value}")
    return environment
