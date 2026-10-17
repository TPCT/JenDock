"""Database image and environment credential parsing rules."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from sqlalchemy import Engine

from backend.domain.actor import Actor
from backend.domain.database import ConnectionRef, DatabaseEngine
from backend.domain.database_detection import (
    DetectionContainer,
    choose_database_network,
    parse_database_credentials,
    scope_database_services,
)
from backend.domain.enums import EnvironmentName
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.repositories.models import DatabaseBackup, DatabaseConnection
from backend.services.database_detection import (
    DatabaseDetectionService,
    DetectionLockRegistry,
)
from backend.services.environments import EnvironmentService, EnvironmentUpdate
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretInfo, SecretService
from tests.container_support import FakeEngine, make_record


class _SecretGateway:
    """Use the real encrypted test store behind the SecretsGateway boundary."""

    def __init__(self, engine: Engine, cipher: SecretCipher) -> None:
        self._engine = engine
        self._cipher = cipher

    def _service(self) -> SecretService:
        return SecretService(UnitOfWork(create_session_factory(self._engine)), self._cipher)

    def ping(self) -> None:
        return None

    def set_secret(
        self, slug: str, env: EnvironmentName, name: str, value: str, actor: Actor
    ) -> SecretInfo:
        return self._service().set(slug, env, name, value, actor)

    def list_secrets(self, slug: str, env: EnvironmentName, actor: Actor) -> list[SecretInfo]:
        return list(self._service().list_names(slug, env))

    def delete_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
        return self._service().delete(slug, env, name, actor)

    def reveal_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> str:
        return self._service().reveal(slug, env, name, actor)


class _ContendedSecretGateway(_SecretGateway):
    """Pause the first write so a second detection can expose an unprotected race."""

    def __init__(self, engine: Engine, cipher: SecretCipher) -> None:
        super().__init__(engine, cipher)
        self.first_write_started = Event()
        self.release_first_write = Event()
        self.second_write_started = Event()
        self._write_count = 0
        self._write_lock = Lock()

    def set_secret(
        self, slug: str, env: EnvironmentName, name: str, value: str, actor: Actor
    ) -> SecretInfo:
        secret = super().set_secret(slug, env, name, value, actor)
        with self._write_lock:
            self._write_count += 1
            write_number = self._write_count
        if write_number == 1:
            self.first_write_started.set()
            if not self.release_first_write.wait(timeout=5):
                raise TimeoutError("test did not release the first secret write")
        elif write_number == 2:
            self.second_write_started.set()
        return secret


class _InspectionStub:
    def test(self, ref: ConnectionRef, actor: Actor) -> dict[str, object]:
        return {"ok": True, "error": None, "server": None, "network": "demo-dev_default"}


def test_database_detection_scopes_dependencies_and_single_database_networks() -> None:
    containers = (
        DetectionContainer("web", ("app",), ("api",), False),
        DetectionContainer("api", ("app", "data"), ("db",), False),
        DetectionContainer("db", ("data",), (), True),
        DetectionContainer("metrics-db", ("app",), (), True),
    )
    assert scope_database_services(containers, ("web",)) == frozenset({"db", "metrics-db"})
    assert choose_database_network(containers[2], containers, ("web",)) == "data"


def test_shared_network_does_not_scope_multiple_databases() -> None:
    containers = (
        DetectionContainer("web", ("shared",), (), False),
        DetectionContainer("db-a", ("shared",), (), True),
        DetectionContainer("db-b", ("shared",), (), True),
    )
    assert scope_database_services(containers, ("web",)) == frozenset()


def test_detection_stores_custom_network_for_database_connection(
    engine: Engine, project: str, cipher: SecretCipher
) -> None:
    env = EnvironmentName.DEVELOPMENT
    EnvironmentService(UnitOfWork(create_session_factory(engine))).update(
        project, env, EnvironmentUpdate(compose_project="octa", services=["web"])
    )
    containers = FakeEngine()
    containers.add(make_record(project="octa", service="web", networks=("octa_data",)))
    db = make_record(
        project="octa",
        service="db",
        image="postgres:16",
        env={"POSTGRES_PASSWORD": "custom-network-secret"},
        labels={"com.docker.compose.depends_on": "db:service_healthy:false"},
        networks=("octa_data",),
    )
    containers.add(db)
    service = DatabaseDetectionService(
        UnitOfWork(create_session_factory(engine)),
        _SecretGateway(engine, cipher),
        containers,
        _InspectionStub(),
    )

    result = service.detect(project, env, Actor.system("test"))

    connection = result.results[0].connection
    assert connection is not None
    assert connection.network == "octa_data"
    assert connection.auto_detected is True


def test_shared_compose_project_detection_is_scoped_per_environment(
    engine: Engine, project: str, cipher: SecretCipher
) -> None:
    sessions = create_session_factory(engine)
    environments = EnvironmentService(UnitOfWork(sessions))
    environments.update(
        project,
        EnvironmentName.DEVELOPMENT,
        EnvironmentUpdate(compose_project="shared", services=["web-a"]),
    )
    environments.update(
        project,
        EnvironmentName.PRODUCTION,
        EnvironmentUpdate(compose_project="shared", services=["web-b"]),
    )
    containers = FakeEngine()
    for app, database in (("web-a", "db-a"), ("web-b", "db-b")):
        containers.add(
            make_record(
                project="shared",
                service=app,
                labels={"com.docker.compose.depends_on": f"{database}:service_started:false"},
            )
        )
        containers.add(
            make_record(
                project="shared",
                service=database,
                image="postgres:16",
                env={"POSTGRES_PASSWORD": f"secret-{database}"},
            )
        )
    found: dict[EnvironmentName, set[str]] = {}
    for env in (EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION):
        result = DatabaseDetectionService(
            UnitOfWork(sessions), _SecretGateway(engine, cipher), containers, _InspectionStub()
        ).detect(project, env, Actor.system("test"))
        found[env] = {item.service for item in result.results}
    assert found == {
        EnvironmentName.DEVELOPMENT: {"db-a"},
        EnvironmentName.PRODUCTION: {"db-b"},
    }


def test_transitive_dependencies_are_detected_through_service(
    engine: Engine, project: str, cipher: SecretCipher
) -> None:
    env = EnvironmentName.DEVELOPMENT
    EnvironmentService(UnitOfWork(create_session_factory(engine))).update(
        project, env, EnvironmentUpdate(compose_project="chain", services=["web"])
    )
    containers = FakeEngine()
    containers.add(
        make_record(
            project="chain",
            service="web",
            labels={"com.docker.compose.depends_on": "api:service_started:false"},
        )
    )
    containers.add(
        make_record(
            project="chain",
            service="api",
            labels={"com.docker.compose.depends_on": "db:service_healthy:false"},
        )
    )
    containers.add(
        make_record(
            project="chain",
            service="db",
            image="postgres:16",
            env={"POSTGRES_PASSWORD": "transitive-secret"},
        )
    )
    result = DatabaseDetectionService(
        UnitOfWork(create_session_factory(engine)),
        _SecretGateway(engine, cipher),
        containers,
        _InspectionStub(),
    ).detect(project, env, Actor.system("test"))
    assert [item.service for item in result.results] == ["db"]


def test_prune_preserves_manual_backed_up_and_stopped_in_scope_connections(
    engine: Engine, project: str, cipher: SecretCipher
) -> None:
    sessions = create_session_factory(engine)
    env = EnvironmentName.DEVELOPMENT
    EnvironmentService(UnitOfWork(sessions)).update(
        project, env, EnvironmentUpdate(compose_project="prune", services=["web"])
    )
    with UnitOfWork(sessions) as uow:
        environment = uow.environments.get_by_project_slug(project, env)
        assert environment is not None
        for name, host, automatic in (
            ("old", "old-db", True),
            ("manual", "manual-db", False),
            ("archived", "archived-db", True),
            ("stopped", "stopped-db", True),
        ):
            connection = uow.database_connections.add(
                DatabaseConnection(
                    environment_id=environment.id,
                    name=name,
                    engine=DatabaseEngine.POSTGRESQL,
                    host=host,
                    port=5432,
                    database="app",
                    username="reader",
                    password_secret=f"DBCONN_{name.upper()}_PASSWORD",
                    auto_detected=automatic,
                )
            )
            if name == "archived":
                uow.database_backups.add(
                    DatabaseBackup(
                        connection_id=connection.id,
                        environment_id=environment.id,
                        trigger="manual",
                        status="succeeded",
                    )
                )
    containers = FakeEngine()
    containers.add(
        make_record(
            project="prune",
            service="web",
            labels={
                "com.docker.compose.depends_on": (
                    "db:service_started:false,stopped-db:service_started:false"
                )
            },
        )
    )
    containers.add(
        make_record(
            project="prune",
            service="db",
            image="postgres:16",
            env={"POSTGRES_PASSWORD": "running-secret"},
        )
    )
    containers.add(
        make_record(
            project="prune",
            service="stopped-db",
            image="postgres:16",
            running=False,
            env={"POSTGRES_PASSWORD": "stopped-secret"},
        )
    )
    result = DatabaseDetectionService(
        UnitOfWork(sessions), _SecretGateway(engine, cipher), containers, _InspectionStub()
    ).detect(project, env, Actor.system("test"), prune=True)
    assert result.removed == ("old",)
    assert result.kept == (("archived", "has backups"),)
    with UnitOfWork(sessions) as uow:
        environment = uow.environments.get_by_project_slug(project, env)
        assert environment is not None
        names = {
            item.name for item in uow.database_connections.list_for_environment(environment.id)
        }
    assert {"manual", "archived", "stopped", "db"} <= names
    assert "old" not in names


def test_empty_services_detects_every_database_service(
    engine: Engine, project: str, cipher: SecretCipher
) -> None:
    env = EnvironmentName.DEVELOPMENT
    EnvironmentService(UnitOfWork(create_session_factory(engine))).update(
        project, env, EnvironmentUpdate(compose_project="all-databases", services=[])
    )
    containers = FakeEngine()
    for service in ("db-a", "db-b"):
        containers.add(
            make_record(
                project="all-databases",
                service=service,
                image="postgres:16",
                env={"POSTGRES_PASSWORD": f"secret-{service}"},
            )
        )
    result = DatabaseDetectionService(
        UnitOfWork(create_session_factory(engine)),
        _SecretGateway(engine, cipher),
        containers,
        _InspectionStub(),
    ).detect(project, env, Actor.system("test"))
    assert {item.service for item in result.results} == {"db-a", "db-b"}


def test_concurrent_detection_keeps_one_connection_and_its_secret(
    engine: Engine, project: str, cipher: SecretCipher
) -> None:
    session_factory = create_session_factory(engine)
    uow = UnitOfWork(session_factory)
    env = EnvironmentName.DEVELOPMENT
    EnvironmentService(uow).update(project, env, EnvironmentUpdate(compose_project="demo-dev"))
    containers = FakeEngine()
    containers.add(
        make_record(
            project="demo-dev",
            service="db",
            image="postgres:16",
            env={"POSTGRES_PASSWORD": "concurrent-database-password"},
        )
    )
    gateway = _ContendedSecretGateway(engine, cipher)
    locks = DetectionLockRegistry()
    services = [
        DatabaseDetectionService(
            UnitOfWork(session_factory), gateway, containers, _InspectionStub(), lock_registry=locks
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(services[0].detect, project, env, Actor.system("test"))
        assert gateway.first_write_started.wait(timeout=2)
        second = pool.submit(services[1].detect, project, env, Actor.system("test"))
        try:
            second_overlapped_secret_write = gateway.second_write_started.wait(timeout=0.2)
        finally:
            gateway.release_first_write.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert not second_overlapped_secret_write
    assert sorted(result.results[0].status for result in results) == ["created", "existing"]
    environment = EnvironmentService(UnitOfWork(session_factory)).get(project, env)
    with UnitOfWork(session_factory) as active:
        assert len(active.database_connections.list_for_environment(environment.id)) == 1
        secret = active.secrets.get(environment.id, "DBCONN_DB_PASSWORD")
        assert secret is not None
        assert cipher.decrypt(secret.ciphertext) == "concurrent-database-password"


def test_matches_registry_image_and_parses_mysql_credentials() -> None:
    result = parse_database_credentials(
        "library/mysql:8.4",
        {"MYSQL_DATABASE": "app", "MYSQL_USER": "reader", "MYSQL_PASSWORD": "secret"},
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is DatabaseEngine.MYSQL
    assert result.credentials.database == "app"
    assert result.credentials.username == "reader"
    assert result.credentials.password == "secret"


def test_mariadb_accepts_aliases_and_root_fallback() -> None:
    result = parse_database_credentials(
        "mariadb:11", {"MYSQL_DATABASE": "app", "MYSQL_ROOT_PASSWORD": "root-secret"}
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is DatabaseEngine.MARIADB
    assert result.credentials.username == "root"
    assert result.credentials.password == "root-secret"


def test_mysql_image_names_parse_to_their_distinct_engines() -> None:
    mariadb = parse_database_credentials(
        "mariadb:10.11.8",
        {"MARIADB_DATABASE": "app", "MARIADB_USER": "reader", "MARIADB_PASSWORD": "secret"},
    )
    mysql = parse_database_credentials(
        "mysql:8.4", {"MYSQL_DATABASE": "app", "MYSQL_USER": "reader", "MYSQL_PASSWORD": "secret"}
    )
    assert mariadb is not None and mariadb.credentials is not None
    assert mysql is not None and mysql.credentials is not None
    assert mariadb.credentials.engine is DatabaseEngine.MARIADB
    assert mysql.credentials.engine is DatabaseEngine.MYSQL


@pytest.mark.parametrize(
    ("auto_detected", "expected_engine"),
    [(True, DatabaseEngine.MARIADB), (False, DatabaseEngine.MYSQL)],
)
def test_redetection_reuses_mysql_family_connection_and_respects_ownership(
    engine: Engine,
    project: str,
    cipher: SecretCipher,
    auto_detected: bool,
    expected_engine: DatabaseEngine,
) -> None:
    sessions = create_session_factory(engine)
    env = EnvironmentName.DEVELOPMENT
    EnvironmentService(UnitOfWork(sessions)).update(
        project, env, EnvironmentUpdate(compose_project="maria-redetect", services=["web"])
    )
    with UnitOfWork(sessions) as uow:
        environment = uow.environments.get_by_project_slug(project, env)
        assert environment is not None
        uow.database_connections.add(
            DatabaseConnection(
                environment_id=environment.id,
                name="db",
                engine=DatabaseEngine.MYSQL,
                host="db",
                port=3306,
                database="app",
                username="reader",
                password_secret="DBCONN_DB_PASSWORD",
                auto_detected=auto_detected,
            )
        )
    containers = FakeEngine()
    containers.add(
        make_record(
            project="maria-redetect",
            service="web",
            labels={"com.docker.compose.depends_on": "db:service_started:false"},
        )
    )
    containers.add(
        make_record(
            project="maria-redetect",
            service="db",
            image="mariadb:10.11.8",
            env={"MARIADB_DATABASE": "app", "MARIADB_USER": "reader", "MARIADB_PASSWORD": "secret"},
        )
    )

    result = DatabaseDetectionService(
        UnitOfWork(sessions), _SecretGateway(engine, cipher), containers, _InspectionStub()
    ).detect(project, env, Actor.system("test"))

    assert result.results[0].status == "existing"
    assert result.results[0].connection is not None
    assert result.results[0].connection.engine is expected_engine
    with UnitOfWork(sessions) as uow:
        environment = uow.environments.get_by_project_slug(project, env)
        assert environment is not None
        rows = uow.database_connections.list_for_environment(environment.id)
        assert len(rows) == 1
        assert rows[0].engine is expected_engine


def test_named_mysql_user_without_password_falls_back_to_root() -> None:
    result = parse_database_credentials(
        "mysql:8",
        {
            "MYSQL_DATABASE": "app",
            "MYSQL_USER": "application",
            "MYSQL_ROOT_PASSWORD": "root-secret",
        },
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.username == "root"
    assert result.credentials.password == "root-secret"


def test_root_only_password_flags_do_not_reject_a_normal_user() -> None:
    result = parse_database_credentials(
        "mariadb:11",
        {
            "MARIADB_DATABASE": "app",
            "MARIADB_USER": "reader",
            "MARIADB_PASSWORD": "reader-secret",
            "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD": "yes",
        },
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.username == "reader"


def test_postgresql_defaults_database_and_user() -> None:
    result = parse_database_credentials("bitnami/postgresql:16", {"POSTGRES_PASSWORD": "secret"})
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is DatabaseEngine.POSTGRESQL
    assert result.credentials.database == "postgres"
    assert result.credentials.username == "postgres"


def test_missing_database_or_file_password_is_skipped_safely() -> None:
    no_database = parse_database_credentials("mysql:8", {"MYSQL_PASSWORD": "secret"})
    assert (
        no_database is not None
        and no_database.reason == "no database name in the container environment"
    )
    file_password = parse_database_credentials(
        "postgres:16", {"POSTGRES_DB": "app", "POSTGRES_PASSWORD_FILE": "/run/secret"}
    )
    assert file_password is not None and file_password.credentials is None


def test_non_database_image_is_ignored() -> None:
    assert parse_database_credentials("redis:7", {}) is None


def test_custom_postgres_image_parses_standard_credentials() -> None:
    result = parse_database_credentials(
        "elfak-postgres:16-ubuntu24",
        {
            "POSTGRES_DB": "app",
            "POSTGRES_USER": "reader",
            "POSTGRES_PASSWORD": "secret",
            "PG_MAJOR": "16",
            "PGDATA": "/var/lib/postgresql/data",
        },
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is DatabaseEngine.POSTGRESQL
    assert result.credentials.database == "app"
    assert result.credentials.username == "reader"
    assert result.credentials.password == "secret"


@pytest.mark.parametrize(
    ("image", "env", "expected_engine"),
    [
        (
            "bitnami/mariadb:11",
            {"MARIADB_DATABASE": "app", "MARIADB_USER": "reader", "MARIADB_PASSWORD": "secret"},
            DatabaseEngine.MARIADB,
        ),
        (
            "registry.example.com/team/percona-mysql:8",
            {"MYSQL_DATABASE": "app", "MYSQL_USER": "reader", "MYSQL_PASSWORD": "secret"},
            DatabaseEngine.MYSQL,
        ),
        (
            "my_postgres.custom",
            {"POSTGRES_DB": "app", "POSTGRES_PASSWORD": "secret"},
            DatabaseEngine.POSTGRESQL,
        ),
    ],
)
def test_custom_image_name_segments_identify_database_engine(
    image: str, env: dict[str, str], expected_engine: DatabaseEngine
) -> None:
    result = parse_database_credentials(image, env)
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is expected_engine


@pytest.mark.parametrize(
    ("env", "expected_engine"),
    [
        ({"POSTGRES_PASSWORD": "secret"}, DatabaseEngine.POSTGRESQL),
        (
            {
                "MARIADB_DATABASE": "app",
                "MARIADB_PASSWORD": "secret",
                "MARIADB_USER": "reader",
            },
            DatabaseEngine.MARIADB,
        ),
        (
            {"MYSQL_DATABASE": "app", "MYSQL_PASSWORD": "secret", "MYSQL_USER": "reader"},
            DatabaseEngine.MYSQL,
        ),
    ],
)
def test_custom_image_is_identified_by_database_environment(
    env: dict[str, str], expected_engine: DatabaseEngine
) -> None:
    result = parse_database_credentials("acme/db-server:1", env)
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is expected_engine


@pytest.mark.parametrize(
    ("image", "env"),
    [
        ("acme/api:2", {"APP_DB_PASSWORD": "secret", "DB_HOST": "db"}),
        ("mysqlrouter:8", {}),
        ("mysql.example.com/team/api:1", {}),
    ],
)
def test_non_database_image_and_environment_are_ignored(image: str, env: dict[str, str]) -> None:
    assert parse_database_credentials(image, env) is None


@pytest.mark.parametrize(
    "env",
    [
        {"MYSQL_HOST": "db", "MYSQL_DATABASE": "app", "MYSQL_PASSWORD": "secret"},
        {"POSTGRES_HOST": "db", "POSTGRES_PASSWORD": "secret"},
    ],
)
def test_client_database_environment_does_not_identify_server(
    env: dict[str, str],
) -> None:
    assert parse_database_credentials("acme/api:2", env) is None
