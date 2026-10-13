"""A stand-in ``docker`` executable for testing the CLI adapter.

Behaviour comes from ``state/behavior.json`` next to the script; every call is appended to
``state/calls.jsonl``. Run it as: python fake_docker.py <state dir> <docker args...>
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

Behavior = dict[str, object]
RECORDED_ENV = ("DOCKER_HOST", "MODE", "SECRET_ARG", "PATH")


def number(behavior: Behavior, key: str, default: float = 0) -> float:
    """A numeric setting."""
    value = behavior.get(key, default)
    return float(value) if isinstance(value, int | float) else default


def lines(behavior: Behavior) -> list[str]:
    """Output lines configured for ``run``."""
    value = behavior.get("lines", [])
    return [str(item) for item in value] if isinstance(value, list) else []


def run(state: Path, args: list[str], behavior: Behavior) -> int:
    """``docker run``: capture the env file, print output, optionally linger."""
    env_file = Path(args[args.index("--env-file") + 1]) if "--env-file" in args else None
    info: dict[str, object] = {}
    if env_file is not None:
        info["env_file_text"] = env_file.read_text()
        info["env_file_mode"] = env_file.stat().st_mode & 0o777
    (state / "run_info.json").write_text(json.dumps(info))
    (state / "container").write_text("1")  # the daemon has created the container
    for line in lines(behavior):
        print(line, flush=True)
    print("to stderr", file=sys.stderr, flush=True)
    time.sleep(number(behavior, "sleep"))
    if env_file is not None:
        (state / "env_exists_late").write_text(str(env_file.exists()))
    return int(number(behavior, "exit"))


def inspect(state: Path, _args: list[str], _behavior: Behavior) -> int:
    """``docker inspect``: succeeds once ``run`` created the container."""
    return 0 if (state / "container").exists() else 1


def ps(_state: Path, _args: list[str], behavior: Behavior) -> int:
    """``docker ps``: print the configured ids."""
    ids = behavior.get("ps", [])
    print("\n".join(str(item) for item in ids) if isinstance(ids, list) else "")
    return 0


def kill(state: Path, args: list[str], _behavior: Behavior) -> int:
    """``docker kill``: remember what was killed."""
    (state / "killed").write_text(" ".join(args[1:]))
    return 0


def build(_state: Path, _args: list[str], behavior: Behavior) -> int:
    """``docker build``."""
    print("Step 1/1 : FROM scratch")
    return int(number(behavior, "build_exit"))


def pull(_state: Path, _args: list[str], behavior: Behavior) -> int:
    """``docker pull``."""
    print("sha256:abc")
    return int(number(behavior, "pull_exit"))


def image(_state: Path, _args: list[str], behavior: Behavior) -> int:
    """``docker image inspect``."""
    return int(number(behavior, "image_exit"))


def network(_state: Path, _args: list[str], behavior: Behavior) -> int:
    """``docker network inspect``."""
    return int(number(behavior, "network_exit"))


def rmi(_state: Path, _args: list[str], behavior: Behavior) -> int:
    """``docker rmi``."""
    if behavior.get("rmi_missing"):
        print("Error: No such image: x", file=sys.stderr)
        return 1
    return 0


COMMANDS: dict[str, Callable[[Path, list[str], Behavior], int]] = {
    "run": run,
    "inspect": inspect,
    "ps": ps,
    "kill": kill,
    "build": build,
    "pull": pull,
    "image": image,
    "network": network,
    "rmi": rmi,
}


def main() -> int:
    """Record the call and dispatch on the docker subcommand."""
    state = Path(sys.argv[1])
    args = sys.argv[2:]
    behavior_file = state / "behavior.json"
    behavior: Behavior = json.loads(behavior_file.read_text()) if behavior_file.exists() else {}
    record = {"args": args, "env": {k: os.environ[k] for k in RECORDED_ENV if k in os.environ}}
    with (state / "calls.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    handler = COMMANDS.get(args[0])
    return handler(state, args, behavior) if handler else 0


if __name__ == "__main__":
    sys.exit(main())
