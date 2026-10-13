"""The one rule for reading data of a database: production needs the ``admin`` scope."""

from __future__ import annotations

from backend.api.principal import Principal
from backend.domain.enums import EnvironmentName, TokenScope
from backend.domain.errors import PermissionDeniedError


def require_data_scope(principal: Principal, env: EnvironmentName) -> None:
    """Rows and the console need ``read`` on development and ``admin`` on production."""
    if env is EnvironmentName.PRODUCTION and not principal.has_scope(TokenScope.ADMIN):
        raise PermissionDeniedError("production data requires the admin scope")
