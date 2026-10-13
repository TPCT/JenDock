"""Per-repository deploy keys: generated with ``ssh-keygen``, stored one file per repository.

GitHub accepts a deploy key on one repository only, so each repository gets its own ed25519
key at ``<dir>/<owner>__<name>`` (see ``deploy_key_path``). Only public keys ever leave this
module.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backend.adapters.git_access import deploy_key_path
from backend.domain.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from backend.domain.values import GitHubRepository

KEYGEN_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class CreatedKey:
    """A generated key: only the public line, and whether it replaced an older key."""

    public_key: str
    rotated: bool


class DeployKeyStore:
    """Creates, shows and lists the deploy keys of one directory."""

    def __init__(self, directory: Path, keygen: str = "ssh-keygen") -> None:
        self._dir = directory
        self._keygen = keygen

    def create(self, repository: str, *, rotate: bool = False) -> CreatedKey:
        """Generate a key; refuses to replace an existing one unless ``rotate``."""
        repo = str(GitHubRepository(repository))
        target = deploy_key_path(self._dir, repo)
        if target.exists() and not rotate:
            raise ConflictError(
                f"a deploy key for {repo} already exists; use --rotate to replace it "
                "(the public key on GitHub must be replaced too)"
            )
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Same directory as the target, so the final rename is atomic.
        scratch = Path(tempfile.mkdtemp(prefix=".new-", dir=self._dir))
        try:
            key = scratch / "key"
            self._run(["-q", "-t", "ed25519", "-N", "", "-C", f"jendock:{repo}", "-f", str(key)])
            public = (scratch / "key.pub").read_text().strip()
            key.chmod(0o600)
            rotated = target.exists()
            key.replace(target)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return CreatedKey(public, rotated)

    def public_key(self, repository: str) -> str:
        """The public key line of an existing key, derived from the private key file."""
        repo = str(GitHubRepository(repository))
        path = deploy_key_path(self._dir, repo)
        if not path.is_file():
            raise NotFoundError(f"no deploy key for {repo}")
        return self._run(["-y", "-f", str(path)]).strip()

    def repositories(self) -> list[str]:
        """The repositories that have a key, sorted."""
        if not self._dir.is_dir():
            return []
        names = []
        for entry in self._dir.iterdir():
            owner, sep, name = entry.name.partition("__")
            if not sep or not entry.is_file():
                continue
            try:
                names.append(str(GitHubRepository(f"{owner}/{name}")))
            except ValidationError:
                continue
        return sorted(names)

    def _run(self, args: list[str]) -> str:
        try:
            result = subprocess.run(  # noqa: S603 - explicit argv list, never a shell
                [self._keygen, *args],
                capture_output=True,
                text=True,
                timeout=KEYGEN_TIMEOUT,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ConfigurationError(f"{self._keygen} could not be run: {error}") from error
        if result.returncode != 0:
            raise ConfigurationError(f"{self._keygen} failed: {result.stderr.strip()[:200]}")
        return result.stdout
