"""Pure construction of docker CLI argument lists. Nothing here runs a process.

Secret values never appear in an argument list: container environments go to an env file
and build argument values are taken from the environment of the docker client process.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from backend.adapters.container_runtime import BuildSpec, RunSpec
from backend.adapters.errors import ContainerRuntimeError

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9._-]*")
# Names of variables that change how the docker client itself behaves. A build argument
# with such a name would be a way to run code in the worker.
_RESERVED_EXACT = frozenset(
    {"PATH", "HOME", "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG", "IFS", "PWD", "TZ"}
    | {"GODEBUG", "GOGC", "GOMAXPROCS", "GOMEMLIMIT", "SYSTEMROOT"}
)
_RESERVED_PREFIXES = ("LD_", "DOCKER_", "BUILDKIT_", "XDG_", "SSH_", "GIT_", "LC_", "SSL_")
_PROXY_NAMES = frozenset({"http_proxy", "https_proxy", "no_proxy", "all_proxy"})


HARDENED_ARGS = [
    "--read-only",
    "--cap-drop",
    "ALL",
    "--pids-limit",
    "128",
    "--tmpfs",
    "/tmp:rw,noexec,nosuid,size=16m",  # noqa: S108 - inside the container
]


def check_env_name(name: str) -> None:
    """Reject names that cannot be written to an env file."""
    if _NAME.fullmatch(name) is None:
        raise ContainerRuntimeError(f"invalid environment variable name: {name!r}")


def env_file_text(env: Mapping[str, str]) -> str:
    """Render ``NAME=value`` lines; values containing a line break cannot be represented."""
    lines = []
    for name, value in env.items():
        check_env_name(name)
        if "\n" in value or "\r" in value or "\0" in value:
            raise ContainerRuntimeError(
                f"the value of {name} contains a line break or NUL, which an env file cannot hold"
            )
        lines.append(f"{name}={value}")
    return "\n".join(lines) + ("\n" if lines else "")


def check_build_arg_name(name: str) -> None:
    """Reject build argument names that would alter the docker client's own behaviour."""
    check_env_name(name)
    upper = name.upper()
    if upper in _RESERVED_EXACT or upper.startswith(_RESERVED_PREFIXES) or name in _PROXY_NAMES:
        raise ContainerRuntimeError(f"build argument name {name!r} is reserved")


def _label_args(labels: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in labels.items():
        if _LABEL_KEY.fullmatch(key) is None or "\n" in value:
            raise ContainerRuntimeError(f"invalid label: {key!r}")
        args += ["--label", f"{key}={value}"]
    return args


def build_argv(binary: str, spec: BuildSpec, labels: Mapping[str, str]) -> list[str]:
    """``docker build`` arguments; build argument values are passed by name only."""
    argv = [binary, "build", "--file", str(spec.dockerfile), "--tag", spec.tag]
    argv += _label_args({**spec.labels, **labels})
    if spec.target:
        argv += ["--target", spec.target]
    if spec.memory:
        argv += ["--memory", spec.memory]
    for name in spec.build_args:
        check_build_arg_name(name)
        argv += ["--build-arg", name]
    return [*argv, "--", str(spec.context)]


def run_argv(
    binary: str, spec: RunSpec, labels: Mapping[str, str], env_file: str | None
) -> list[str]:
    """``docker run`` arguments for a container that is removed when it exits."""
    argv = [binary, "run", "--rm", "--name", spec.name, "--security-opt", "no-new-privileges"]
    argv += _label_args({**spec.labels, **labels})
    if env_file is not None:
        argv += ["--env-file", env_file]
    if spec.workdir:
        argv += ["--workdir", spec.workdir]
    for mount in spec.mounts:
        argv += ["--volume", _volume(mount.source.as_posix(), mount.target, mount.read_only)]
    if spec.network:
        argv += ["--network", spec.network]
    if spec.cpus is not None:
        argv += ["--cpus", f"{spec.cpus:g}"]
    if spec.memory:
        argv += ["--memory", spec.memory]
    argv += ["--cpu-shares", str(spec.cpu_shares)]
    if spec.user:
        argv += ["--user", spec.user]
    if spec.hardened:
        argv += HARDENED_ARGS
    if spec.entrypoint is not None:
        argv += ["--entrypoint", spec.entrypoint]
    return [*argv, spec.image, *spec.command]


def _volume(source: str, target: str, read_only: bool) -> str:
    if ":" in source or ":" in target or "," in source:
        raise ContainerRuntimeError(f"mount path is not supported: {source!r} -> {target!r}")
    return f"{source}:{target}" + (":ro" if read_only else "")
