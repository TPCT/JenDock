"""DockerCliRuntime against a fake docker executable."""

from __future__ import annotations

import itertools
import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.adapters.container_runtime import BuildSpec, Mount, RunSpec
from backend.adapters.docker_cli import DockerCliRuntime
from backend.adapters.errors import ContainerRuntimeError
from tests.docker_support import session_labels

FAKE = Path(__file__).parent / "fake_docker.py"
SECRET = "very-secret-value-42"


class Fixture:
    """A fake docker binary, its state directory and a runtime that uses it."""

    def __init__(self, root: Path) -> None:
        self.state = root / "state"
        self.state.mkdir()
        self.private = root / "private"
        self.binary = root / "docker"
        self.binary.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} {self.state} "$@"\n')
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.runtime = DockerCliRuntime(
            binary=str(self.binary),
            docker_host="unix:///fake.sock",
            private_dir=self.private,
            extra_labels=session_labels(),
        )

    def behave(self, **values: object) -> None:
        (self.state / "behavior.json").write_text(json.dumps(values))

    def calls(self, command: str | None = None) -> list[dict[str, object]]:
        lines = (self.state / "calls.jsonl").read_text().splitlines()
        records = [json.loads(line) for line in lines]
        return [r for r in records if command is None or r["args"][0] == command]

    def run_args(self) -> list[str]:
        args = self.calls("run")[0]["args"]
        assert isinstance(args, list)
        return [str(a) for a in args]


@pytest.fixture
def fake(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _spec(**kwargs: object) -> RunSpec:
    base: dict[str, object] = {
        "image": "alpine:3.20",
        "name": "sc-test",
        "command": ("-c", "echo hi"),
    }
    base.update(kwargs)
    return RunSpec(**base)  # type: ignore[arg-type]


def test_run_builds_the_expected_argv_and_streams_lines(fake: Fixture, tmp_path: Path) -> None:
    fake.behave(lines=["one", "two"], exit=3)
    lines: list[str] = []
    spec = _spec(
        entrypoint="sh",
        workdir="/workspace/app",
        mounts=(Mount(tmp_path / "ws", "/workspace"),),
        network="proj_default",
        labels={"jendock.run_id": "7", "jendock.step_id": "9"},
        cpus=1.5,
        memory="512m",
        user="1000:1000",
    )
    result = fake.runtime.run_container(spec, lines.append)
    assert result.exit_code == 3
    assert not result.ok
    assert lines == ["one", "two", "to stderr"]  # stdout and stderr merged, in order
    args = fake.run_args()
    assert args[:6] == ["run", "--rm", "--name", "sc-test", "--security-opt", "no-new-privileges"]
    assert args[-4:] == ["alpine:3.20", "-c", "echo hi"] or args[-3:] == [
        "alpine:3.20",
        "-c",
        "echo hi",
    ]
    pairs = list(itertools.pairwise(args))
    assert ("--label", "jendock.run_id=7") in pairs
    assert ("--label", "jendock.test=1") in pairs  # extra labels on every container
    assert ("--workdir", "/workspace/app") in pairs
    assert ("--volume", f"{tmp_path / 'ws'}:/workspace") in pairs
    assert ("--network", "proj_default") in pairs
    assert ("--cpus", "1.5") in pairs
    assert ("--memory", "512m") in pairs
    assert ("--cpu-shares", "512") in pairs
    assert ("--user", "1000:1000") in pairs
    assert ("--entrypoint", "sh") in pairs
    assert fake.calls("run")[0]["env"]["DOCKER_HOST"] == "unix:///fake.sock"  # type: ignore[index]


def test_environment_goes_through_a_private_env_file_never_argv(fake: Fixture) -> None:
    fake.behave(sleep=1.2)
    env = {"TOKEN": SECRET, "PLAIN": "value"}
    fake.runtime.run_container(_spec(env=env), lambda _line: None)
    args = fake.run_args()
    assert SECRET not in " ".join(args)
    assert "TOKEN" not in " ".join(args)
    info = json.loads((fake.state / "run_info.json").read_text())
    assert info["env_file_text"] == f"TOKEN={SECRET}\nPLAIN=value\n"
    assert info["env_file_mode"] == 0o600
    assert fake.private.stat().st_mode & 0o777 == 0o700
    # deleted once the container existed (long before the process exited), and gone afterwards
    assert (fake.state / "env_exists_late").read_text() == "False"
    assert list(fake.private.iterdir()) == []


def test_env_file_is_removed_even_when_the_process_fails_to_start(tmp_path: Path) -> None:
    runtime = DockerCliRuntime(binary=str(tmp_path / "missing"), private_dir=tmp_path / "p")
    with pytest.raises(ContainerRuntimeError, match="not usable"):
        runtime.run_container(_spec(env={"A": "b"}), lambda _line: None)
    assert list((tmp_path / "p").iterdir()) == []


def test_env_values_with_line_breaks_are_refused(fake: Fixture) -> None:
    with pytest.raises(ContainerRuntimeError, match="line break"):
        fake.runtime.run_container(_spec(env={"KEY": "a\nb"}), lambda _line: None)
    assert not (fake.state / "calls.jsonl").exists()
    assert not fake.private.exists() or list(fake.private.iterdir()) == []


def test_no_env_file_without_environment(fake: Fixture) -> None:
    fake.runtime.run_container(_spec(), lambda _line: None)
    assert "--env-file" not in fake.run_args()


def test_timeout_kills_the_named_container(fake: Fixture) -> None:
    fake.behave(sleep=30)
    started = time.monotonic()
    result = fake.runtime.run_container(_spec(), lambda _line: None, timeout=0.5)
    assert time.monotonic() - started < 15
    assert result.timed_out
    assert not result.ok
    assert (fake.state / "killed").read_text() == "sc-test"


def test_cancel_signal_kills_the_container(fake: Fixture) -> None:
    fake.behave(sleep=30)
    cancel = threading.Event()
    threading.Timer(0.4, cancel.set).start()
    started = time.monotonic()
    result = fake.runtime.run_container(_spec(), lambda _line: None, cancel=cancel)
    assert time.monotonic() - started < 15
    assert result.cancelled
    assert (fake.state / "killed").read_text() == "sc-test"


def test_kill_by_label_lists_then_kills(fake: Fixture) -> None:
    fake.behave(ps=["aaa", "bbb"])
    assert fake.runtime.kill_by_label("jendock.run_id=5") == 2
    ps_args = fake.calls("ps")[0]["args"]
    assert "label=jendock.run_id=5" in ps_args  # type: ignore[operator]
    assert (fake.state / "killed").read_text() == "aaa bbb"


def test_kill_by_label_without_matches_does_not_call_kill(fake: Fixture) -> None:
    assert fake.runtime.kill_by_label("jendock.run_id=5") == 0
    assert fake.calls("kill") == []


def test_build_passes_argument_values_by_environment_only(fake: Fixture, tmp_path: Path) -> None:
    spec = BuildSpec(
        context=tmp_path,
        dockerfile=tmp_path / "Dockerfile",
        tag="sc/x:1",
        target="prod",
        build_args={"MODE": "fast", "SECRET_ARG": SECRET},
        labels={"jendock.run_id": "1"},
        memory="1g",
    )
    lines: list[str] = []
    assert fake.runtime.build_image(spec, lines.append).ok
    record = fake.calls("build")[0]
    args = [str(a) for a in record["args"]]  # type: ignore[attr-defined]
    assert SECRET not in " ".join(args)
    assert "fast" not in args
    pairs = list(itertools.pairwise(args))
    assert ("--build-arg", "MODE") in pairs
    assert ("--build-arg", "SECRET_ARG") in pairs
    assert ("--target", "prod") in pairs
    assert ("--memory", "1g") in pairs
    assert ("--label", "jendock.test=1") in pairs
    assert args[-2:] == ["--", str(tmp_path)]
    assert record["env"]["SECRET_ARG"] == SECRET  # type: ignore[index]
    assert lines == ["Step 1/1 : FROM scratch"]


@pytest.mark.parametrize(
    "name", ["PATH", "LD_PRELOAD", "DOCKER_CONFIG", "HOME", "http_proxy", "bad-name"]
)
def test_dangerous_build_argument_names_are_refused(
    fake: Fixture, tmp_path: Path, name: str
) -> None:
    spec = BuildSpec(tmp_path, tmp_path / "Dockerfile", "sc/x:1", build_args={name: "v"})
    with pytest.raises(ContainerRuntimeError):
        fake.runtime.build_image(spec, lambda _line: None)
    assert not (fake.state / "calls.jsonl").exists()  # nothing was started


def test_pull_image_exists_network_exists_and_remove(fake: Fixture) -> None:
    assert fake.runtime.pull_image("node:22", lambda _line: None).ok
    assert fake.calls("pull")[0]["args"] == ["pull", "--quiet", "node:22"]
    assert fake.runtime.image_exists("node:22")
    fake.behave(image_exit=1, network_exit=1)
    assert not fake.runtime.image_exists("nope:1")
    assert not fake.runtime.network_exists("nope_default")
    fake.runtime.remove_image("x:1")
    fake.behave(rmi_missing=True)
    fake.runtime.remove_image("gone:1")  # a missing image is not an error
