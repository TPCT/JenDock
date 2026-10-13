"""Import of v1 projects: planning, Jenkinsfile conversion, idempotent apply and the CLI.

``tests/fixtures/v1/projects.json`` is a verbatim copy of the real v1 file (read-only source).
It was checked for secrets: it holds paths, branches, image names and obviously dummy test
values (``APP_KEY=base64:AAAA...``, ``JWT_SECRET=isolated-build-check-no-production-secret``).
The compose fixtures are synthetic, because the real ones live on the VPS only.
"""

import argparse
import hashlib
import io
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from backend import cli
from backend.config import get_settings
from backend.domain.actor import Actor
from backend.domain.enums import EnvironmentName, GitTransport
from backend.domain.errors import ValidationError
from backend.importer.apply import ImportServices, apply_plan
from backend.importer.cli import add_arguments, run_import
from backend.importer.compose_images import resolve_service_images
from backend.importer.plan import ENVIRONMENT_FIELDS, ImportPlan, build_plan
from backend.importer.recipe_pipeline import build_pipeline
from backend.importer.v1_model import V1Check, load_projects, load_registry
from backend.pipeline import parse, serialize
from backend.repositories.database import UnitOfWork
from backend.services.audit import AuditService
from backend.services.environments import EnvironmentService
from backend.services.jenkinsfile_validator import JenkinsfileValidator
from backend.services.pipelines import PipelineService
from backend.services.projects import ProjectService
from tests.conftest import DEV, PROD
from tests.ingress_support import FIXTURES

V1 = FIXTURES / "v1"
PROJECTS = V1 / "projects.json"
GOLDEN = {"aic-website": "v1_aic_website", "etsawaa": "v1_etsawaa", "captcha": "v1_captcha"}
ACTOR = Actor.system("test")


@pytest.fixture(scope="module")
def plan() -> ImportPlan:
    return build_plan(load_projects(PROJECTS))


def by_slug(plan: ImportPlan, slug: str):  # type: ignore[no-untyped-def]
    return next(p for p in plan.projects if p.slug == slug)


# --- reading v1 -------------------------------------------------------------------------------


def test_the_real_v1_file_parses_completely() -> None:
    projects = load_projects(PROJECTS)
    assert len(projects) == 20
    sys_ = next(p for p in projects if p.key == "aic-sys")
    assert sys_.repository == "TPCT/sys.aicconsultation.com"
    assert sys_.recipe is not None and sys_.recipe.compose_project == "aic"
    assert [i.name for i in sys_.recipe.images] == ["aic-sys-app", "aic-sys-web"]
    assert all(not p.warnings for p in projects)  # no v1 field is unknown to the importer


def test_unknown_v1_fields_are_reported_not_silently_dropped(tmp_path: Path) -> None:
    document = {
        "x": {
            "repository": "o/r",
            "branch": "main",
            "label": "X",
            "secrets": {"TOKEN": "hunter2"},
            "recipe": {"services": ["a"], "images": [], "tests": [], "novel": 1},
        }
    }
    path = tmp_path / "p.json"
    path.write_text(json.dumps(document))
    [project] = load_projects(path)
    assert any("secrets" in w for w in project.warnings)
    assert any("novel" in w for w in project.warnings)


@pytest.mark.parametrize("content", ["not json", "[]", '{"a": 1}', '{"a": {"branch": "main"}}'])
def test_malformed_files_are_a_validation_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "p.json"
    path.write_text(content)
    with pytest.raises(ValidationError):
        load_projects(path)
    with pytest.raises(ValidationError):
        load_projects(tmp_path / "missing.json")


def v1_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE registry_projects(id TEXT PRIMARY KEY, revision INTEGER, repository TEXT,
          repository_id INTEGER, label TEXT, legacy_binding TEXT, legacy_document TEXT,
          created REAL, updated REAL);
        CREATE TABLE registry_environments(project TEXT, name TEXT, revision INTEGER, branch TEXT,
          automatic INTEGER, wake_on_push INTEGER, provision_on_push INTEGER, state TEXT,
          recipe_revision INTEGER, PRIMARY KEY(project, name));
        CREATE TABLE settings(project TEXT PRIMARY KEY, branch TEXT NOT NULL, automatic INTEGER);
        INSERT INTO registry_projects
          VALUES('captcha',1,'TPCT/captcha',1,'c','production','{}',0,0);
        INSERT INTO registry_environments VALUES('captcha','development',1,'dev-branch',0,0,0,
          'provisioned',NULL);
        INSERT INTO registry_environments VALUES('captcha','production',1,'production',1,0,0,
          'legacy-bound',1);
        INSERT INTO settings VALUES('captcha','release-2026',0);
        """
    )
    db.commit()
    db.close()


def test_the_v1_database_refines_binding_branch_and_automatic(tmp_path: Path) -> None:
    db_path = tmp_path / "v1.sqlite"
    v1_database(db_path)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    registry = load_registry(db_path)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before  # opened read-only
    plan = build_plan(load_projects(PROJECTS), registry)
    captcha = by_slug(plan, "captcha")
    assert captcha.bound is PROD  # from legacy_binding, not guessed from 'main'
    assert captcha.environment(PROD).branch == "release-2026"  # saved setting wins
    assert captcha.environment(PROD).auto_deploy is False
    assert captcha.environment(DEV).branch == "dev-branch"  # real v1 row, not a default
    assert captcha.environment(DEV).source == "v1 registry"
    assert not any("treated as" in w for w in captcha.warnings)


def test_a_missing_or_foreign_database_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_registry(tmp_path / "nope.sqlite")
    bogus = tmp_path / "bogus.sqlite"
    bogus.write_text("this is not sqlite")
    with pytest.raises(ValidationError):
        load_registry(bogus)


# --- planning -----------------------------------------------------------------------------------


def test_every_project_is_planned_with_a_valid_jenkinsfile(plan: ImportPlan) -> None:
    assert not plan.has_errors
    validator = JenkinsfileValidator()
    for project in plan.projects:
        env = project.environment(project.bound)
        assert env.jenkinsfile, project.slug
        validator.validate(env.jenkinsfile)
        pipeline = parse(env.jenkinsfile)
        assert serialize(pipeline) == env.jenkinsfile  # canonical: the serializer's own output
        assert parse(serialize(pipeline)) == pipeline
        assert (
            project.environment(
                EnvironmentName.PRODUCTION if project.bound is DEV else DEV
            ).jenkinsfile
            is None
        )


@pytest.mark.parametrize(("slug", "golden"), sorted(GOLDEN.items()))
def test_generated_jenkinsfiles_match_the_golden_files(
    plan: ImportPlan, slug: str, golden: str
) -> None:
    project = by_slug(plan, slug)
    expected = (FIXTURES / "jenkinsfiles" / "valid" / f"{golden}.groovy").read_text()
    assert project.environment(project.bound).jenkinsfile == expected


def test_no_jenkinsfile_references_a_credential_or_a_real_secret(plan: ImportPlan) -> None:
    for project in plan.projects:
        text = project.environment(project.bound).jenkinsfile or ""
        assert "credentials(" not in text
        assert "hunter2" not in text


def test_project_and_environment_mapping_for_a_known_project(plan: ImportPlan) -> None:
    project = by_slug(plan, "aic-sys")
    assert (project.name, project.repository) == (
        "sys.aicconsultation.com",
        "TPCT/sys.aicconsultation.com",
    )
    assert project.bound is PROD
    prod, dev = project.environment(PROD), project.environment(DEV)
    assert (prod.branch, prod.auto_deploy) == ("main", True)
    assert (dev.branch, dev.auto_deploy, dev.values) == ("development", False, {})
    assert prod.values["compose_project"] == "aic"
    assert prod.values["services"] == [
        "sys-app",
        "sys-web",
        "sys-queue",
        "sys-scheduler",
        "sys-reverb",
    ]
    assert prod.values["compose_env_files"] == ["/opt/docker-engine/projects/aic/ports.env"]
    assert prod.unmapped == {}
    assert any("treated as the production" in w for w in project.warnings)
    assert any("no v1 data for the development" in w for w in project.warnings)


@pytest.mark.parametrize(
    ("branch", "bound", "ambiguous"),
    [
        ("main", PROD, False),
        ("master", PROD, False),
        ("production", PROD, False),
        ("development", DEV, False),
        ("developement", DEV, False),
        ("feature/x", DEV, True),
    ],
)
def test_the_single_v1_branch_is_assigned_to_an_environment(
    tmp_path: Path, branch: str, bound: EnvironmentName, ambiguous: bool
) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"p": {"repository": "o/r", "branch": branch, "label": "P"}}))
    [project] = build_plan(load_projects(path)).projects
    assert project.bound is bound
    assert any("AMBIGUOUS" in w for w in project.warnings) is ambiguous
    assert any("misspelled" in w for w in project.warnings) is (branch == "developement")
    assert any("no v1 recipe" in w for w in project.warnings)
    assert all(env.jenkinsfile is None for env in project.environments)


def test_the_environment_mapping_is_table_driven() -> None:
    fields = {item.v1_key: item.v2_field for item in ENVIRONMENT_FIELDS}
    assert fields == {
        "compose_project": "compose_project",
        "compose_files": "compose_files",
        "services": "services",
        "env_file": "compose_env_files",
    }


# --- conversion details -------------------------------------------------------------------------


def converted(plan: ImportPlan, slug: str) -> str:
    project = by_slug(plan, slug)
    return project.environment(project.bound).jenkinsfile or ""


def test_images_become_docker_builds_with_all_their_options(plan: ImportPlan) -> None:
    text = converted(plan, "aic-website")
    backend = "dockerBuild(image: 'aic-website-backend', dockerfile: 'backend/Dockerfile', "
    assert backend + "context: 'backend')" in text
    assert "target: 'runner'" in text
    assert "args: [NEXT_PUBLIC_SITE_URL: 'https://aicconsultation.com'" in text
    # a test-only image (deploy: false) is built too, but never deployed
    etsawaa = converted(plan, "etsawaa")
    assert (
        "dockerBuild(image: 'deploy-test-etsawaa', dockerfile: 'Dockerfile', target: 'test')"
        in etsawaa
    )
    assert "'deploy-test-etsawaa'" not in etsawaa.split("stage('Deploy')")[1]


def test_tests_run_in_their_image_with_the_environment_as_stage_environment(
    plan: ImportPlan,
) -> None:
    text = converted(plan, "aic-website")
    assert (
        "stage('Test aic-website-backend')" in text and "stage('Test aic-website-reverb')" in text
    )
    assert "image 'aic-website-backend'" in text
    assert "DATABASE_URL = 'sqlite:///:memory:'" in text
    assert "environment {" in text
    one = converted(plan, "captcha")
    assert "stage('Test')" in one and "sh 'python tests/check_container.py'" in one  # shell-joined
    assert "environment {" not in one  # no test environment in v1


def test_a_sh_dash_c_command_becomes_the_script_itself(plan: ImportPlan) -> None:
    text = converted(plan, "etsawaa")
    assert "sh 'mkdir -p storage/framework/cache/data" in text
    assert "&& php vendor/bin/phpunit'" in text
    assert "sh -c" not in text


def test_warnings_for_what_cannot_be_carried_over(plan: ImportPlan) -> None:
    project = by_slug(plan, "aic-website")
    text = "\n".join(project.warnings)
    assert "'JWT_SECRET' looks like a credential" in text
    assert "tmpfs mounts" in text and "not supported by v2" in text


def test_a_service_without_an_image_is_called_out(tmp_path: Path) -> None:
    document = {
        "p": {
            "repository": "o/r",
            "branch": "main",
            "label": "P",
            "recipe": {
                "services": ["db", "app"],
                "images": [
                    {"name": "p-app", "dockerfile": "Dockerfile", "env_key": "K"},
                    {"name": "p-web", "dockerfile": "Dockerfile", "env_key": "W"},
                ],
                "tests": [],
            },
        }
    }
    path = tmp_path / "p.json"
    path.write_text(json.dumps(document))
    [project] = build_plan(load_projects(path)).projects
    assert any(
        "'db' has no image mapping" in w and "NOT in the deploy step" in w for w in project.warnings
    )
    text = project.environment(project.bound).jenkinsfile or ""
    assert "deploy(services: [app: 'p-app'])" in text and "db" not in text.split("deploy")[1]


def test_multiple_health_statuses_keep_the_first_and_warn() -> None:
    [project] = load_projects(PROJECTS)[:1]
    assert project.recipe is not None
    recipe = project.recipe
    two = replace(recipe, checks=(V1Check("https://x.test/", (301, 200)),))
    generated = build_pipeline(two, resolve_service_images(two))
    assert "expectStatus: 301" in generated.jenkinsfile
    assert any("accepts statuses [301, 200]" in w for w in generated.warnings)


# --- service -> image mapping --------------------------------------------------------------------


def recipe_of(slug: str):  # type: ignore[no-untyped-def]
    recipe = next(p for p in load_projects(PROJECTS) if p.key == slug).recipe
    assert recipe is not None
    return recipe


def test_compose_files_give_the_exact_mapping_when_readable() -> None:
    mapping = {
        s.service: (s.image, s.source)
        for s in resolve_service_images(recipe_of("aic-sys"), V1 / "compose")
    }
    assert mapping == {
        "sys-app": ("aic-sys-app", "compose"),
        "sys-web": ("aic-sys-web", "compose"),
        "sys-queue": ("aic-sys-app", "compose"),
        "sys-scheduler": ("aic-sys-app", "compose"),
        "sys-reverb": ("aic-sys-web", "compose"),  # unlike the name-based guess
    }
    etsawaa = {
        s.service: s.image for s in resolve_service_images(recipe_of("etsawaa"), V1 / "compose")
    }
    assert etsawaa["reverb"] == "etsawaa-app" and etsawaa["frontend"] == "etsawaa-frontend"
    assert all(
        s.source == "compose" for s in resolve_service_images(recipe_of("etsawaa"), V1 / "compose")
    )


def test_without_compose_files_names_decide_and_guesses_are_flagged() -> None:
    mapping = {s.service: s for s in resolve_service_images(recipe_of("aic-sys"))}
    assert mapping["sys-app"].source == "name" and mapping["sys-web"].source == "name"
    assert mapping["sys-queue"].image == "aic-sys-app" and mapping["sys-queue"].source == "guess"
    assert (
        mapping["sys-reverb"].image == "aic-sys-app"
    )  # differs from compose: that is why it is flagged


def test_the_compose_mapping_flows_into_the_deploy_step() -> None:
    plan = build_plan(load_projects(PROJECTS), compose_root=V1 / "compose")
    text = by_slug(plan, "aic-sys").environment(PROD).jenkinsfile or ""
    assert "'sys-reverb': 'aic-sys-web'" in text
    assert not any("AMBIGUOUS" in w for w in by_slug(plan, "aic-sys").warnings)
    assert not any("not readable" in w for w in by_slug(plan, "aic-sys").warnings)


# --- applying -----------------------------------------------------------------------------------


@pytest.fixture
def services(uow: UnitOfWork) -> ImportServices:
    return ImportServices(ProjectService(uow), EnvironmentService(uow), PipelineService(uow))


def small_plan(slugs: set[str]) -> ImportPlan:
    full = build_plan(load_projects(PROJECTS), compose_root=V1 / "compose")
    return ImportPlan(tuple(p for p in full.projects if p.slug in slugs))


def test_apply_creates_projects_environments_and_pipelines(
    services: ImportServices, uow: UnitOfWork
) -> None:
    outcomes = apply_plan(small_plan({"aic-sys", "captcha"}), services, ACTOR)
    assert [o.created for o in outcomes] == [True, True]
    assert not any(o.errors or o.differences for o in outcomes)
    detail = ProjectService(uow).get("aic-sys")
    assert (detail.project.name, detail.project.repository) == (
        "sys.aicconsultation.com",
        "TPCT/sys.aicconsultation.com",
    )
    prod = EnvironmentService(uow).get("aic-sys", PROD)
    assert (prod.branch, prod.auto_deploy, prod.compose_project) == ("main", True, "aic")
    assert prod.services == ["sys-app", "sys-web", "sys-queue", "sys-scheduler", "sys-reverb"]
    assert len(prod.compose_files) == 4
    dev = EnvironmentService(uow).get("aic-sys", DEV)
    assert (dev.branch, dev.auto_deploy, dev.compose_project, dev.services) == (
        "development",
        False,
        None,
        [],
    )
    stored = PipelineService(uow).latest_stored("aic-sys", PROD)
    assert stored is not None and stored.version == 1
    assert "deploy(services:" in stored.jenkinsfile
    assert PipelineService(uow).latest_stored("aic-sys", DEV) is None
    actions = {e.action for e in AuditService(uow).list(limit=200)}
    assert {"project.create", "environment.update"} <= actions


def test_a_second_run_changes_nothing_and_a_third_with_edits_only_reports(
    services: ImportServices, uow: UnitOfWork
) -> None:
    plan = small_plan({"etsawaa"})
    apply_plan(plan, services, ACTOR)
    events_before = len(AuditService(uow).list(limit=200))
    [again] = apply_plan(plan, services, ACTOR)
    assert not again.created and not again.changes and not again.differences and not again.errors
    assert any("matches" in text for text in again.unchanged)
    assert len(AuditService(uow).list(limit=200)) == events_before  # nothing was written at all
    assert PipelineService(uow).current_version("etsawaa", PROD) == 1

    # Someone edits the platform afterwards: the importer reports but never overwrites.
    PipelineService(uow).save(
        "etsawaa",
        PROD,
        (plan.projects[0].environment(PROD).jenkinsfile or "").replace(
            "stage('Build')", "stage('Compile')"
        ),
    )
    EnvironmentService(uow).update(
        "etsawaa",
        PROD,
        __import__(
            "backend.services.environments", fromlist=["EnvironmentUpdate"]
        ).EnvironmentUpdate(branch="release"),
    )
    [third] = apply_plan(plan, services, ACTOR)
    assert not third.changes
    assert any(
        "differs from the one generated" in d and "not overwritten" in d for d in third.differences
    )
    assert any("branch is 'release'" in d for d in third.differences)
    assert PipelineService(uow).current_version("etsawaa", PROD) == 2


def test_blank_compose_settings_of_an_existing_environment_are_filled(
    services: ImportServices, uow: UnitOfWork
) -> None:
    ProjectService(uow).create(
        "captcha", "Captcha", "TPCT/captcha.example.com", "development", "main"
    )
    [outcome] = apply_plan(small_plan({"captcha"}), services, ACTOR)
    assert not outcome.created
    assert any(
        "set compose_env_files, compose_files, compose_project, services" in c
        for c in outcome.changes
    )
    assert any("stored Jenkinsfile as version 1" in c for c in outcome.changes)
    assert any("project name" in d for d in outcome.differences)  # 'Captcha' != v1 label, reported
    env = EnvironmentService(uow).get("captcha", PROD)
    assert env.compose_project == "dsl" and env.auto_deploy is False  # existing env keeps its flag


# --- the command line ---------------------------------------------------------------------------


def forbidden() -> ImportServices:
    raise AssertionError("the database must not be used")


def parse_args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    return parser.parse_args(argv)


def test_dry_run_prints_everything_and_never_touches_the_database() -> None:
    out = io.StringIO()

    code = run_import(parse_args("--projects-json", str(PROJECTS), "--dry-run"), forbidden, out)
    text = out.getvalue()
    assert code == 0
    assert text.rstrip().endswith("dry run: nothing was written")
    assert "20 project(s), 20 Jenkinsfile(s) generated and valid" in text
    assert "project aic-website" in text and "| pipeline {" in text
    assert "WARNING:" in text and "secrets: no secret is referenced" in text
    assert "compose_env_files = ['/opt/docker-engine/projects/aic/ports.env']" in text


def test_a_real_run_through_the_cli_function(services: ImportServices, uow: UnitOfWork) -> None:
    out = io.StringIO()
    args = parse_args("--projects-json", str(PROJECTS), "--compose-root", str(V1 / "compose"))
    assert run_import(args, lambda: services, out) == 0
    assert "20 created, 0 with differences, 0 with errors" in out.getvalue()
    assert len(ProjectService(uow).list()) == 20
    out = io.StringIO()
    assert run_import(args, lambda: services, out) == 0
    assert "0 created, 0 with differences, 0 with errors" in out.getvalue()


def test_bad_input_fails_with_exit_code_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    code = run_import(
        parse_args("--projects-json", str(bad), "--dry-run"), forbidden, io.StringIO()
    )
    assert code == 1
    assert "cannot read v1 projects file" in capsys.readouterr().err


def test_main_dry_run_does_not_create_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "data" / "jendock.db"
    monkeypatch.setenv("JENDOCK_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("JENDOCK_SECRET_KEY_FILE", str(tmp_path / "k"))
    get_settings.cache_clear()
    try:
        code = cli.main(["import-v1", "--projects-json", str(PROJECTS), "--dry-run"])
    finally:
        get_settings.cache_clear()
    assert code == 0
    assert not db.exists() and not db.parent.exists()
    assert "dry run: nothing was written" in capsys.readouterr().out


# --- compose env files, git transport, decision summary -----------------------------------------


def test_the_v1_env_file_becomes_the_compose_env_files_of_the_environment(
    services: ImportServices, uow: UnitOfWork
) -> None:
    apply_plan(small_plan({"aic-sys"}), services, ACTOR)
    env = EnvironmentService(uow).get("aic-sys", PROD)
    assert env.compose_env_files == ["/opt/docker-engine/projects/aic/ports.env"]
    assert EnvironmentService(uow).get("aic-sys", DEV).compose_env_files == []
    [again] = apply_plan(small_plan({"aic-sys"}), services, ACTOR)
    assert not again.changes and not again.differences
    assert any("compose_env_files matches" in text for text in again.unchanged)


def test_a_recipe_without_env_file_leaves_compose_env_files_empty(tmp_path: Path) -> None:
    document = {
        "p": {
            "repository": "o/r",
            "branch": "main",
            "label": "P",
            "recipe": {"services": ["app"], "images": [{"name": "p-app"}], "tests": []},
        }
    }
    path = tmp_path / "p.json"
    path.write_text(json.dumps(document))
    [project] = build_plan(load_projects(path)).projects
    assert "compose_env_files" not in project.environment(PROD).values


def test_imported_projects_default_to_ssh_with_a_deploy_key_note(
    services: ImportServices, uow: UnitOfWork
) -> None:
    plan = small_plan({"captcha"})
    assert plan.projects[0].git_transport is GitTransport.SSH
    note = plan.projects[0].deploy_key_note
    assert "JENDOCK_GIT_SSH_KEY_FILE" in note and "ssh.github.com:443" in note
    assert "github.com/TPCT/captcha.example.com" in note
    apply_plan(plan, services, ACTOR)
    assert ProjectService(uow).get("captcha").project.git_transport is GitTransport.SSH


def test_the_https_flag_switches_the_transport(services: ImportServices, uow: UnitOfWork) -> None:
    out = io.StringIO()
    args = parse_args("--projects-json", str(PROJECTS), "--https", "--dry-run")
    assert run_import(args, forbidden, out) == 0
    assert "git transport: https" in out.getvalue() and "git transport: ssh" not in out.getvalue()
    real = parse_args("--projects-json", str(PROJECTS), "--https")
    run_import(real, lambda: services, io.StringIO())
    assert ProjectService(uow).get("captcha").project.git_transport is GitTransport.HTTPS


def test_an_existing_project_with_another_transport_is_reported_not_changed(
    services: ImportServices, uow: UnitOfWork
) -> None:
    ProjectService(uow).create(
        "captcha", "captcha.example.com", "TPCT/captcha.example.com", "development", "main"
    )
    [outcome] = apply_plan(small_plan({"captcha"}), services, ACTOR)
    assert any("git transport" in d for d in outcome.differences)
    assert ProjectService(uow).get("captcha").project.git_transport is GitTransport.HTTPS


def test_the_dry_run_starts_with_a_grouped_summary_of_operator_decisions() -> None:
    out = io.StringIO()
    run_import(parse_args("--projects-json", str(PROJECTS), "--dry-run"), forbidden, out)
    text = out.getvalue()
    head, _, _ = text.partition("project aic-website")
    assert head.startswith("#" * 78 + "\nNEEDS OPERATOR DECISION")
    order = [
        "ambiguous service -> image guesses",
        "branches that look misspelled",
        "single v1 branch assigned",
        "other environment has no v1 data",
        "tmpfs mounts dropped",
        "test environment variables that look like credentials",
    ]
    positions = [head.index(title) for title in order]
    assert positions == sorted(positions)
    assert "- elfak01: branch 'developement' looks misspelled" in head
    assert "- aic-sys: service 'sys-queue' -> image 'aic-sys-app' is a guess" in head
    assert "- 30sv01: no v1 data for the production environment" in head
    assert "- aic-ops: test in deploy-test-aic-ops: tmpfs mounts" in head
    assert "git transport for all projects: ssh" in head
    assert "[3] branches that look misspelled" in head


def test_the_summary_says_so_when_nothing_needs_a_decision(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"p": {"repository": "o/r", "branch": "main", "label": "P"}}))
    database = tmp_path / "v1.sqlite"
    db = sqlite3.connect(database)
    db.executescript(
        "CREATE TABLE registry_projects(id TEXT, legacy_binding TEXT);"
        "INSERT INTO registry_projects VALUES('p','production');"
        "CREATE TABLE registry_environments(project TEXT, name TEXT, branch TEXT, automatic INT,"
        " state TEXT);"
        "INSERT INTO registry_environments VALUES('p','development','dev',0,'provisioned');"
    )
    db.commit()
    db.close()
    out = io.StringIO()
    args = parse_args("--projects-json", str(path), "--v1-db", str(database), "--dry-run")
    run_import(args, forbidden, out)
    assert "nothing: every value came from v1 data" in out.getvalue()


def test_a_wal_snapshot_on_a_read_only_directory_can_be_read(tmp_path: Path) -> None:
    """A v1 snapshot in WAL mode on a read-only mount opens without creating ``-shm``."""
    database = tmp_path / "v1.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE unrelated (x)")
    connection.commit()
    connection.close()
    for leftover in tmp_path.glob("v1.sqlite-*"):
        leftover.unlink()
    tmp_path.chmod(0o500)
    try:
        load_registry(database)
    finally:
        tmp_path.chmod(0o700)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["v1.sqlite"]
