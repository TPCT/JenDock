"""Which built image does each compose service run? (read-only)

v1 recipes list services and images separately; the link is the compose file (``image:
${AIC_WEBSITE_BACKEND_IMAGE}`` or ``image: etsawaa-app:${ETSAWAA_VERSION}``). When the compose
files are readable the answer is exact (``source="compose"``); otherwise name heuristics give a
best guess (``source="guess"``) that is always reported as ambiguous so a human reviews it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from backend.importer.v1_model import V1Image, V1Recipe

V1_COMPOSE_PREFIX = "/opt/docker-engine/projects"
_VARIABLE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)(?::?[-?+][^}]*)?\}?$")
# Roles that normally run the same image as the main application container.
WORKER_ROLES = frozenset(
    {
        "queue",
        "scheduler",
        "horizon",
        "reverb",
        "backups",
        "whatsapp",
        "notifications",
        "worker",
        "cron",
    }
)
APP_ROLES = ("app", "backend", "api")
ROLE_ALIASES = {"api": "app", "backend": "app"}


@dataclass(frozen=True, slots=True)
class ServiceImage:
    """The image chosen for a service and how sure we are."""

    service: str
    image: str | None
    source: str  # "compose" (exact) | "name" (names match) | "guess" (review) | "none"
    note: str = ""


def resolve_service_images(
    recipe: V1Recipe, compose_root: Path | None = None
) -> list[ServiceImage]:
    """Map every service of ``recipe`` to one of its deployable images."""
    deployable = [image for image in recipe.images if image.deploy]
    from_compose = _from_compose(recipe, deployable, compose_root)
    result = []
    for service in recipe.services:
        exact = from_compose.get(service)
        if exact is not None:
            result.append(ServiceImage(service, exact, "compose"))
        else:
            result.append(_guess(service, deployable))
    return result


def _from_compose(
    recipe: V1Recipe, deployable: Sequence[V1Image], compose_root: Path | None
) -> dict[str, str]:
    declared: dict[str, str] = {}
    for path in recipe.compose_files:
        found = _locate(path, compose_root)
        if found is None:
            continue
        declared.update(_service_images(found))
    resolved: dict[str, str] = {}
    for service, text in declared.items():
        image = _match(text, deployable)
        if image is not None:
            resolved[service] = image
    return resolved


def _locate(path: str, compose_root: Path | None) -> Path | None:
    candidates = [Path(path)]
    if compose_root is not None and path.startswith(V1_COMPOSE_PREFIX + "/"):
        candidates.insert(0, compose_root / path[len(V1_COMPOSE_PREFIX) + 1 :])
    return next((item for item in candidates if item.is_file()), None)


def _service_images(path: Path) -> dict[str, str]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return {}
    return {
        str(name): body["image"]
        for name, body in services.items()
        if isinstance(body, dict) and isinstance(body.get("image"), str)
    }


def _match(text: str, deployable: Sequence[V1Image]) -> str | None:
    variable = _VARIABLE.match(text.strip())
    if variable:
        key = variable.group(1)
        hits = [i.name for i in deployable if i.env_key == key and i.env_mode == "image"]
        return hits[0] if len(hits) == 1 else None
    name = text.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    return next((image.name for image in deployable if image.name == name), None)


def _role(name: str) -> str:
    role = name.rsplit("-", 1)[-1]
    return ROLE_ALIASES.get(role, role)


def _guess(service: str, deployable: Sequence[V1Image]) -> ServiceImage:
    if not deployable:
        return ServiceImage(service, None, "none", "the recipe has no deployable image")
    exact = [i.name for i in deployable if i.name == service]
    if len(exact) == 1:
        return ServiceImage(service, exact[0], "name", "image has the service's name")
    role = _role(service)
    same_role = [i.name for i in deployable if _role(i.name) == role]
    if len(same_role) == 1:
        return ServiceImage(service, same_role[0], "name", f"same role '{role}'")
    if len(deployable) == 1:
        return ServiceImage(service, deployable[0].name, "guess", "the only deployable image")
    if role in WORKER_ROLES:
        app = [i.name for i in deployable if _role(i.name) == "app"]
        if len(app) == 1:
            return ServiceImage(service, app[0], "guess", f"'{role}' runs the application image")
    return ServiceImage(service, None, "none", "no image could be determined")
