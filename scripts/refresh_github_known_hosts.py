"""Rewrite docker/github_known_hosts from GitHub's published SSH host keys.

GitHub publishes them (with their fingerprints) at https://api.github.com/meta under
``ssh_keys``. Run this when GitHub rotates its host key, compare the printed fingerprint with
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
and commit the result. Needs network access; nothing else in the platform does.

    .venv/bin/python scripts/refresh_github_known_hosts.py
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

META_URL = "https://api.github.com/meta"
TARGET = Path(__file__).resolve().parent.parent / "docker" / "github_known_hosts"
KEY_TYPE = "ssh-ed25519"
HEADER = (
    "# Pinned GitHub SSH host key (ed25519), refreshed from https://api.github.com/meta\n"
    "# by scripts/refresh_github_known_hosts.py. Only ed25519 is pinned and accepted.\n"
    "# The worker connects to ssh.github.com:443 with HostKeyAlias=github.com.\n"
)


def main() -> None:
    """Fetch the keys, keep the ed25519 one and write both host name forms."""
    with urllib.request.urlopen(META_URL, timeout=15) as response:
        keys: list[str] = json.load(response)["ssh_keys"]
    ed25519 = [key for key in keys if key.startswith(KEY_TYPE + " ")]
    if len(ed25519) != 1:
        raise SystemExit(f"expected exactly one {KEY_TYPE} key, got {len(ed25519)}")
    lines = [f"github.com {ed25519[0]}", f"[ssh.github.com]:443 {ed25519[0]}"]
    TARGET.write_text(HEADER + "\n".join(lines) + "\n")
    print(f"wrote {TARGET}; verify the fingerprint with: ssh-keygen -lf {TARGET}")


if __name__ == "__main__":
    main()
