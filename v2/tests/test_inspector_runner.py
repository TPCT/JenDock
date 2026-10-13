"""The helper-container runner, the wire framing, the helper's request handling and the network
guard, all without Docker."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from shiftcodes.adapters.container_runtime import ExecResult, LineSink, RunSpec
from shiftcodes.adapters.docker_args import run_argv
from shiftcodes.adapters.errors import ContainerRuntimeError
from shiftcodes.adapters.inspector_runner import HelperRequest, InspectorRunner
from shiftcodes.domain.errors import ConfigurationError, ConflictError
from shiftcodes.inspector import wire
from shiftcodes.inspector.helper import REQUEST_ENV, handle, parse_request
from shiftcodes.inspector.model import (
    ConnectionSpec,
    Engine,
    ErrorCode,
    InspectorError,
    TlsMode,
)
from shiftcodes.inspector.netguard import NetworkPolicy, resolve_address

PASSWORD = "p@ss'w\"ord;$(x)\n-end"
SPEC = ConnectionSpec(Engine.POSTGRESQL, "db", 5432, "app", "reader", PASSWORD, TlsMode.REQUIRE)
IPAM = json.dumps([{"Subnet": "172.30.0.0/16", "Gateway": "172.30.0.1"}])
Answer = Callable[[LineSink], ExecResult]


class FakeRuntime:
    """A docker stand-in: scripted ``network inspect`` and helper container output."""

    def __init__(self) -> None:
        self.image_present = True
        self.network_stdout = IPAM
        self.network_code = 0
        self.specs: list[RunSpec] = []
        self.timeouts: list[float | None] = []
        self.queries: list[list[str]] = []
        self.answer: Answer = lambda sink: _emit(sink, {"ok": True, "result": {"hello": "world"}})
        self.error: Exception | None = None

    def query(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.queries.append(args)
        return subprocess.CompletedProcess(args, self.network_code, self.network_stdout, "")

    def image_exists(self, image: str) -> bool:
        return self.image_present

    def run_container(
        self, spec: RunSpec, sink: LineSink, *, timeout: float | None = None
    ) -> ExecResult:
        self.specs.append(spec)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.answer(sink)


def _emit(sink: LineSink, payload: dict[str, object], exit_code: int = 0) -> ExecResult:
    for line in wire.encode(payload):
        sink(line)
    return ExecResult(exit_code)


def _request(operation: str = "test") -> HelperRequest:
    return HelperRequest(operation, {"schema": "public"}, SPEC)


# --- runner --------------------------------------------------------------------------------


def test_helper_container_is_hardened_and_attached_to_the_environment_network_only() -> None:
    runtime = FakeRuntime()
    result = InspectorRunner(runtime, "img:1").run("demo-dev_default", _request())
    assert result == {"hello": "world"}
    spec = runtime.specs[0]
    assert spec.image == "img:1"
    assert spec.network == "demo-dev_default"
    assert spec.hardened is True
    assert spec.user == "65534:65534"
    assert spec.memory and spec.cpus
    assert spec.labels == {"shiftcodes.inspector": "1"}
    assert spec.command == ("python", "-m", "shiftcodes.inspector.helper")
    assert runtime.timeouts == [30.0]
    assert runtime.queries == [
        ["network", "inspect", "--format", "{{json .IPAM.Config}}", "demo-dev_default"]
    ]


def test_password_travels_in_the_env_not_in_any_command_line() -> None:
    runtime = FakeRuntime()
    InspectorRunner(runtime, "img:1").run("net", _request())
    spec = runtime.specs[0]
    request = json.loads(spec.env[REQUEST_ENV])
    assert request["connection"]["password"] == PASSWORD
    assert request["network"] == {"subnets": ["172.30.0.0/16"], "blocked": ["172.30.0.1"]}
    argv = run_argv("docker", spec, {}, "/private/env-1")
    assert not any(PASSWORD in part or "reader" in part for part in argv)
    assert "--env-file" in argv
    assert not any(part.startswith(REQUEST_ENV) for part in argv)
    for flag in ("--read-only", "--cap-drop", "--pids-limit", "--security-opt", "--rm"):
        assert flag in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--network") + 1] == "net"


def test_each_run_gets_a_fresh_container_name() -> None:
    runtime = FakeRuntime()
    runner = InspectorRunner(runtime, "img:1")
    runner.run("net", _request())
    runner.run("net", _request())
    assert runtime.specs[0].name != runtime.specs[1].name
    assert runtime.specs[0].name.startswith("shiftcodes-inspect-")


def test_missing_image_is_a_configuration_error_with_the_build_hint() -> None:
    runtime = FakeRuntime()
    runtime.image_present = False
    with pytest.raises(ConfigurationError, match="build"):
        InspectorRunner(runtime, "img:1").run("net", _request())
    assert runtime.specs == []


def test_missing_network_asks_to_deploy_first() -> None:
    runtime = FakeRuntime()
    runtime.network_code = 1
    with pytest.raises(ConflictError, match="deploy"):
        InspectorRunner(runtime, "img:1").run("net", _request())
    assert runtime.specs == []


def test_helper_failure_answer_becomes_an_inspector_error() -> None:
    runtime = FakeRuntime()
    failure = {"ok": False, "error": {"code": "connect_failed", "message": "refused"}}
    runtime.answer = lambda sink: _emit(sink, failure)
    with pytest.raises(InspectorError) as raised:
        InspectorRunner(runtime, "img:1").run("net", _request())
    assert raised.value.code is ErrorCode.CONNECT_FAILED
    assert raised.value.message == "refused"


@pytest.mark.parametrize(
    "answer",
    [
        lambda _sink: ExecResult(1),
        lambda sink: (sink("Traceback: boom"), ExecResult(0))[1],
        lambda sink: _emit(sink, {"ok": True, "result": {}}, exit_code=3),
        lambda sink: (sink(wire.FRAME_PREFIX + "AAAA"), sink(wire.FRAME_END), ExecResult(0))[2],
    ],
)
def test_crash_or_garbage_is_a_generic_helper_failure(answer: Answer) -> None:
    runtime = FakeRuntime()
    runtime.answer = answer
    with pytest.raises(InspectorError) as raised:
        InspectorRunner(runtime, "img:1").run("net", _request())
    assert raised.value.code is ErrorCode.HELPER_FAILED
    assert "Traceback" not in raised.value.message


def test_wall_clock_timeout_and_docker_errors() -> None:
    runtime = FakeRuntime()
    runtime.answer = lambda _sink: ExecResult(-15, timed_out=True)
    with pytest.raises(InspectorError) as timed_out:
        InspectorRunner(runtime, "img:1").run("net", _request())
    assert timed_out.value.code is ErrorCode.TIMEOUT
    runtime.error = ContainerRuntimeError("docker is not usable")
    with pytest.raises(InspectorError) as broken:
        InspectorRunner(runtime, "img:1").run("net", _request())
    assert broken.value.code is ErrorCode.HELPER_FAILED


# --- wire framing --------------------------------------------------------------------------


def test_wire_round_trip_of_a_large_payload_and_noise_lines() -> None:
    payload: dict[str, object] = {"ok": True, "result": {"rows": [["é" * 100] * 50] * 200}}
    lines = wire.encode(payload)
    assert all(len(line) <= wire.CHUNK_CHARS + len(wire.FRAME_PREFIX) for line in lines)
    noisy = ["warning: something", *lines[:1], "more noise", *lines[1:]]
    assert wire.decode(noisy) == payload


def test_wire_rejects_incomplete_or_tampered_frames() -> None:
    lines = wire.encode({"ok": True, "result": {}})
    assert wire.decode(lines[:-1]) is None
    assert wire.decode([wire.FRAME_END]) is None
    assert wire.decode([wire.FRAME_PREFIX + "!!!not-base64", wire.FRAME_END]) is None
    assert wire.decode(["x" * 10]) is None


# --- helper request handling ---------------------------------------------------------------


def _raw(**changes: object) -> str:
    request: dict[str, object] = {
        "op": "tables",
        "args": {"schema": "public"},
        "connection": {
            "engine": "postgresql",
            "host": "db",
            "port": 5432,
            "database": "app",
            "username": "u",
            "password": PASSWORD,
            "tls": "disable",
        },
        "network": {"subnets": ["10.0.0.0/8"], "blocked": []},
    }
    request.update(changes)
    return json.dumps(request)


def test_parse_request_builds_the_connection_and_policy() -> None:
    operation, args, spec, policy = parse_request(_raw())
    assert (operation, args) == ("tables", {"schema": "public"})
    assert spec.engine is Engine.POSTGRESQL and spec.password == PASSWORD
    assert policy == NetworkPolicy(("10.0.0.0/8",), ())
    assert PASSWORD not in repr(spec)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        _raw(op=5),
        _raw(args=[]),
        _raw(network={"subnets": "10.0.0.0/8", "blocked": []}),
        _raw(connection={"engine": "oracle"}),
        _raw(connection={"engine": "mysql", "host": "db", "port": 0}),
    ],
)
def test_helper_answers_bad_requests_without_echoing_secrets(raw: str) -> None:
    answer = handle(raw)
    assert answer["ok"] is False
    assert isinstance(answer["error"], dict)
    assert answer["error"]["code"] == "bad_request"
    assert PASSWORD not in json.dumps(answer)


def test_helper_refuses_hosts_outside_the_environment_network() -> None:
    answer = handle(_raw(connection={**json.loads(_raw())["connection"], "host": "8.8.8.8"}))
    assert answer["ok"] is False
    assert isinstance(answer["error"], dict)
    assert answer["error"]["code"] == "network_denied"


def test_helper_rejects_unknown_operations() -> None:
    answer = handle(_raw(op="drop_everything"))
    assert answer["ok"] is False


# --- network guard -------------------------------------------------------------------------


def test_addresses_inside_the_subnet_pass_the_guard() -> None:
    policy = NetworkPolicy(("172.30.0.0/16",), ("172.30.0.1",))
    assert resolve_address("172.30.0.7", 5432, policy) == "172.30.0.7"


@pytest.mark.parametrize(
    "host", ["172.30.0.1", "8.8.8.8", "127.0.0.1", "10.0.0.5", "169.254.169.254"]
)
def test_gateway_and_foreign_addresses_are_denied(host: str) -> None:
    policy = NetworkPolicy(("172.30.0.0/16",), ("172.30.0.1",))
    with pytest.raises(InspectorError) as raised:
        resolve_address(host, 5432, policy)
    assert raised.value.code is ErrorCode.NETWORK_DENIED


def test_localhost_by_name_is_outside_the_network() -> None:
    with pytest.raises(InspectorError) as raised:
        resolve_address("localhost", 5432, NetworkPolicy(("172.30.0.0/16",), ()))
    assert raised.value.code is ErrorCode.NETWORK_DENIED


def test_policy_without_subnets_denies_everything() -> None:
    with pytest.raises(InspectorError) as raised:
        resolve_address("172.30.0.7", 5432, NetworkPolicy((), ()))
    assert raised.value.code is ErrorCode.NETWORK_DENIED


def test_unresolvable_host_is_a_connect_failure() -> None:
    with pytest.raises(InspectorError) as raised:
        resolve_address("no-such-host.invalid", 5432, NetworkPolicy(("172.30.0.0/16",), ()))
    assert raised.value.code is ErrorCode.CONNECT_FAILED
