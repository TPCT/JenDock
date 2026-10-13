import pytest

from shiftcodes.domain.errors import ValidationError
from shiftcodes.domain.hostnames import Hostname
from shiftcodes.domain.ingress import DnsRecordType, validate_dns_target, validate_port


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("app.example.com", "app.example.com"),
        ("  App.Example.COM. ", "app.example.com"),
        ("a-b.c-d.example.co.uk", "a-b.c-d.example.co.uk"),
        ("bücher.example.com", "xn--bcher-kva.example.com"),
        ("xn--bcher-kva.example.com", "xn--bcher-kva.example.com"),
        ("münchen.de", "xn--mnchen-3ya.de"),
        ("\uff25xample.com", "example.com"),  # full-width E is mapped by UTS #46
    ],
)
def test_parse_normalizes_to_ascii_lowercase(raw: str, expected: str) -> None:
    assert Hostname.parse(raw).value == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "example",  # single label
        "localhost",
        "app.localhost",
        "printer.local",
        "svc.internal",
        "1.2.3.4",
        "::1",
        "-bad.example.com",
        "bad-.example.com",
        "a..example.com",
        ".example.com",
        "exa mple.com",
        "app.example.com/path",
        "app.example.com:8080",
        "user@example.com",
        "app_name.example.com",  # underscore is not valid in a public hostname
        "app.example.123",  # numeric TLD
        "a" * 64 + ".example.com",  # label too long
        ("a" * 60 + ".") * 5 + "com",  # name too long
        "xn--.example.com",
        "app.example.com\nServerName evil.test",
        'app.example.com"',
        "app.exa\x00mple.com",
    ],
)
def test_parse_rejects_invalid_names(raw: str) -> None:
    with pytest.raises(ValidationError):
        Hostname.parse(raw)


def test_wildcards_are_rejected_unless_explicitly_allowed() -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        Hostname.parse("*.example.com")
    assert Hostname.parse("*.example.com", allow_wildcard=True).value == "*.example.com"
    assert Hostname.parse("*.example.com", allow_wildcard=True).is_wildcard


@pytest.mark.parametrize("raw", ["a.*.example.com", "*.*.example.com", "*example.com", "*.com"])
def test_wildcard_only_as_leftmost_label_with_a_zone(raw: str) -> None:
    with pytest.raises(ValidationError):
        Hostname.parse(raw, allow_wildcard=True)


def test_direct_construction_requires_canonical_form() -> None:
    assert str(Hostname("app.example.com")) == "app.example.com"
    with pytest.raises(ValidationError):
        Hostname("App.Example.com")
    with pytest.raises(ValidationError):
        Hostname("bücher.example.com")


def test_validate_port_bounds() -> None:
    assert validate_port(8080, "p") == 8080
    for bad in (0, -1, 65536):
        with pytest.raises(ValidationError):
            validate_port(bad, "p")
    with pytest.raises(ValidationError):
        validate_port(80, "p", minimum=1024)


def test_dns_targets_match_the_record_type() -> None:
    assert validate_dns_target(DnsRecordType.A, "203.0.113.7") == "203.0.113.7"
    assert validate_dns_target(DnsRecordType.A, None) is None
    assert validate_dns_target(DnsRecordType.AAAA, "2001:db8::1") == "2001:db8::1"
    assert validate_dns_target(DnsRecordType.CNAME, "Edge.Example.NET") == "edge.example.net"
    for record_type, target in [
        (DnsRecordType.A, "2001:db8::1"),
        (DnsRecordType.A, "example.com"),
        (DnsRecordType.AAAA, "203.0.113.7"),
        (DnsRecordType.CNAME, "203.0.113.7"),
        (DnsRecordType.CNAME, None),
    ]:
        with pytest.raises(ValidationError):
            validate_dns_target(record_type, target)
