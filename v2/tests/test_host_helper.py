import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shiftcodes.hosthelper.errors import HelperError, InvalidRequestError
from shiftcodes.hosthelper.service import HelperService
from shiftcodes.hosthelper.vhost import VhostPaths, VhostSpec, render_vhost
from tests.host_helper_support import ScriptedApache, make_service, vhost_params
from tests.ingress_support import make_certificate

HOST = "app.example.com"


@pytest.fixture
def helper(tmp_path: Path) -> tuple[HelperService, ScriptedApache, Path]:
    service, apache, config = make_service(tmp_path)
    return service, apache, config.vhost_dir


def put(service: HelperService, **overrides: object) -> dict[str, object]:
    return service.execute("vhost.put", vhost_params(**overrides))  # type: ignore[arg-type]


# --- writing, atomicity, reload order ----------------------------------------------------


def test_put_writes_the_rendered_file_tests_then_reloads(
    helper: tuple[HelperService, ScriptedApache, Path], tmp_path: Path
) -> None:
    service, apache, vhosts = helper
    assert put(service) == {"changed": True}
    path = vhosts / f"sc-{HOST}.conf"
    expected = render_vhost(
        VhostSpec(HOST, 20001, False, True), VhostPaths(tmp_path / "certs", tmp_path / "webroot")
    )
    assert path.read_text() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert apache.events == ["configtest", "reload"]
    # The new file was already in place (atomically renamed) when Apache's config was tested.
    assert apache.seen_at_configtest[0][path.name] == expected
    assert [p.name for p in vhosts.iterdir()] == [path.name]  # no temp files left behind


def test_put_is_idempotent_and_does_not_reload_when_nothing_changed(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, _ = helper
    put(service)
    apache.events.clear()
    assert put(service) == {"changed": False}
    assert apache.events == []


def test_configtest_failure_reverts_an_updated_file_and_never_reloads(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, vhosts = helper
    put(service, port=20001)
    path = vhosts / f"sc-{HOST}.conf"
    before = path.read_text()
    apache.events.clear()
    apache.configtest_ok = False
    with pytest.raises(HelperError) as raised:
        put(service, port=20002)
    assert raised.value.code == "configtest_failed"
    assert "Syntax error" in raised.value.message
    assert path.read_text() == before
    assert apache.events == ["configtest"]
    assert [p.name for p in vhosts.iterdir()] == [path.name]


def test_configtest_failure_removes_a_file_that_did_not_exist_before(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, vhosts = helper
    apache.configtest_ok = False
    with pytest.raises(HelperError):
        put(service)
    assert list(vhosts.iterdir()) == []


def test_reload_failure_reverts_the_file_and_reloads_the_old_state_again(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, vhosts = helper
    put(service, port=20001)
    path = vhosts / f"sc-{HOST}.conf"
    before = path.read_text()
    apache.events.clear()
    apache.reload_ok = False
    with pytest.raises(HelperError) as raised:
        put(service, port=20002)
    assert raised.value.code == "reload_failed"
    assert path.read_text() == before
    assert apache.events == ["configtest", "reload", "reload"]


def test_remove_deletes_the_managed_file_and_reverts_on_failure(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, vhosts = helper
    put(service)
    path = vhosts / f"sc-{HOST}.conf"
    before = path.read_text()
    apache.configtest_ok = False
    with pytest.raises(HelperError):
        service.execute("vhost.remove", {"hostname": HOST})
    assert path.read_text() == before
    apache.configtest_ok = True
    assert service.execute("vhost.remove", {"hostname": HOST}) == {"removed": True}
    assert not path.exists()
    assert service.execute("vhost.remove", {"hostname": HOST}) == {"removed": False}


def test_a_file_without_the_marker_is_never_overwritten_or_removed(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, _apache, vhosts = helper
    foreign = vhosts / f"sc-{HOST}.conf"
    foreign.write_text("<VirtualHost *:80>\n# hand written\n</VirtualHost>\n")
    calls: list[Callable[[], object]] = [
        lambda: put(service),
        lambda: service.execute("vhost.remove", {"hostname": HOST}),
        lambda: service.execute("vhost.get", {"hostname": HOST}),
    ]
    for call in calls:
        with pytest.raises(HelperError) as raised:
            call()
        assert raised.value.code == "foreign_file"
    assert "hand written" in foreign.read_text()


def test_vhost_render_is_a_dry_run_and_get_list_report_state(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, vhosts = helper
    preview = service.execute("vhost.render", vhost_params())
    assert preview["current"] is None
    assert "ProxyPass / http://127.0.0.1:20001/" in str(preview["proposed"])
    assert list(vhosts.iterdir()) == [] and apache.events == []
    put(service)
    put(service, hostname="b.example.org", port=20002)
    assert service.execute("vhost.get", {"hostname": HOST})["exists"] is True
    assert service.execute("vhost.get", {"hostname": "nope.example.com"}) == {
        "exists": False,
        "content": None,
    }
    assert service.execute("vhost.list", {}) == {"hostnames": [HOST, "b.example.org"]}


def test_vhost_count_is_limited(tmp_path: Path) -> None:
    service, _apache, _config = make_service(tmp_path, max_vhosts=1)
    put(service)
    with pytest.raises(HelperError) as raised:
        put(service, hostname="b.example.com")
    assert raised.value.code == "limit_reached"
    put(service)  # updating an existing one is still fine


# --- validation: traversal, injection, allowlists ------------------------------------------

BAD_HOSTNAMES = [
    "../etc/passwd",
    "..",
    "a/../../b.example.com",
    "sub/app.example.com",
    "app.example.com/../x",
    "app.example.com\nInclude /etc/passwd",
    "app.example.com\r\nServerName evil.example.com",
    "app.example.com;rm",
    'app".example.com',
    "app example.com",
    "app.example.com ",
    "app.example.com#",
    "app.example.com$(id)",
    "<VirtualHost>.example.com",
    "%2e%2e.example.com",
    "APP.example.com",  # must be canonical lowercase already
    "bücher.example.com",  # must be an A-label already
    "app.evil.net",  # outside the allowed zones
    "example.com.evil.net",
    "notexample.com",  # suffix match must be on a label boundary
    "*.example.com",  # wildcards disabled in this config
    "",
    None,
    42,
    ["app.example.com"],
]


@pytest.mark.parametrize("hostname", BAD_HOSTNAMES)
def test_hostnames_with_traversal_injection_or_outside_zones_are_rejected(
    helper: tuple[HelperService, ScriptedApache, Path], hostname: object
) -> None:
    service, apache, vhosts = helper
    for operation in ("vhost.put", "vhost.render"):
        with pytest.raises(InvalidRequestError):
            service.execute(operation, vhost_params(hostname=hostname))  # type: ignore[arg-type]
    for operation in ("vhost.remove", "vhost.get", "cert.info", "cert.remove"):
        with pytest.raises(InvalidRequestError):
            service.execute(operation, {"hostname": hostname})
    assert list(vhosts.iterdir()) == [] and apache.events == []
    assert not (vhosts.parent / "etc").exists()


@pytest.mark.parametrize("port", [0, 1, 22, 80, 443, 1023, 19999, 30000, 65535, 65536, -1])
def test_ports_outside_the_allowlist_are_rejected(
    helper: tuple[HelperService, ScriptedApache, Path], port: int
) -> None:
    service, apache, _ = helper
    with pytest.raises(InvalidRequestError):
        put(service, port=port)
    with pytest.raises(InvalidRequestError):
        service.execute("backend.check", {"port": port})
    assert apache.events == []


@pytest.mark.parametrize("port", ["20001", 20001.0, True, None, [20001]])
def test_ports_must_be_integers(
    helper: tuple[HelperService, ScriptedApache, Path], port: object
) -> None:
    service, _apache, _ = helper
    with pytest.raises(InvalidRequestError):
        service.execute("vhost.put", vhost_params(port=port))  # type: ignore[arg-type]


def test_unknown_operations_extra_params_and_wrong_types_are_rejected(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, _ = helper
    for operation in ("shell", "vhost.put ", "VHOST.PUT", "apache.exec", "", None, 7):
        with pytest.raises(InvalidRequestError, match="unknown operation"):
            service.execute(operation, {})
    with pytest.raises(InvalidRequestError, match="unexpected parameters"):
        put(service, content="<VirtualHost>")  # no free-form configuration is accepted
    with pytest.raises(InvalidRequestError, match="unexpected parameters"):
        service.execute("vhost.remove", {"hostname": HOST, "path": "/etc/passwd"})
    with pytest.raises(InvalidRequestError, match="unexpected parameters"):
        service.execute("ping", {"x": 1})
    with pytest.raises(InvalidRequestError, match="tls"):
        put(service, tls="yes")
    with pytest.raises(InvalidRequestError, match="params"):
        service.execute("vhost.put", ["hostname"])
    assert apache.events == []


def test_wildcards_work_only_when_the_administrator_enabled_them(tmp_path: Path) -> None:
    service, _apache, config = make_service(tmp_path, allow_wildcards=True)
    assert put(service, hostname="*.example.com") == {"changed": True}
    assert (config.vhost_dir / "sc-_wildcard_.example.com.conf").is_file()
    with pytest.raises(InvalidRequestError):
        put(service, hostname="*.evil.net")
    with pytest.raises(InvalidRequestError):
        put(service, hostname="a.*.example.com")


# --- ACME challenge files -------------------------------------------------------------------


def test_challenge_files_are_written_below_the_webroot_only(
    helper: tuple[HelperService, ScriptedApache, Path], tmp_path: Path
) -> None:
    service, apache, _ = helper
    token = "tok_" + "A" * 40
    content = token + "." + "B" * 43
    service.execute("challenge.put", {"token": token, "content": content})
    written = tmp_path / "webroot" / ".well-known" / "acme-challenge" / token
    assert written.read_text() == content
    assert apache.events == []  # no reload for challenge files
    assert service.execute("challenge.remove", {"token": token}) == {"removed": True}
    assert not written.exists()


@pytest.mark.parametrize(
    "token",
    ["../../etc/passwd", "a" * 5, "ok_token_ok_token_ok/../x", "tok\nen" + "a" * 20, "", None],
)
def test_challenge_tokens_cannot_traverse_or_inject(
    helper: tuple[HelperService, ScriptedApache, Path], token: object
) -> None:
    service, _apache, _ = helper
    with pytest.raises(InvalidRequestError):
        service.execute("challenge.put", {"token": token, "content": "x" * 40})
    with pytest.raises(InvalidRequestError):
        service.execute("challenge.remove", {"token": token})


def test_challenge_content_must_be_a_key_authorization(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, _apache, _ = helper
    for content in ("short", "x" * 40 + "\n<script>", "x" * 300, None):
        with pytest.raises(InvalidRequestError):
            service.execute("challenge.put", {"token": "t" * 30, "content": content})


# --- certificates ----------------------------------------------------------------------------


def valid_pair(hostname: str = HOST, **kwargs: object) -> tuple[str, str]:
    now = datetime.now(UTC)
    return make_certificate(hostname, now - timedelta(days=1), now + timedelta(days=89), **kwargs)  # type: ignore[arg-type]


def install(
    service: HelperService, chain: str, key: str, hostname: str = HOST
) -> dict[str, object]:
    return service.execute(
        "cert.install", {"hostname": hostname, "fullchain_pem": chain, "private_key_pem": key}
    )


def test_cert_install_writes_private_files_and_reloads_only_when_a_vhost_uses_tls(
    helper: tuple[HelperService, ScriptedApache, Path], tmp_path: Path
) -> None:
    service, apache, _ = helper
    chain, key = valid_pair()
    result = install(service, chain, key)
    directory = tmp_path / "certs" / HOST
    assert (directory / "fullchain.pem").read_text() == chain
    assert (directory / "privkey.pem").read_text() == key
    assert stat.S_IMODE((directory / "privkey.pem").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert "not_after" in result
    assert apache.events == []  # nothing serves the certificate yet
    put(service, tls=True)
    apache.events.clear()
    install(service, *valid_pair())
    assert apache.events == ["configtest", "reload"]
    info = service.execute("cert.info", {"hostname": HOST})
    assert info["installed"] is True


def test_cert_install_rolls_back_to_the_previous_certificate_when_configtest_fails(
    helper: tuple[HelperService, ScriptedApache, Path], tmp_path: Path
) -> None:
    service, apache, _ = helper
    old_chain, old_key = valid_pair()
    install(service, old_chain, old_key)
    put(service, tls=True)
    apache.configtest_ok = False
    with pytest.raises(HelperError):
        install(service, *valid_pair())
    directory = tmp_path / "certs" / HOST
    assert (directory / "fullchain.pem").read_text() == old_chain
    assert (directory / "privkey.pem").read_text() == old_key


def test_tls_vhost_needs_an_installed_certificate(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, apache, vhosts = helper
    with pytest.raises(HelperError) as raised:
        put(service, tls=True)
    assert raised.value.code == "certificate_missing"
    assert list(vhosts.iterdir()) == [] and apache.events == []


def test_cert_install_rejects_bad_material(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, _apache, _ = helper
    chain, key = valid_pair()
    other_chain, other_key = valid_pair()
    now = datetime.now(UTC)
    expired_chain, expired_key = make_certificate(
        HOST, now - timedelta(days=100), now - timedelta(days=10)
    )
    wrong_name_chain, wrong_name_key = valid_pair("other.example.com")
    for chain_pem, key_pem, hostname in [
        (chain, other_key, HOST),  # key does not match
        (other_chain, key, HOST),
        (expired_chain, expired_key, HOST),
        (wrong_name_chain, wrong_name_key, HOST),  # does not cover the hostname
        ("not a pem", key, HOST),
        (chain, "not a key", HOST),
        (chain, key.replace("PRIVATE", "PUBLIC"), HOST),
        ("", "", HOST),
        (chain, key, "../evil.example.com"),
    ]:
        with pytest.raises(InvalidRequestError):
            install(service, chain_pem, key_pem, hostname)
    with pytest.raises(InvalidRequestError):
        service.execute(
            "cert.install", {"hostname": HOST, "fullchain_pem": 1, "private_key_pem": 2}
        )
    with pytest.raises(InvalidRequestError, match="unexpected"):
        service.execute(
            "cert.install",
            {"hostname": HOST, "fullchain_pem": chain, "private_key_pem": key, "path": "/etc"},
        )


def test_a_wildcard_certificate_covers_a_subdomain(
    helper: tuple[HelperService, ScriptedApache, Path],
) -> None:
    service, _apache, _ = helper
    chain, key = valid_pair(san=["*.example.com"])
    install(service, chain, key)  # covered by the wildcard SAN
    deep_chain, deep_key = valid_pair(san=["other.example.com"])
    with pytest.raises(InvalidRequestError):
        install(service, deep_chain, deep_key)


def test_cert_remove_refuses_while_the_vhost_still_serves_tls(
    helper: tuple[HelperService, ScriptedApache, Path], tmp_path: Path
) -> None:
    service, _apache, _ = helper
    install(service, *valid_pair())
    put(service, tls=True)
    with pytest.raises(HelperError) as raised:
        service.execute("cert.remove", {"hostname": HOST})
    assert raised.value.code == "in_use"
    put(service, tls=False)
    assert service.execute("cert.remove", {"hostname": HOST}) == {"removed": True}
    assert not (tmp_path / "certs" / HOST).exists()
    assert service.execute("cert.info", {"hostname": HOST}) == {
        "installed": False,
        "not_after": None,
    }
