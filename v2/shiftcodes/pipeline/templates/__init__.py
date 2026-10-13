"""Starter pipelines for common project types (Jenkinsfile text shipped with the package).

Every template validates cleanly with the engine and, like all pipelines, is also compared with
a real Jenkins linter (``make lint-jenkins``). They use placeholder service names and addresses
that the comments at the top tell the user to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True, slots=True)
class Template:
    """One starter pipeline."""

    name: str
    title: str
    description: str
    jenkinsfile: str


_TEXTS: tuple[tuple[str, str, str], ...] = (
    (
        "laravel",
        "Laravel",
        "Builds your Docker image, runs php artisan test inside it, then deploys it.",
    ),
    (
        "nextjs",
        "Next.js",
        "Installs, lints, tests and builds with Node, then builds the image and deploys it.",
    ),
    (
        "node-api",
        "Node.js API",
        "Installs and tests with Node, then builds the image and deploys the API.",
    ),
    (
        "python-fastapi",
        "Python / FastAPI",
        "Installs the requirements and runs pytest, then builds the image and deploys it.",
    ),
    (
        "static-site",
        "Static site",
        "Builds the site with Node, packages it in a Docker image and deploys it.",
    ),
    (
        "dockerfile",
        "Any project with a Dockerfile",
        "Builds the image from your Dockerfile and deploys it. The simplest starting point.",
    ),
)


def _read(name: str) -> str:
    return (resources.files(__name__) / f"{name}.Jenkinsfile").read_text(encoding="utf-8")


def load_templates() -> tuple[Template, ...]:
    """All templates in display order."""
    return tuple(Template(name, title, text, _read(name)) for name, title, text in _TEXTS)
