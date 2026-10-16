import json

import httpx
import pytest

from backend.adapters.cloudflare import (
    CloudflareClient,
    CloudflareError,
    DnsDesired,
    _decode,
)
from backend.domain.ingress import MANAGED_TAG, DnsRecordType, PlanAction
from tests.ingress_support import HOST, ZONE, FakeCloudflare, fixture

TAG = f"{MANAGED_TAG} domain=1"


def desired(content: str = "203.0.113.7", *, proxied: bool = False, kind: str = "A") -> DnsDesired:
    return DnsDesired(DnsRecordType(kind), HOST, content, proxied, TAG)


def test_find_zone_walks_up_from_the_hostname() -> None:
    fake = FakeCloudflare()
    zone = fake.client().find_zone("a.b.example.com")
    assert (zone.id, zone.name) == ("023e105f4ecef8ad9ca31a8372d0c353", ZONE)


def test_find_zone_fails_when_the_token_cannot_see_a_zone() -> None:
    with pytest.raises(CloudflareError, match="no Cloudflare zone"):
        FakeCloudflare().client().find_zone("app.other.org")


def test_creates_a_missing_record_with_the_ownership_tag() -> None:
    fake = FakeCloudflare()
    dns = fake.dns()
    plan = dns.plan_upsert(desired())
    assert plan.action is PlanAction.CREATE
    assert fake.mutations() == []  # planning is a dry run: GET only
    record = dns.apply_upsert(plan)
    assert record is not None
    stored = fake.records[0]
    assert (stored["type"], stored["name"], stored["content"]) == ("A", HOST, "203.0.113.7")
    assert stored["comment"] == TAG
    assert stored["proxied"] is False
    assert stored["ttl"] == 1


def test_apply_is_idempotent() -> None:
    fake = FakeCloudflare()
    dns = fake.dns()
    dns.apply_upsert(dns.plan_upsert(desired()))
    mutations_after_first = len(fake.mutations())
    second = dns.plan_upsert(desired())
    assert second.action is PlanAction.NOOP
    dns.apply_upsert(second)
    assert len(fake.mutations()) == mutations_after_first
    assert len(fake.records) == 1


def test_updates_an_owned_record_when_content_or_proxy_flag_changes() -> None:
    fake = FakeCloudflare()
    existing = fake.add_record(type="A", name=HOST, content="198.51.100.1", comment=TAG)
    dns = fake.dns()
    plan = dns.plan_upsert(desired("203.0.113.7", proxied=True))
    assert plan.action is PlanAction.UPDATE
    assert plan.existing is not None
    assert plan.existing.content == "198.51.100.1"
    dns.apply_upsert(plan)
    assert fake.records == [existing]
    assert existing["content"] == "203.0.113.7"
    assert existing["proxied"] is True
    assert existing["comment"] == TAG


def test_a_foreign_record_is_a_conflict_and_is_never_touched() -> None:
    fake = FakeCloudflare()
    foreign = fake.add_record(type="A", name=HOST, content="192.0.2.99", comment="my own record")
    untagged = fake.add_record(type="A", name="other.example.com", content="192.0.2.1")
    before = json.dumps(fake.records, sort_keys=True)
    dns = fake.dns()
    plan = dns.plan_upsert(desired())
    assert plan.action is PlanAction.CONFLICT
    assert "not managed by Jendock" in plan.message
    assert plan.foreign == (dns.client.list_records(plan.zone.id, HOST)[0],)
    with pytest.raises(CloudflareError, match="not managed"):
        dns.apply_upsert(plan)
    assert json.dumps(fake.records, sort_keys=True) == before
    assert foreign in fake.records
    assert untagged in fake.records
    assert fake.mutations() == []


def test_a_foreign_cname_blocks_an_address_record_and_vice_versa() -> None:
    fake = FakeCloudflare()
    fake.add_record(type="CNAME", name=HOST, content="elsewhere.example.net", comment=None)
    assert fake.dns().plan_upsert(desired()).action is PlanAction.CONFLICT
    other = FakeCloudflare()
    other.add_record(type="A", name=HOST, content="192.0.2.1", comment=None)
    plan = other.dns().plan_upsert(desired("edge.example.net", kind="CNAME"))
    assert plan.action is PlanAction.CONFLICT


def test_a_foreign_aaaa_does_not_block_an_a_record() -> None:
    fake = FakeCloudflare()
    fake.add_record(type="AAAA", name=HOST, content="2001:db8::1", comment=None)
    dns = fake.dns()
    plan = dns.plan_upsert(desired())
    assert plan.action is PlanAction.CREATE
    dns.apply_upsert(plan)
    assert {r["type"] for r in fake.records} == {"A", "AAAA"}


def test_changing_the_record_type_replaces_only_the_owned_record() -> None:
    fake = FakeCloudflare()
    fake.add_record(type="A", name=HOST, content="198.51.100.1", comment=TAG)
    dns = fake.dns()
    plan = dns.plan_upsert(desired("edge.example.net", kind="CNAME"))
    assert plan.action is PlanAction.CREATE
    assert [r.record_type for r in plan.stale] == ["A"]
    dns.apply_upsert(plan)
    assert [(r["type"], r["content"]) for r in fake.records] == [("CNAME", "edge.example.net")]


def test_delete_removes_owned_records_and_leaves_foreign_ones() -> None:
    fake = FakeCloudflare()
    fake.add_record(type="A", name=HOST, content="198.51.100.1", comment=TAG)
    fake.add_record(type="AAAA", name=HOST, content="2001:db8::1", comment="hand made")
    dns = fake.dns()
    plan = dns.plan_delete(HOST)
    assert plan.action is PlanAction.DELETE
    assert [r.record_type for r in plan.stale] == ["A"]
    assert "left untouched" in plan.message
    assert dns.apply_delete(plan) == 1
    assert [r["type"] for r in fake.records] == ["AAAA"]
    assert dns.plan_delete(HOST).action is PlanAction.NOOP


def test_losing_a_creation_race_converges_without_duplicates() -> None:
    fake = FakeCloudflare()
    dns = fake.dns()
    plan = dns.plan_upsert(desired())
    # Someone else (this platform, concurrently) creates the record just before our POST.
    fake.add_record(type="A", name=HOST, content="203.0.113.7", comment=TAG)
    fake.fail_create_once_with = fixture("cloudflare", "error_exists.json")
    record = dns.apply_upsert(plan)
    assert record is not None
    assert len(fake.records) == 1


def test_a_rejected_token_gives_a_clear_error_without_the_token() -> None:
    fake = FakeCloudflare(token="cf-super-secret-token")
    fake.reject_auth = True
    with pytest.raises(CloudflareError) as raised:
        fake.client().find_zone(HOST)
    assert "token" in str(raised.value)
    assert "cf-super-secret-token" not in str(raised.value)


def test_user_token_verification_uses_only_the_user_endpoint() -> None:
    fake = FakeCloudflare()
    assert fake.client().verify_token() == "active"
    assert fake.requests == [("GET", "/client/v4/user/tokens/verify")]


def test_account_token_falls_back_after_user_verification_401() -> None:
    fake = FakeCloudflare()
    fake.account_token = True
    assert fake.client().verify_token() == "active"
    assert fake.requests == [
        ("GET", "/client/v4/user/tokens/verify"),
        ("GET", "/client/v4/accounts"),
        ("GET", "/client/v4/accounts/account-1/tokens/verify"),
    ]


def test_cfat_token_skips_user_verification() -> None:
    fake = FakeCloudflare(token="cfat_account-secret")
    assert fake.client().verify_token() == "active"
    assert all("user/tokens/verify" not in path for _, path in fake.requests)


@pytest.mark.parametrize("accounts_status", [403, 200])
def test_account_verification_can_discover_id_from_zone(
    accounts_status: int,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/accounts"):
            if accounts_status == 403:
                return httpx.Response(403, json=fixture("cloudflare", "error_auth.json"))
            return httpx.Response(200, json={"success": True, "result": []})
        if path.endswith("/zones"):
            return httpx.Response(
                200,
                json={"success": True, "result": [{"account": {"id": "zone-account"}}]},
            )
        return httpx.Response(200, json={"success": True, "result": {"status": "active"}})

    client = CloudflareClient(
        "cfat_token",
        base_url="https://api.test/client/v4",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.verify_token() == "active"
    assert calls[-1].endswith("/accounts/zone-account/tokens/verify")


def test_invalid_account_token_has_safe_invalid_message() -> None:
    fake = FakeCloudflare(token="cfat-super-secret-token")
    fake.reject_auth = True
    with pytest.raises(CloudflareError) as raised:
        fake.client().verify_token()
    assert "invalid" in str(raised.value)
    assert fake.token not in str(raised.value)


def test_forbidden_request_explains_missing_permission() -> None:
    response = httpx.Response(403, json=fixture("cloudflare", "error_auth.json"))
    with pytest.raises(CloudflareError, match="missing a permission") as raised:
        _decode(response)
    assert raised.value.http_status == 403


def test_the_token_is_sent_only_as_bearer_header_never_in_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=fixture("cloudflare", "zones.json"))

    client = CloudflareClient(
        "cf-header-token",
        base_url="https://api.test/client/v4",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.find_zone(HOST)
    request = seen[0]
    assert request.headers["authorization"] == "Bearer cf-header-token"
    assert "cf-header-token" not in str(request.url)


def test_network_failures_become_adapter_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom with details")

    client = CloudflareClient(
        "t",
        base_url="https://api.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CloudflareError, match="could not be reached"):
        client.find_zone(HOST)


def test_pagination_collects_all_pages() -> None:
    pages = {
        "1": {
            "success": True,
            "result": [{"id": "a", "type": "A", "name": HOST, "content": "1.1.1.1"}],
            "result_info": {"page": 1, "total_pages": 2},
        },
        "2": {
            "success": True,
            "result": [{"id": "b", "type": "TXT", "name": HOST, "content": "x"}],
            "result_info": {"page": 2, "total_pages": 2},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params["page"]])

    client = CloudflareClient(
        "t",
        base_url="https://api.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert [r.id for r in client.list_records("z", HOST)] == ["a", "b"]
