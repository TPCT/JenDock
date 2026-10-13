"""Process-wide setup shared by every entrypoint."""

from __future__ import annotations

import os

# Files created by the api (uid 10002) and the worker (uid 10001) share group 10001 on the
# data volume, so both must be group-writable and closed to others.
SHARED_UMASK = 0o007


def apply_shared_umask() -> None:
    """Make new files group-accessible and private from others."""
    os.umask(SHARED_UMASK)
