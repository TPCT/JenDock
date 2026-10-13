"""Two-factor authentication: enrollment, recovery codes, disabling and login verification.

Keys are generated and opened by the worker (see ``backend.services.totp``): this service
stores only ciphertext. Recovery codes carry 80 bits of entropy, so their plain SHA-256 is
enough at rest (the same reasoning as for session and API tokens). A TOTP time step is
accepted once (compare-and-set), so a code observed over a shoulder cannot be replayed.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from backend.adapters.worker_client import TotpGateway
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ConflictError, NotFoundError, ValidationError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import User
from backend.services.audit import add_audit_event
from backend.services.credentials import require_password
from backend.services.passwords import PasswordHasher
from backend.services.rate_limit import LoginRateLimiter
from backend.services.sessions import hash_token
from backend.services.totp import provisioning_uri

RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_RECOVERY_LENGTH = 16
_CODE_LENGTH = 6
INVALID_CODE = "the code is incorrect"
METHOD_TOTP = "totp"
METHOD_RECOVERY = "recovery_code"


@dataclass(frozen=True, slots=True)
class TwoFactorStatus:
    """Whether two-factor is on and how many recovery codes are left."""

    enabled: bool
    enabled_at: datetime | None
    recovery_codes_left: int


@dataclass(frozen=True, slots=True)
class Enrollment:
    """What the enrollment screen shows once: the manual key and the ``otpauth://`` URI."""

    secret: str
    uri: str


def new_recovery_codes() -> list[str]:
    """Ten random one-time codes formatted ``xxxx-xxxx-xxxx-xxxx`` (80 bits each)."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append("-".join(raw[i : i + 4] for i in range(0, _RECOVERY_LENGTH, 4)))
    return codes


def normalize_code(code: str) -> str:
    """Strip spaces and dashes and lowercase, so ``ABCD-efgh ...`` and ``123 456`` both work."""
    return "".join(char for char in code if char not in " -\t").lower()


def hash_recovery_code(code: str) -> str:
    """The stored form of a (normalized) recovery code."""
    return hash_token(normalize_code(code))


class TwoFactorService:
    """Manages a user's second factor and verifies it at login."""

    def __init__(
        self,
        uow: UnitOfWork,
        hasher: PasswordHasher,
        totp: TotpGateway,
        limiter: LoginRateLimiter,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._totp = totp
        self._limiter = limiter
        self._clock = clock

    def status(self, user_id: int) -> TwoFactorStatus:
        """The user's two-factor state."""
        with self._uow:
            user = self._require(user_id)
            return TwoFactorStatus(
                user.totp_enabled_at is not None,
                user.totp_enabled_at,
                self._uow.users.count_recovery_codes(user_id),
            )

    def begin_enrollment(self, user_id: int) -> Enrollment:
        """Create a pending key (replacing an earlier pending one); ``ConflictError`` if on."""
        enrollment = self._totp.totp_generate()
        with self._uow:
            user = self._require(user_id)
            if user.totp_enabled_at is not None:
                raise ConflictError("two-factor authentication is already enabled")
            user.totp_secret = enrollment.ciphertext
            user.totp_last_step = None
            return Enrollment(enrollment.secret, provisioning_uri(user.username, enrollment.secret))

    def confirm_enrollment(
        self, user_id: int, code: str, actor: Actor, client_ip: str
    ) -> list[str]:
        """Activate with a valid code and return the recovery codes (shown once)."""
        username, ciphertext, enabled = self._load(user_id)
        if enabled:
            raise ConflictError("two-factor authentication is already enabled")
        if ciphertext is None:
            raise ValidationError("start enrollment first")
        self._limiter.check(username, client_ip)
        if not self._accept_totp(user_id, ciphertext, normalize_code(code)):
            self._limiter.record_failure(username, client_ip)
            raise ValidationError(INVALID_CODE)
        codes = new_recovery_codes()
        now = self._clock()
        with self._uow:
            user = self._require(user_id)
            user.totp_enabled_at = now
            self._uow.users.replace_recovery_codes(
                user_id, [hash_recovery_code(item) for item in codes], now
            )
            self._audit(actor, "two_factor.enable", user)
        return codes

    def regenerate_recovery_codes(
        self, user_id: int, code: str, actor: Actor, client_ip: str
    ) -> list[str]:
        """Replace all recovery codes after a valid second-factor code."""
        self._require_second_factor(user_id, code, client_ip)
        codes = new_recovery_codes()
        now = self._clock()
        with self._uow:
            user = self._require(user_id)
            self._uow.users.replace_recovery_codes(
                user_id, [hash_recovery_code(item) for item in codes], now
            )
            self._audit(actor, "two_factor.recovery_regenerate", user)
        return codes

    def disable(self, user_id: int, password: str, code: str, actor: Actor, client_ip: str) -> None:
        """Turn two-factor off; needs the password and a valid code (or recovery code)."""
        with self._uow:
            user = self._require(user_id)
            if user.totp_enabled_at is None:
                raise ConflictError("two-factor authentication is not enabled")
            require_password(self._hasher, self._limiter, user, password, client_ip)
        self._require_second_factor(user_id, code, client_ip)
        with self._uow:
            user = self._require(user_id)
            user.totp_secret = None
            user.totp_enabled_at = None
            user.totp_last_step = None
            self._uow.users.delete_recovery_codes(user_id)
            self._audit(actor, "two_factor.disable", user)

    def verify_login(self, user_id: int, code: str) -> str | None:
        """Check the second factor of a login: ``"totp"``, ``"recovery_code"`` or ``None``."""
        _name, ciphertext, enabled = self._load(user_id)
        if not enabled or ciphertext is None:
            return None
        return self._check(user_id, ciphertext, code)

    def is_enabled(self, user_id: int) -> bool:
        """Whether the user must present a second factor to log in."""
        with self._uow:
            return self._require(user_id).totp_enabled_at is not None

    def _require_second_factor(self, user_id: int, code: str, client_ip: str) -> None:
        username, ciphertext, enabled = self._load(user_id)
        if not enabled or ciphertext is None:
            raise ConflictError("two-factor authentication is not enabled")
        self._limiter.check(username, client_ip)
        if self._check(user_id, ciphertext, code) is None:
            self._limiter.record_failure(username, client_ip)
            raise ValidationError(INVALID_CODE)

    def _check(self, user_id: int, ciphertext: bytes, code: str) -> str | None:
        normalized = normalize_code(code)
        if len(normalized) == _CODE_LENGTH and normalized.isdigit():
            return METHOD_TOTP if self._accept_totp(user_id, ciphertext, normalized) else None
        if len(normalized) != _RECOVERY_LENGTH:
            return None
        with self._uow:
            used = self._uow.users.consume_recovery_code(
                user_id, hash_recovery_code(normalized), self._clock()
            )
        return METHOD_RECOVERY if used else None

    def _accept_totp(self, user_id: int, ciphertext: bytes, code: str) -> bool:
        with self._uow:
            last_step = self._require(user_id).totp_last_step
        # The worker opens the key outside any transaction: no database lock during the call.
        step = self._totp.totp_check(ciphertext, normalize_code(code), last_step)
        if step is None:
            return False
        with self._uow:
            return self._uow.users.advance_totp_step(user_id, step)

    def _load(self, user_id: int) -> tuple[str, bytes | None, bool]:
        with self._uow:
            user = self._require(user_id)
            return user.username, user.totp_secret, user.totp_enabled_at is not None

    def _require(self, user_id: int) -> User:
        user = self._uow.users.get(user_id)
        if user is None:
            raise NotFoundError(f"user not found: {user_id}")
        return user

    def _audit(self, actor: Actor, action: str, user: User) -> None:
        entry = AuditEntry(
            actor, action, AuditTarget("user", str(user.id)), {"username": user.username}
        )
        add_audit_event(self._uow, entry, self._clock)
