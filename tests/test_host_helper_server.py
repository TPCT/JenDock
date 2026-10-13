import dataclasses
import grp
import json
import os
import socket
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.adapters.host_helper_client import HostHelperClient, HostHelperError
from backend.hosthelper.config import ConfigError, load_config, parse_config
from backend.hosthelper.server import MAX_REQUEST_BYTES, HelperServer, handle_line
from backend.hosthelper.service import HelperService
from tests.host_helper_support import ScriptedApache, make_config, make_service

HOST = "app.example.com"


@pytest.fixture
def server(
    socket_dir: Path, tmp_path: Path
) -> Iterator[tuple[HostHelperClient, ScriptedApache, Path]]:
    config = make_config(tmp_path, socket_path=socket_dir / "h.sock")
    apache = ScriptedApache()
    srv = HelperServer(config, HelperService(config, apache))
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield HostHelperClient(config.socket_path, timeout=5.0), apache, config.vhost_dir
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def test_client_and_server_round_trip(
    server: tuple[HostHelperClient, ScriptedApache, Path],
) -> None:
    client, apache, vhosts = server
    client.ping()
    proposal = client.render_vhost(HOST, 20001, tls=False)
    assert proposal.current is None and "ServerName app.example.com" in proposal.proposed
    assert client.put_vhost(HOST, 20001, tls=False) is True
    assert client.put_vhost(HOST, 20001, tls=False) is False
    assert client.get_vhost(HOST) == (vhosts / f"jendock-{HOST}.conf").read_text()
    assert apache.events == ["configtest", "reload"]
    assert client.remove_vhost(HOST) is True
    assert client.get_vhost(HOST) is None


def test_helper_errors_arrive_with_their_code(
    server: tuple[HostHelperClient, ScriptedApache, Path],
) -> None:
    client, apache, vhosts = server
    apache.configtest_ok = False
    with pytest.raises(HostHelperError) as raised:
        client.put_vhost(HOST, 20001, tls=False)
    assert raised.value.code == "configtest_failed"
    assert list(vhosts.iterdir()) == []
    with pytest.raises(HostHelperError) as invalid:
        client.put_vhost("../etc/passwd", 20001, tls=False)
    assert invalid.value.code == "invalid_request"


def test_the_socket_is_not_accessible_to_others(
    server: tuple[HostHelperClient, ScriptedApache, Path], socket_dir: Path
) -> None:
    mode = stat.S_IMODE((socket_dir / "h.sock").stat().st_mode)
    assert mode == 0o660


def test_the_socket_group_also_owns_the_socket_directory(socket_dir: Path, tmp_path: Path) -> None:
    """systemd creates the runtime directory root:root; the group must reach the socket."""
    current = socket_dir.stat().st_gid
    other = next((gid for gid in os.getgroups() if gid != current), None)
    if other is None:
        pytest.skip("the test user belongs to a single group")
    config = dataclasses.replace(
        make_config(tmp_path, socket_path=socket_dir / "h.sock"),
        socket_group=grp.getgrgid(other).gr_name,
    )
    srv = HelperServer(config, HelperService(config, ScriptedApache()))
    try:
        assert socket_dir.stat().st_gid == other
        assert (socket_dir / "h.sock").stat().st_gid == other
    finally:
        srv.server_close()


def test_a_peer_with_a_foreign_uid_is_refused(socket_dir: Path, tmp_path: Path) -> None:
    config = make_config(
        tmp_path, socket_path=socket_dir / "h.sock", allowed_uids=frozenset({os.geteuid() + 1})
    )
    srv = HelperServer(config, HelperService(config, ScriptedApache()))
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        with pytest.raises(HostHelperError) as raised:
            HostHelperClient(config.socket_path, timeout=5.0).ping()
        assert raised.value.code == "forbidden"
        assert list(config.vhost_dir.iterdir()) == []
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def _raw(socket_path: Path, payload: bytes) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(socket_path))
        connection.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "payload",
    [
        b"not json\n",
        b"[]\n",
        b'{"op": "ping"}\n',
        b'{"id": 1, "op": "ping"}\n',
        b'{"id": "x" , "op": "ping", "extra": 1}\n',
    ],
)
def test_malformed_requests_get_an_error_not_a_crash(
    server: tuple[HostHelperClient, ScriptedApache, Path], socket_dir: Path, payload: bytes
) -> None:
    response = _raw(socket_dir / "h.sock", payload)
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"  # type: ignore[index]
    server[0].ping()  # still serving


def test_oversized_requests_are_refused(
    server: tuple[HostHelperClient, ScriptedApache, Path], socket_dir: Path
) -> None:
    huge = b'{"id":"a","op":"ping","params":{"x":"' + b"a" * (MAX_REQUEST_BYTES + 10) + b'"}}\n'
    response = _raw(socket_dir / "h.sock", huge)
    assert response["error"]["code"] == "invalid_request"  # type: ignore[index]


def test_handle_line_hides_unexpected_exceptions(tmp_path: Path) -> None:
    service, _apache, _config = make_service(tmp_path)

    def explode(_params: object) -> dict[str, object]:
        raise RuntimeError("secret path /etc/shadow")

    service._operations["ping"] = explode
    response = handle_line(service, b'{"id": "1", "op": "ping", "params": {}}\n')
    assert response["error"] == {"code": "internal", "message": "internal helper error"}


def test_a_stale_socket_is_replaced_but_a_regular_file_is_not(
    socket_dir: Path, tmp_path: Path
) -> None:
    path = socket_dir / "h.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()  # leaves a dead socket file behind
    config = make_config(tmp_path, socket_path=path)
    HelperServer(config, HelperService(config, ScriptedApache())).server_close()
    path.write_text("not a socket")
    with pytest.raises(FileExistsError):
        HelperServer(config, HelperService(config, ScriptedApache()))


# --- configuration -----------------------------------------------------------------------------

VALID = {
    "socket_path": "/run/x/h.sock",
    "allowed_uids": [10001],
    "vhost_dir": "/etc/apache2/sc",
    "cert_dir": "/etc/jendock/certs",
    "webroot_dir": "/var/lib/jendock/webroot",
    "apachectl": ["/usr/sbin/apachectl"],
    "allowed_hostname_suffixes": ["example.com"],
    "allowed_upstream_ports": [[20000, 29999]],
}


def test_valid_config_parses() -> None:
    config = parse_config(VALID)
    assert config.port_allowed(20000) and config.port_allowed(29999)
    assert not config.port_allowed(19999)
    assert config.socket_mode == 0o660 and not config.allow_wildcards


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": 1},
        {"socket_path": "relative.sock"},
        {"vhost_dir": "/etc/../tmp"},
        {"allowed_uids": []},
        {"allowed_uids": [-1]},
        {"allowed_uids": ["root"]},
        {"apachectl": ["apachectl"]},
        {"apachectl": []},
        {"allowed_hostname_suffixes": ["Example.COM"]},
        {"allowed_hostname_suffixes": []},
        {"allowed_upstream_ports": [[80, 90]]},
        {"allowed_upstream_ports": [[30000, 20000]]},
        {"allowed_upstream_ports": []},
        {"socket_mode": "0666"},
        {"socket_mode": "0664"},
        {"socket_mode": "rw"},
        {"allow_wildcards": "yes"},
        {"max_vhosts": 0},
        {"command_timeout_seconds": 0},
    ],
)
def test_invalid_config_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_config({**VALID, **change})


def test_missing_required_keys_are_refused() -> None:
    incomplete = {k: v for k, v in VALID.items() if k != "vhost_dir"}
    with pytest.raises(ConfigError, match="vhost_dir"):
        parse_config(incomplete)
    with pytest.raises(ConfigError):
        parse_config([])


def test_config_file_must_not_be_writable_by_others(tmp_path: Path) -> None:
    path = tmp_path / "helper.json"
    path.write_text(json.dumps(VALID))
    path.chmod(0o644)
    assert load_config(path).allowed_uids == frozenset({10001})
    path.chmod(0o664)
    with pytest.raises(ConfigError, match="writable"):
        load_config(path)
    link = tmp_path / "link.json"
    path.chmod(0o600)
    link.symlink_to(path)
    with pytest.raises(ConfigError, match="symlink"):
        load_config(link)
    with pytest.raises(ConfigError, match="cannot"):
        load_config(tmp_path / "missing.json")
