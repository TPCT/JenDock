"""Text rendering of an import plan and of the outcome of applying it."""

from __future__ import annotations

from shiftcodes.importer.apply import ProjectOutcome
from shiftcodes.importer.plan import EnvironmentPlan, ImportPlan, ProjectPlan


def render_plan(plan: ImportPlan, *, full: bool = True) -> str:
    """The plan as text; ``full`` includes the generated Jenkinsfiles."""
    lines: list[str] = []
    for project in plan.projects:
        lines.extend(_project(project, full=full))
    lines.append(_summary(plan))
    return "\n".join(lines) + "\n"


def _project(project: ProjectPlan, *, full: bool) -> list[str]:
    lines = [
        "=" * 78,
        f"project {project.slug}  ({project.name})  repository {project.repository}",
        f"  recipe bound to: {project.bound.value}",
    ]
    for env in project.environments:
        lines.extend(_environment(env, full=full))
    if project.service_images:
        lines.append("  service -> image:")
        lines.extend(
            f"    {item.service} -> {item.image or '(none)'}  [{item.source}]"
            for item in project.service_images
        )
    lines.extend(f"  secrets: {note}" for note in project.secrets)
    lines.extend(f"  WARNING: {text}" for text in project.warnings)
    lines.extend(f"  ERROR: {text}" for text in project.errors)
    return lines


def _environment(env: EnvironmentPlan, *, full: bool) -> list[str]:
    auto = "on" if env.auto_deploy else "off"
    lines = [
        f"  environment {env.name.value}: branch {env.branch!r} (from {env.source}), "
        f"auto deploy {auto}"
    ]
    for name, value in env.values.items():
        lines.append(f"    {name} = {value}")
    for name, value in env.unmapped.items():
        lines.append(f"    {name} = {value}   (recorded only: v2 has no field yet)")
    if env.jenkinsfile and full:
        lines.append("    Jenkinsfile:")
        lines.extend(f"      | {line}" for line in env.jenkinsfile.rstrip("\n").split("\n"))
    elif env.jenkinsfile:
        lines.append("    Jenkinsfile generated and valid")
    return lines


def _summary(plan: ImportPlan) -> str:
    warnings = sum(len(p.warnings) for p in plan.projects)
    errors = sum(len(p.errors) for p in plan.projects)
    pipelines = sum(1 for p in plan.projects for e in p.environments if e.jenkinsfile)
    return (
        f"{len(plan.projects)} project(s), {pipelines} Jenkinsfile(s) generated and valid, "
        f"{warnings} warning(s), {errors} error(s)"
    )


def render_outcomes(outcomes: list[ProjectOutcome]) -> str:
    """The result of a real run."""
    lines: list[str] = []
    for outcome in outcomes:
        lines.append(f"{outcome.slug}: {'created' if outcome.created else 'existing'}")
        lines.extend(f"  + {text}" for text in outcome.changes)
        lines.extend(f"  = {text}" for text in outcome.unchanged)
        lines.extend(f"  ! {text}" for text in outcome.differences)
        lines.extend(f"  ERROR {text}" for text in outcome.errors)
    created = sum(1 for o in outcomes if o.created)
    differing = sum(1 for o in outcomes if o.differences)
    failed = sum(1 for o in outcomes if o.errors)
    lines.append(f"{created} created, {differing} with differences, {failed} with errors")
    return "\n".join(lines) + "\n"
