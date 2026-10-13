"""Secret endpoints. Everything goes through the worker; the API never decrypts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from backend.api.deps import Container, require
from backend.api.principal import Principal
from backend.api.schemas.secrets import RevealResponse, SecretResponse, SetSecretRequest
from backend.domain.access import Action
from backend.domain.enums import EnvironmentName

router = APIRouter(prefix="/api/projects/{slug}/environments/{env}/secrets", tags=["secrets"])
Reader = Annotated[Principal, Depends(require(Action.SECRET_LIST))]
Writer = Annotated[Principal, Depends(require(Action.SECRET_WRITE))]
Revealer = Annotated[Principal, Depends(require(Action.SECRET_REVEAL))]


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@dataclass(frozen=True, slots=True)
class SecretPath:
    """Path parameters identifying one secret."""

    slug: str
    env: EnvironmentName
    name: str


@router.get("")
def list_secrets(
    path: Annotated[EnvPath, Depends()], principal: Reader, container: Container
) -> list[SecretResponse]:
    """List secret names and timestamps."""
    infos = container.secrets.list_secrets(path.slug, path.env, principal.actor)
    return [SecretResponse.from_info(info) for info in infos]


@router.put("/{name}")
def put_secret(
    path: Annotated[SecretPath, Depends()],
    body: SetSecretRequest,
    principal: Writer,
    container: Container,
) -> SecretResponse:
    """Create or rotate a secret."""
    info = container.secrets.set_secret(path.slug, path.env, path.name, body.value, principal.actor)
    return SecretResponse.from_info(info)


@router.delete("/{name}", status_code=204)
def delete_secret(
    path: Annotated[SecretPath, Depends()], principal: Writer, container: Container
) -> Response:
    """Delete a secret."""
    container.secrets.delete_secret(path.slug, path.env, path.name, principal.actor)
    return Response(status_code=204)


@router.post("/{name}/reveal")
def reveal_secret(
    path: Annotated[SecretPath, Depends()], principal: Revealer, container: Container
) -> RevealResponse:
    """Return the plaintext of one secret; the worker audits the reveal."""
    value = container.secrets.reveal_secret(path.slug, path.env, path.name, principal.actor)
    return RevealResponse(name=path.name, value=value)
