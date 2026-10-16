"""Pure image and credential parsing for automatic database connection detection."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass

from backend.domain.database import DatabaseEngine


@dataclass(frozen=True, slots=True)
class DetectionContainer:
    """The service, dependencies, networks and database classification of a container."""

    service: str
    networks: tuple[str, ...]
    depends_on: tuple[str, ...]
    is_database: bool


def _service_closure(containers: Sequence[DetectionContainer], services: Sequence[str]) -> set[str]:
    """Return services and every dependency reachable from them."""
    dependencies: dict[str, set[str]] = defaultdict(set)
    for item in containers:
        dependencies[item.service].update(item.depends_on)
    closure = set(services)
    pending = deque(services)
    while pending:
        for dependency in dependencies[pending.popleft()] - closure:
            closure.add(dependency)
            pending.append(dependency)
    return closure


def scope_database_services(
    containers: Sequence[DetectionContainer], services: Sequence[str]
) -> frozenset[str]:
    """Return database services in scope for an environment's configured app services."""
    databases = {item.service for item in containers if item.is_database}
    if not services:
        return frozenset(databases)
    closure = _service_closure(containers, services)
    scoped = databases & closure
    counts: dict[str, int] = defaultdict(int)
    for item in containers:
        if item.is_database:
            for network in item.networks:
                counts[network] += 1
    app_networks = {
        network
        for item in containers
        if not item.is_database and item.service in closure
        for network in item.networks
    }
    for item in containers:
        shared = set(item.networks) & app_networks
        if item.is_database and shared and any(counts[network] == 1 for network in shared):
            scoped.add(item.service)
    return frozenset(scoped)


def choose_database_network(
    database: DetectionContainer,
    containers: Sequence[DetectionContainer],
    services: Sequence[str],
) -> str | None:
    """Pick a deterministic DB network shared with relevant app services when possible."""
    roots = services or tuple(item.service for item in containers if not item.is_database)
    closure = _service_closure(containers, roots)
    shared = {
        network
        for item in containers
        if not item.is_database and item.service in closure
        for network in item.networks
    } & set(database.networks)
    return min(shared or set(database.networks), default=None)


@dataclass(frozen=True, slots=True)
class DetectedCredentials:
    """Usable database credentials parsed from one container's environment."""

    engine: DatabaseEngine
    database: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class CredentialResult:
    """Credential parse result, with a safe skip reason when unusable."""

    credentials: DetectedCredentials | None
    reason: str | None = None


def parse_database_credentials(image: str, env: dict[str, str]) -> CredentialResult | None:
    """Parse supported database image credentials; ``None`` means an unsupported image."""
    image_name = image.rsplit("/", 1)[-1].split("@", 1)[0].split(":", 1)[0].lower()
    if image_name in {"mysql", "mariadb"}:
        return _mysql_credentials(image_name, env)
    if image_name in {"postgres", "postgresql"}:
        return _postgres_credentials(env)
    return None


def _mysql_credentials(image_name: str, env: dict[str, str]) -> CredentialResult:
    prefixes = ("MARIADB_", "MYSQL_") if image_name == "mariadb" else ("MYSQL_",)
    database = _first(env, prefixes, "DATABASE")
    username = _first(env, prefixes, "USER")
    password = _first(env, prefixes, "PASSWORD")
    root_fallback = not username or not password
    if root_fallback:
        username = "root"
        password = _first(env, prefixes, "ROOT_PASSWORD")
    if not database:
        return CredentialResult(None, "no database name in the container environment")
    password_is_file = _file_variant(
        env, prefixes, "ROOT_PASSWORD" if root_fallback else "PASSWORD"
    )
    if not password or password_is_file:
        return CredentialResult(None, "no usable database password in the container environment")
    if root_fallback and (
        env.get("MYSQL_ALLOW_EMPTY_PASSWORD") == "yes"
        or env.get("MARIADB_ALLOW_EMPTY_ROOT_PASSWORD") == "yes"
        or env.get("MYSQL_RANDOM_ROOT_PASSWORD") == "yes"
        or env.get("MARIADB_RANDOM_ROOT_PASSWORD") == "yes"
    ):
        return CredentialResult(None, "no usable database password in the container environment")
    return CredentialResult(
        DetectedCredentials(
            DatabaseEngine.MARIADB if image_name == "mariadb" else DatabaseEngine.MYSQL,
            database,
            username or "root",
            password,
        )
    )


def _postgres_credentials(env: dict[str, str]) -> CredentialResult:
    username = env.get("POSTGRES_USER") or "postgres"
    database = env.get("POSTGRES_DB") or username
    password = env.get("POSTGRES_PASSWORD")
    if not password or "POSTGRES_PASSWORD_FILE" in env:
        return CredentialResult(None, "no usable database password in the container environment")
    return CredentialResult(
        DetectedCredentials(DatabaseEngine.POSTGRESQL, database, username, password)
    )


def _first(env: dict[str, str], prefixes: tuple[str, ...], key: str) -> str | None:
    return next((env[prefix + key] for prefix in prefixes if env.get(prefix + key)), None)


def _file_variant(env: dict[str, str], prefixes: tuple[str, ...], key: str) -> bool:
    return any(prefix + key + "_FILE" in env for prefix in prefixes)
