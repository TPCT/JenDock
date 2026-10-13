"""Pure planning: turn v1 projects into the v2 projects, environments and pipelines to create.

Nothing here touches a database. ``build_plan`` returns everything a dry run prints and a real
run applies; every guess is recorded as a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import DomainError
from shiftcodes.importer.compose_images import ServiceImage, resolve_service_images
from shiftcodes.importer.recipe_pipeline import build_pipeline
from shiftcodes.importer.v1_model import V1Project, V1Recipe, V1Registry, V1RegistryEntry
from shiftcodes.services.jenkinsfile_validator import JenkinsfileValidator

DEV = EnvironmentName.DEVELOPMENT
PROD = EnvironmentName.PRODUCTION
DEFAULT_BRANCH = {DEV: "development", PROD: "production"}
PRODUCTION_BRANCHES = frozenset({"main", "master", "production", "prod", "release"})
DEVELOPMENT_BRANCHES = frozenset({"development", "develop", "dev", "developement", "staging"})
LIKELY_TYPOS = {"developement": "development"}


@dataclass(frozen=True, slots=True)
class EnvField:
    """One v1 recipe field and where it goes in v2.

    ``v2_field`` is the ``EnvironmentUpdate``/``Environment`` attribute; ``None`` means v2 has no
    place for it yet, so it is only reported. Adding a field is adding one line here.
    """

    v1_key: str
    v2_field: str | None
    note: str = ""


ENVIRONMENT_FIELDS: tuple[EnvField, ...] = (
    EnvField("compose_project", "compose_project"),
    EnvField("compose_files", "compose_files"),
    EnvField("services", "services"),
    EnvField("env_file", None, "compose env file; v2 gets a field with Task 7"),
)


@dataclass(frozen=True, slots=True)
class EnvironmentPlan:
    """Desired state of one environment."""

    name: EnvironmentName
    branch: str
    auto_deploy: bool
    values: dict[str, object] = field(default_factory=dict)  # v2 field -> value
    unmapped: dict[str, object] = field(default_factory=dict)  # v1 key -> value (report only)
    jenkinsfile: str | None = None
    source: str = ""  # where the branch came from


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    """Desired state of one project."""

    slug: str
    name: str
    repository: str
    environments: tuple[EnvironmentPlan, ...]
    bound: EnvironmentName
    service_images: tuple[ServiceImage, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()

    def environment(self, name: EnvironmentName) -> EnvironmentPlan:
        """The plan of environment ``name``."""
        return next(item for item in self.environments if item.name is name)


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """All projects to import."""

    projects: tuple[ProjectPlan, ...]

    @property
    def has_errors(self) -> bool:
        """Whether any project could not be planned cleanly (a pipeline failed validation, ...)."""
        return any(project.errors for project in self.projects)


def build_plan(
    projects: list[V1Project], registry: V1Registry | None = None, compose_root: Path | None = None
) -> ImportPlan:
    """Plan the import of ``projects``; ``registry`` is the optional v1 database content."""
    entries = registry or {}
    return ImportPlan(
        tuple(_plan_project(item, entries.get(item.key), compose_root) for item in projects)
    )


def _plan_project(
    project: V1Project, entry: V1RegistryEntry | None, compose_root: Path | None
) -> ProjectPlan:
    warnings = list(project.warnings)
    errors: list[str] = []
    bound, branch, source = _binding(project, entry, warnings)
    auto_deploy = _automatic(project, entry)
    environments = {
        bound: EnvironmentPlan(bound, branch, auto_deploy, source=source),
        _other(bound): _other_environment(bound, entry, warnings),
    }
    images: tuple[ServiceImage, ...] = ()
    secrets: tuple[str, ...] = ()
    if project.recipe is None:
        warnings.append(
            "no v1 recipe: environments are created without compose settings or pipeline"
        )
    else:
        images = tuple(resolve_service_images(project.recipe, compose_root))
        environments[bound] = _with_recipe(environments[bound], project.recipe)
        text, problems = _pipeline(project.recipe, images, warnings)
        errors.extend(problems)
        environments[bound] = _replace_pipeline(environments[bound], text)
        warnings.extend(_image_warnings(images))
        secrets = _secret_notes(project.recipe)
    ordered = (environments[DEV], environments[PROD])
    return ProjectPlan(
        slug=project.key,
        name=project.label,
        repository=project.repository,
        environments=ordered,
        bound=bound,
        service_images=images,
        warnings=tuple(warnings),
        errors=tuple(errors),
        secrets=secrets,
    )


def _other(name: EnvironmentName) -> EnvironmentName:
    return PROD if name is DEV else DEV


def _binding(
    project: V1Project, entry: V1RegistryEntry | None, warnings: list[str]
) -> tuple[EnvironmentName, str, str]:
    """Which environment the single v1 recipe/branch belongs to, its branch and the evidence."""
    branch = (entry.saved_branch if entry and entry.saved_branch else None) or project.branch
    source = "v1 saved setting" if entry and entry.saved_branch else "projects.json"
    if branch in LIKELY_TYPOS:
        warnings.append(
            f"branch '{branch}' looks misspelled (did you mean '{LIKELY_TYPOS[branch]}'?); kept "
            "as written in v1 because it may be the real branch name"
        )
    if entry and entry.legacy_binding in {DEV.value, PROD.value}:
        return EnvironmentName(entry.legacy_binding), branch, f"{source}; v1 registry binding"
    if branch in PRODUCTION_BRANCHES:
        warnings.append(f"v1 has one branch ('{branch}'): treated as the production environment")
        return PROD, branch, source
    if branch in DEVELOPMENT_BRANCHES:
        warnings.append(f"v1 has one branch ('{branch}'): treated as the development environment")
        return DEV, branch, source
    warnings.append(
        f"AMBIGUOUS: branch '{branch}' does not look like a development or production branch; "
        "assigned to development, check this"
    )
    return DEV, branch, source


def _automatic(project: V1Project, entry: V1RegistryEntry | None) -> bool:
    if entry and entry.saved_automatic is not None:
        return entry.saved_automatic
    return project.enabled


def _other_environment(
    bound: EnvironmentName, entry: V1RegistryEntry | None, warnings: list[str]
) -> EnvironmentPlan:
    other = _other(bound)
    row = entry.environments.get(other.value) if entry else None
    if row is not None and row.state != "not-provisioned":
        return EnvironmentPlan(other, row.branch, row.automatic, source="v1 registry")
    warnings.append(
        f"no v1 data for the {other.value} environment: created with the default branch "
        f"'{DEFAULT_BRANCH[other]}', auto deploy off, no pipeline"
    )
    return EnvironmentPlan(other, DEFAULT_BRANCH[other], False, source="default")


def _with_recipe(plan: EnvironmentPlan, recipe: V1Recipe) -> EnvironmentPlan:
    values: dict[str, object] = {}
    unmapped: dict[str, object] = {}
    raw = {
        "compose_project": recipe.compose_project,
        "compose_files": list(recipe.compose_files),
        "services": list(recipe.services),
        "env_file": recipe.env_file,
    }
    for item in ENVIRONMENT_FIELDS:
        value = raw.get(item.v1_key)
        if value in (None, []):
            continue
        (values if item.v2_field else unmapped)[item.v2_field or item.v1_key] = value
    return EnvironmentPlan(
        plan.name, plan.branch, plan.auto_deploy, values, unmapped, plan.jenkinsfile, plan.source
    )


def _replace_pipeline(plan: EnvironmentPlan, text: str | None) -> EnvironmentPlan:
    return EnvironmentPlan(
        plan.name, plan.branch, plan.auto_deploy, plan.values, plan.unmapped, text, plan.source
    )


def _pipeline(
    recipe: V1Recipe, images: tuple[ServiceImage, ...], warnings: list[str]
) -> tuple[str | None, list[str]]:
    generated = build_pipeline(recipe, list(images))
    warnings.extend(generated.warnings)
    try:
        JenkinsfileValidator().validate(generated.jenkinsfile)
    except DomainError as error:
        return None, [f"generated Jenkinsfile failed validation: {error}"]
    return generated.jenkinsfile, []


def _image_warnings(images: tuple[ServiceImage, ...]) -> list[str]:
    warnings = [
        f"AMBIGUOUS: service '{item.service}' -> image '{item.image}' is a guess ({item.note})"
        for item in images
        if item.source == "guess"
    ]
    if any(item.source != "compose" for item in images):
        warnings.append(
            "compose files were not readable: service -> image mapping comes from names; check "
            "it against the compose file (use --compose-root with a copy for an exact mapping)"
        )
    return warnings


def _secret_notes(recipe: V1Recipe) -> tuple[str, ...]:
    """What the operator must provide; v1 secrets and env files are never read or copied."""
    notes = ["no secret is referenced by the generated Jenkinsfile"]
    if recipe.env_file:
        notes.append(
            f"runtime configuration and secrets stay in the compose env file {recipe.env_file} "
            "(not imported); recreate its secret values as environment secrets if v2 should "
            "inject them"
        )
    return tuple(notes)
