"""Typed, validated view of the v1 configuration (``projects.json`` and the v1 SQLite registry).

Read-only: nothing here writes to v1 files. Unknown keys are not an error but are collected as
warnings, so a v1 field the importer does not understand is never silently lost.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from backend.domain.errors import ValidationError

PROJECT_KEYS = frozenset(
    {
        "repository",
        "repository_id",
        "branch",
        "enabled",
        "reason",
        "image_prefixes",
        "label",
        "container_prefixes",
        "recipe",
    }
)
RECIPE_KEYS = frozenset(
    {"compose_project", "env_file", "compose_files", "services", "images", "tests", "http_checks"}
)
IMAGE_KEYS = frozenset(
    {"name", "target", "dockerfile", "context", "env_key", "env_mode", "deploy", "build_args"}
)
TEST_KEYS = frozenset({"image", "environment", "command", "tmpfs"})
CHECK_KEYS = frozenset({"url", "statuses"})
DEFAULT_ENV_MODE = "image"


@dataclass(frozen=True, slots=True)
class V1Image:
    """One image of a v1 recipe."""

    name: str
    dockerfile: str
    context: str | None
    target: str | None
    build_args: Mapping[str, str]
    env_key: str | None
    env_mode: str
    deploy: bool


@dataclass(frozen=True, slots=True)
class V1Test:
    """One isolated test command run in a built image."""

    image: str
    environment: Mapping[str, str]
    command: tuple[str, ...]
    tmpfs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class V1Check:
    """An HTTP check after deployment."""

    url: str
    statuses: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class V1Recipe:
    """The delivery recipe of a v1 project."""

    compose_project: str | None
    env_file: str | None
    compose_files: tuple[str, ...]
    services: tuple[str, ...]
    images: tuple[V1Image, ...]
    tests: tuple[V1Test, ...]
    checks: tuple[V1Check, ...]


@dataclass(frozen=True, slots=True)
class V1Project:
    """One entry of ``projects.json``."""

    key: str
    repository: str
    repository_id: int | None
    branch: str
    enabled: bool
    label: str
    recipe: V1Recipe | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class V1EnvironmentRow:
    """A ``registry_environments`` row."""

    name: str
    branch: str
    automatic: bool
    state: str


@dataclass(frozen=True, slots=True)
class V1RegistryEntry:
    """What the v1 database knows about one project (all parts optional)."""

    legacy_binding: str | None = None
    environments: Mapping[str, V1EnvironmentRow] = field(default_factory=dict)
    saved_branch: str | None = None
    saved_automatic: bool | None = None


V1Registry = Mapping[str, V1RegistryEntry]


def load_projects(path: Path) -> list[V1Project]:
    """Parse ``projects.json``; raises ``ValidationError`` for a malformed document."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValidationError(f"cannot read v1 projects file {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValidationError("v1 projects file must be a JSON object keyed by project")
    return [_project(str(key), value) for key, value in raw.items()]


def _project(key: str, raw: object) -> V1Project:
    data = _mapping(raw, f"project {key}")
    warnings = _unknown(data, PROJECT_KEYS, f"project {key}")
    recipe = data.get("recipe")
    parsed = _recipe(key, recipe, warnings) if recipe else None
    repository_id = data.get("repository_id")
    return V1Project(
        key=key,
        repository=_text(data, "repository", key),
        repository_id=repository_id if isinstance(repository_id, int) else None,
        branch=_text(data, "branch", key),
        enabled=bool(data.get("enabled", True)),
        label=str(data.get("label") or key),
        recipe=parsed,
        warnings=tuple(warnings),
    )


def _recipe(key: str, raw: object, warnings: list[str]) -> V1Recipe:
    data = _mapping(raw, f"recipe of {key}")
    warnings.extend(_unknown(data, RECIPE_KEYS, f"recipe of {key}"))
    return V1Recipe(
        compose_project=_optional_text(data.get("compose_project")),
        env_file=_optional_text(data.get("env_file")),
        compose_files=tuple(str(item) for item in _list(data.get("compose_files"))),
        services=tuple(str(item) for item in _list(data.get("services"))),
        images=tuple(_image(key, item, warnings) for item in _list(data.get("images"))),
        tests=tuple(_test(key, item, warnings) for item in _list(data.get("tests"))),
        checks=tuple(_check(key, item, warnings) for item in _list(data.get("http_checks"))),
    )


def _image(key: str, raw: object, warnings: list[str]) -> V1Image:
    data = _mapping(raw, f"image of {key}")
    warnings.extend(_unknown(data, IMAGE_KEYS, f"image {data.get('name')} of {key}"))
    args = _mapping(data.get("build_args") or {}, f"build_args of {key}")
    context = data.get("context")
    return V1Image(
        name=_text(data, "name", key),
        dockerfile=str(data.get("dockerfile") or "Dockerfile"),
        context=str(context) if context else None,
        target=_optional_text(data.get("target")),
        build_args={str(k): str(v) for k, v in args.items()},
        env_key=_optional_text(data.get("env_key")),
        env_mode=str(data.get("env_mode") or DEFAULT_ENV_MODE),
        deploy=bool(data.get("deploy", True)),
    )


def _test(key: str, raw: object, warnings: list[str]) -> V1Test:
    data = _mapping(raw, f"test of {key}")
    warnings.extend(_unknown(data, TEST_KEYS, f"test of {key}"))
    env = _mapping(data.get("environment") or {}, f"test environment of {key}")
    return V1Test(
        image=_text(data, "image", key),
        environment={str(k): str(v) for k, v in env.items()},
        command=tuple(str(item) for item in _list(data.get("command"))),
        tmpfs=tuple(str(item) for item in _list(data.get("tmpfs"))),
    )


def _check(key: str, raw: object, warnings: list[str]) -> V1Check:
    data = _mapping(raw, f"http check of {key}")
    warnings.extend(_unknown(data, CHECK_KEYS, f"http check of {key}"))
    statuses = tuple(int(item) for item in _list(data.get("statuses")) if isinstance(item, int))
    return V1Check(_text(data, "url", key), statuses or (200,))


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"{what} must be an object")
    return value


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _text(data: Mapping[str, object], name: str, key: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"project {key}: '{name}' must be a non-empty string")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _unknown(data: Mapping[str, object], known: frozenset[str], where: str) -> list[str]:
    extra = sorted(set(data) - known)
    return [f"{where}: unknown v1 field(s) not imported: {', '.join(extra)}"] if extra else []


def load_registry(path: Path) -> V1Registry:
    """Read the v1 SQLite registry and saved settings, read-only (the file is never modified)."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise ValidationError(f"cannot open v1 database {path}: {error}") from error
    try:
        db.row_factory = sqlite3.Row
        entries: dict[str, dict[str, object]] = {}
        _read_projects(db, entries)
        _read_settings(db, entries)
    except sqlite3.Error as error:
        raise ValidationError(f"cannot read v1 database {path}: {error}") from error
    finally:
        db.close()
    return {key: _entry(values) for key, values in entries.items()}


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    return db.execute(query, (name,)).fetchone() is not None


def _read_projects(db: sqlite3.Connection, entries: dict[str, dict[str, object]]) -> None:
    if not _table_exists(db, "registry_projects"):
        return
    for row in db.execute("SELECT id, legacy_binding FROM registry_projects"):
        entries.setdefault(row["id"], {})["legacy_binding"] = row["legacy_binding"]
    if not _table_exists(db, "registry_environments"):
        return
    query = "SELECT project, name, branch, automatic, state FROM registry_environments"
    for row in db.execute(query):
        environments = entries.setdefault(row["project"], {}).setdefault("environments", {})
        assert isinstance(environments, dict)  # noqa: S101 - narrowed for the type checker
        environments[row["name"]] = V1EnvironmentRow(
            row["name"], row["branch"], bool(row["automatic"]), row["state"]
        )


def _read_settings(db: sqlite3.Connection, entries: dict[str, dict[str, object]]) -> None:
    if not _table_exists(db, "settings"):
        return
    for row in db.execute("SELECT project, branch, automatic FROM settings"):
        values = entries.setdefault(row["project"], {})
        values["saved_branch"] = row["branch"]
        values["saved_automatic"] = bool(row["automatic"])


def _entry(values: Mapping[str, object]) -> V1RegistryEntry:
    binding = values.get("legacy_binding")
    environments = values.get("environments")
    branch = values.get("saved_branch")
    automatic = values.get("saved_automatic")
    return V1RegistryEntry(
        legacy_binding=binding if isinstance(binding, str) else None,
        environments=environments if isinstance(environments, dict) else {},
        saved_branch=branch if isinstance(branch, str) else None,
        saved_automatic=automatic if isinstance(automatic, bool) else None,
    )
