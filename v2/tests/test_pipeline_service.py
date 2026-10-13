import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shiftcodes.api.errors import register_error_handlers
from shiftcodes.domain.diagnostics import Diagnostic, Severity, Span
from shiftcodes.domain.errors import NotFoundError, ValidationError
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Secret
from shiftcodes.services.jenkinsfile_validator import JenkinsfileValidator
from shiftcodes.services.lookup import require_environment
from shiftcodes.services.pipelines import PipelineService, PipelineValidator
from tests.conftest import DEV, PROD
from tests.pipeline_support import FIXTURES, wrap

LARAVEL = (FIXTURES / "valid" / "laravel.groovy").read_text()
SECRET_PIPELINE = wrap("echo 'x'", "agent any", "environment { T = credentials('API_TOKEN') }")


def add_secret(uow: UnitOfWork, slug: str, name: str) -> None:
    with uow:
        environment = require_environment(uow, slug, DEV)
        uow.secrets.add(Secret(environment_id=environment.id, name=name, ciphertext=b"x"))


def test_default_validator_is_the_jenkinsfile_engine() -> None:
    validator: PipelineValidator = JenkinsfileValidator()
    validator.validate(wrap("sh 'x'"))


def test_valid_pipeline_is_saved(uow: UnitOfWork, project: str) -> None:
    add_secret(uow, project, "DB_PASSWORD")
    saved = PipelineService(uow).save(project, DEV, LARAVEL)
    assert saved.version == 1
    assert PipelineService(uow).get_active(project, DEV).jenkinsfile == LARAVEL


def test_invalid_pipeline_raises_with_structured_diagnostics(uow: UnitOfWork, project: str) -> None:
    text = wrap("sh 'x'\nfoo 'y'", agent="agent any")
    with pytest.raises(ValidationError) as info:
        PipelineService(uow).save(project, DEV, text)
    diagnostics = info.value.diagnostics
    assert [d.severity for d in diagnostics] == [Severity.ERROR, Severity.ERROR]
    assert [d.span.line for d in diagnostics] == [7, 8]
    assert "unknown step 'foo'" in diagnostics[1].message
    assert "2 error(s)" in str(info.value)
    assert PipelineService(uow).history(project, DEV) == []


def test_syntax_error_carries_position_and_excerpt(uow: UnitOfWork, project: str) -> None:
    with pytest.raises(ValidationError) as info:
        PipelineService(uow).save(project, DEV, "pipeline {\n  agent any\n  script { }\n}")
    (diagnostic,) = info.value.diagnostics
    assert diagnostic.span == Span(3, 3)
    assert "script { }" in str(info.value)
    assert str(info.value).count("^") == 1


def test_credentials_are_checked_against_the_environment_secrets(
    uow: UnitOfWork, project: str
) -> None:
    service = PipelineService(uow)
    with pytest.raises(ValidationError, match="unknown credential 'API_TOKEN'"):
        service.save(project, DEV, SECRET_PIPELINE)
    add_secret(uow, project, "API_TOKEN")
    service.save(project, DEV, SECRET_PIPELINE)
    with pytest.raises(ValidationError, match="unknown credential"):
        service.save(project, PROD, SECRET_PIPELINE)


def test_warnings_do_not_block_saving(uow: UnitOfWork, project: str) -> None:
    add_secret(uow, project, "API_TOKEN")
    text = wrap('echo "${T}"', "agent any", "environment { T = credentials('API_TOKEN') }")
    assert PipelineService(uow).save(project, DEV, text).version == 1


def test_unknown_environment_is_not_found(uow: UnitOfWork) -> None:
    with pytest.raises(NotFoundError):
        PipelineService(uow).save("missing", DEV, wrap("echo 'x'", "agent any"))


def test_error_envelope_lists_diagnostics() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/bad")
    def bad() -> None:
        raise ValidationError("bad pipeline", [Diagnostic(Severity.ERROR, "oops", Span(3, 4))])

    @app.get("/plain")
    def plain() -> None:
        raise ValidationError("plain")

    client = TestClient(app)
    body = client.get("/bad").json()["error"]
    assert body["code"] == "validation_error"
    assert body["diagnostics"] == [{"severity": "error", "message": "oops", "line": 3, "column": 4}]
    assert "diagnostics" not in client.get("/plain").json()["error"]
