"""Validated value objects. Construction raises ``ValidationError`` on bad input."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from backend.domain.errors import ValidationError

_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_BRANCH_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_SECRET_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_SERVICE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
_COMPOSE_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
MAX_BRANCH_LENGTH = 200
MAX_NAME_LENGTH = 120


@dataclass(frozen=True, slots=True)
class ProjectSlug:
    """Lowercase DNS-label-like project identifier (1-64 chars)."""

    value: str

    def __post_init__(self) -> None:
        if not _SLUG.fullmatch(self.value):
            raise ValidationError(f"invalid project slug: {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class GitHubRepository:
    """GitHub repository in ``owner/name`` form."""

    value: str

    def __post_init__(self) -> None:
        owner, sep, name = self.value.partition("/")
        valid = (
            sep == "/"
            and _OWNER.fullmatch(owner) is not None
            and _REPO_NAME.fullmatch(name) is not None
            and name not in {".", ".."}
            and not name.endswith(".git")
        )
        if not valid:
            raise ValidationError(f"invalid GitHub repository: {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class BranchName:
    """Git branch name restricted to a safe subset of the git ref rules.

    Segments separated by ``/`` use only ``[A-Za-z0-9._-]``, never start with ``.``
    or ``-``, never end with ``.lock`` or ``.``, and ``..`` is forbidden.
    """

    value: str

    def __post_init__(self) -> None:
        if not _is_safe_branch(self.value):
            raise ValidationError(f"invalid branch name: {self.value!r}")

    def __str__(self) -> str:
        return self.value


def _is_safe_branch(value: str) -> bool:
    if not value or len(value) > MAX_BRANCH_LENGTH or ".." in value or value == "@":
        return False
    return all(_is_safe_segment(segment) for segment in value.split("/"))


def _is_safe_segment(segment: str) -> bool:
    return _BRANCH_SEGMENT.fullmatch(segment) is not None and not segment.endswith((".lock", "."))


@dataclass(frozen=True, slots=True)
class SecretName:
    """Environment-variable-style secret name."""

    value: str

    def __post_init__(self) -> None:
        if not _SECRET_NAME.fullmatch(self.value):
            raise ValidationError(f"invalid secret name: {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Username:
    """Lowercase login name of 3-64 characters."""

    value: str

    def __post_init__(self) -> None:
        if not _USERNAME.fullmatch(self.value):
            raise ValidationError(
                "username must be 3-64 characters of lowercase letters, digits, '.', '_' or '-'"
            )

    def __str__(self) -> str:
        return self.value


def validate_password(password: str) -> str:
    """Return ``password`` unchanged if it meets the length policy (never echoes it)."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValidationError(
            f"password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters"
        )
    return password


@dataclass(frozen=True, slots=True)
class CommitSha:
    """Full 40-character lowercase hexadecimal git commit id."""

    value: str

    def __post_init__(self) -> None:
        if not _COMMIT_SHA.fullmatch(self.value):
            raise ValidationError(f"invalid commit sha: {self.value!r}")

    def __str__(self) -> str:
        return self.value


def validate_display_name(name: str) -> str:
    """Return ``name`` stripped, requiring 1-120 characters."""
    stripped = name.strip()
    if not stripped or len(stripped) > MAX_NAME_LENGTH:
        raise ValidationError(f"name must be 1-{MAX_NAME_LENGTH} characters")
    return stripped


def validate_service_names(names: list[str]) -> list[str]:
    """Validate docker compose service names; duplicates are rejected."""
    for name in names:
        if not _SERVICE_NAME.fullmatch(name):
            raise ValidationError(f"invalid service name: {name!r}")
    _reject_duplicates(names, "service")
    return list(names)


def validate_compose_files(paths: list[str]) -> list[str]:
    """Validate compose file paths: absolute, no ``..`` parts, no NUL."""
    for raw in paths:
        path = PurePosixPath(raw)
        if "\0" in raw or not path.is_absolute() or ".." in path.parts:
            raise ValidationError(f"compose file must be a clean absolute path: {raw!r}")
    _reject_duplicates(paths, "compose file")
    return list(paths)


def validate_compose_env_files(paths: list[str]) -> list[str]:
    """Validate compose interpolation env file paths: absolute, no ``..`` parts, no NUL."""
    for raw in paths:
        path = PurePosixPath(raw)
        if "\0" in raw or not path.is_absolute() or ".." in path.parts:
            raise ValidationError(f"compose env file must be a clean absolute path: {raw!r}")
    _reject_duplicates(paths, "compose env file")
    return list(paths)


def validate_compose_project(name: str | None) -> str | None:
    """Validate a docker compose project name; ``None`` is allowed."""
    if name is not None and not _COMPOSE_PROJECT.fullmatch(name):
        raise ValidationError(f"invalid compose project name: {name!r}")
    return name


def _reject_duplicates(items: list[str], label: str) -> None:
    if len(set(items)) != len(items):
        raise ValidationError(f"duplicate {label} entries")
