"""Use cases behind the pipeline editors: parse, render, validate and the stored document.

The browser never parses Jenkinsfile text: it asks this service. Nothing here executes a
pipeline; the dry run only plans one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from backend.adapters.worker_client import RepositoryGateway
from backend.domain.actor import Actor
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, ValidationError
from backend.execution.dry_run import DryRun, plan_dry_run
from backend.pipeline import ValidationContext, model
from backend.pipeline.analysis import Analysis, Rendering, analyze, render, to_editing_model
from backend.pipeline.errors import Diagnostic, Severity
from backend.repositories.database import UnitOfWork
from backend.services.lookup import require_environment
from backend.services.pipelines import MAX_JENKINSFILE_BYTES, PipelineService, StoredPipeline


class DocumentSource(StrEnum):
    """Where the pipeline text in force comes from."""

    PLATFORM = "platform"
    REPOSITORY = "repository"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """The editing model of a text (``None`` after a syntax error) and the findings."""

    model: model.Model | None
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class PipelineDocument:
    """What an environment's editors show: the text in force and where it comes from."""

    source: DocumentSource
    version: int
    jenkinsfile: str
    updated_at: datetime | None
    updated_by: str | None


@dataclass(frozen=True, slots=True)
class DryRunOutcome:
    """The plan of a pipeline, or why there is none."""

    diagnostics: tuple[Diagnostic, ...]
    plan: DryRun | None


class PipelineEditorService:
    """Stateless operations on pipeline text and models, with optional environment context."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def context(
        self, project: str | None, environment: EnvironmentName | None
    ) -> ValidationContext:
        """Validation facts of an environment; without one, credential names are not judged."""
        if project is None and environment is None:
            return ValidationContext(check_credentials=False)
        if project is None or environment is None:
            raise ValidationError("project and environment must be given together")
        with self._uow:
            target = require_environment(self._uow, project, environment)
            secrets = self._uow.secrets.list_for_environment(target.id)
            return ValidationContext(frozenset(secret.name for secret in secrets))

    def parse(self, text: str, context: ValidationContext) -> ParseOutcome:
        """The model of ``text`` and its diagnostics; no model after a syntax error."""
        analysis = analyze(_checked(text), context)
        return ParseOutcome(to_editing_model(analysis), analysis.diagnostics)

    def render(self, value: model.Model, context: ValidationContext) -> Rendering:
        """The canonical text of ``value`` and the diagnostics of that text."""
        return render(value, context)

    def validate(self, text: str, context: ValidationContext) -> tuple[Diagnostic, ...]:
        """The diagnostics of ``text``."""
        return analyze(_checked(text), context).diagnostics


class PipelineDocumentService:
    """The pipeline of one environment: repository file, stored versions and dry runs."""

    def __init__(
        self,
        uow: UnitOfWork,
        pipelines: PipelineService,
        editor: PipelineEditorService,
        repository: RepositoryGateway,
    ) -> None:
        self._uow = uow
        self._pipelines = pipelines
        self._editor = editor
        self._repository = repository

    def document(self, slug: str, env: EnvironmentName, actor: Actor) -> PipelineDocument:
        """The pipeline in force: the repository's ``Jenkinsfile`` wins over a stored version.

        When git cannot read the repository the stored pipeline is shown instead; a run decides
        again at its own commit.
        """
        stored = self._pipelines.latest_stored(slug, env)
        version = 0 if stored is None else stored.version
        text = self._repository.repository_jenkinsfile(slug, env, actor).text
        if text is not None:
            return PipelineDocument(DocumentSource.REPOSITORY, version, text, None, None)
        if stored is None:
            return PipelineDocument(DocumentSource.NONE, 0, "", None, None)
        return _platform_document(stored)

    def save(
        self, slug: str, env: EnvironmentName, jenkinsfile: str, base_version: int, actor: Actor
    ) -> PipelineDocument:
        """Store a new version when ``base_version`` is still the latest one.

        Refused (``ConflictError``) while the repository has a ``Jenkinsfile``, because a run
        would ignore the stored text.
        """
        self._pipelines.current_version(slug, env)
        if self._repository.repository_jenkinsfile(slug, env, actor).text is not None:
            raise ConflictError(
                "the pipeline of this environment comes from the Jenkinsfile in the repository; "
                "change that file instead"
            )
        saved = self._pipelines.save(slug, env, jenkinsfile, actor=actor, base_version=base_version)
        return _platform_document(self._pipelines.stored_version(slug, env, saved.version))

    def history(self, slug: str, env: EnvironmentName) -> list[StoredPipeline]:
        """Stored versions, newest first."""
        return self._pipelines.stored_history(slug, env)

    def version(self, slug: str, env: EnvironmentName, version: int) -> StoredPipeline:
        """One stored version; ``NotFoundError`` when it does not exist."""
        return self._pipelines.stored_version(slug, env, version)

    def dry_run(
        self,
        slug: str,
        env: EnvironmentName,
        jenkinsfile: str | None,
        branch: str | None,
        actor: Actor,
    ) -> DryRunOutcome:
        """What the pipeline (the given text, else the one in force) would do on ``branch``."""
        text = (
            jenkinsfile if jenkinsfile is not None else self.document(slug, env, actor).jenkinsfile
        )
        if not text.strip():
            raise ValidationError("there is no pipeline to test yet")
        analysis: Analysis = analyze(_checked(text), self._editor.context(slug, env))
        pipeline = analysis.pipeline
        failed = any(item.severity is Severity.ERROR for item in analysis.diagnostics)
        if pipeline is None or failed:
            return DryRunOutcome(analysis.diagnostics, None)
        with self._uow:
            default_branch = require_environment(self._uow, slug, env).branch
        plan = plan_dry_run(pipeline, slug, env.value, branch or default_branch)
        return DryRunOutcome(analysis.diagnostics, plan)


def _platform_document(stored: StoredPipeline) -> PipelineDocument:
    return PipelineDocument(
        DocumentSource.PLATFORM,
        stored.version,
        stored.jenkinsfile,
        stored.created_at,
        stored.created_by,
    )


def _checked(text: str) -> str:
    if len(text.encode()) > MAX_JENKINSFILE_BYTES:
        raise ValidationError(f"pipeline text exceeds {MAX_JENKINSFILE_BYTES} bytes")
    return text
