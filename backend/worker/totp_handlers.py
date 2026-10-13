"""RPC methods for two-factor keys: the API stores ciphertext, the worker opens it.

``totp.generate`` returns the new key's text once (for the enrollment screen) with its
ciphertext; ``totp.check`` takes a ciphertext and a code and answers with the matching time
step only. Params are never echoed in errors because they carry codes and ciphertexts.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.services.totp import TotpVault
from backend.worker.rpc import BadRequestError, Handler, RpcCall


class _CheckParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ciphertext_b64: str = Field(max_length=2048)
    code: str = Field(max_length=16)
    after_step: int | None = None


def totp_handlers(vault: TotpVault) -> Mapping[str, Handler]:
    """The ``totp.*`` methods backed by ``vault``."""

    def generate(_call: RpcCall) -> object:
        secret, ciphertext = vault.generate()
        return {"secret": secret, "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii")}

    def check(call: RpcCall) -> object:
        try:
            params = _CheckParams.model_validate(call.params)
            ciphertext = base64.b64decode(params.ciphertext_b64, validate=True)
        except (PydanticValidationError, binascii.Error) as error:
            raise BadRequestError("invalid params") from error
        return {"step": vault.check(ciphertext, params.code, params.after_step)}

    return {"totp.generate": generate, "totp.check": check}
