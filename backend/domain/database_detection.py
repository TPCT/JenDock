"""Pure image and credential parsing for automatic database connection detection."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain.database import DatabaseEngine


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
        DetectedCredentials(DatabaseEngine.MYSQL, database, username or "root", password)
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
