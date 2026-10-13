"""The shared list model: page request validation, in-memory paging, and the project and run
lists (search, filters, sort, page bounds, legacy ``limit``/``offset``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.api.container import AppContainer
from backend.domain.enums import EnvironmentName, PipelineSource, RunStatus, RunTrigger
from backend.domain.errors import ValidationError
from backend.domain.paging import MAX_PER_PAGE, Page, PageQuery, SortOrder, paginate
from backend.repositories.models import Run
from backend.services.projects import ProjectService
from backend.services.runs import RunService
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
)
from tests.conftest import DEV, PROD
from tests.github_support import SHA_A, SHA_B, FakeGithub

# --- the model -------------------------------------------------------------------------------


def test_page_query_offsets_and_defaults() -> None:
    query = PageQuery()
    assert (query.page, query.per_page, query.offset, query.limit) == (1, 25, 0, 25)
    assert PageQuery(page=3, per_page=10).offset == 20
    assert PageQuery(page=3, per_page=10, skip=7).offset == 7  # legacy row offset wins
    assert PageQuery(q="  ").term is None
    assert PageQuery(q=" web ").term == "web"


@pytest.mark.parametrize(
    "bad",
    [
        {"page": 0},
        {"per_page": 0},
        {"per_page": MAX_PER_PAGE + 1},
        {"skip": -1},
        {"q": "x" * 101},
    ],
)
def test_page_query_rejects_out_of_range_values(bad: dict[str, int | str]) -> None:
    with pytest.raises(ValidationError):
        PageQuery(**bad)  # type: ignore[arg-type]


def test_sort_key_must_be_known() -> None:
    assert PageQuery(sort="name").sort_key(["name", "created"]) == "name"
    assert PageQuery().sort_key(["name"]) is None
    with pytest.raises(ValidationError, match="unknown sort"):
        PageQuery(sort="bogus").sort_key(["name"])


def test_page_counts_pages_and_an_empty_list_has_one() -> None:
    assert Page.of([], 0, PageQuery()).pages == 1
    assert Page.of([1], 51, PageQuery(per_page=25)).pages == 3
    assert Page.of([1], 50, PageQuery(per_page=25)).pages == 2


@dataclass(frozen=True)
class Item:
    name: str
    size: int


ITEMS = [Item("alpha", 3), Item("Beta", 1), Item("gamma", 2), Item("alphabet", 5)]
SORTS = {"name": lambda i: i.name.lower(), "size": lambda i: i.size}


def _names(page: Page[Item]) -> list[str]:
    return [item.name for item in page.items]


def _run(query: PageQuery) -> Page[Item]:
    return paginate(
        ITEMS,
        query,
        text=lambda i: (i.name,),
        sorts=SORTS,
        default_sort="name",
        descending=frozenset({"size"}),
    )


def test_paginate_searches_sorts_and_slices() -> None:
    assert _names(_run(PageQuery())) == ["alpha", "alphabet", "Beta", "gamma"]
    found = _run(PageQuery(q="ALPHA"))
    assert (_names(found), found.total) == (["alpha", "alphabet"], 2)
    assert _names(_run(PageQuery(sort="size"))) == ["alphabet", "alpha", "gamma", "Beta"]
    assert _names(_run(PageQuery(sort="size", order=SortOrder.ASC)))[0] == "Beta"
    second = _run(PageQuery(page=2, per_page=3))
    assert (_names(second), second.total, second.pages) == (["gamma"], 4, 2)
    assert _run(PageQuery(page=9)).items == ()


# --- API fixtures ----------------------------------------------------------------------------


@pytest.fixture
def wired(tmp_path: Path, engine: Engine, clock: FakeClock, project: str) -> AppContainer:
    source = FakeGithub({"development": SHA_A, "production": SHA_B})
    return make_container(make_settings(tmp_path), engine, clock, github=source)


@pytest.fixture
def api(wired: AppContainer) -> TestClient:
    create_user(wired)
    client = make_client(wired)
    login(client)
    return client


def _queue(
    wired: AppContainer,
    env: EnvironmentName = DEV,
    *,
    branch: str = "development",
    trigger: RunTrigger = RunTrigger.PUSH,
    slug: str = "demo",
) -> Run:
    return RunService(wired.uow(), wired.clock).create_queued(
        slug,
        env,
        trigger=trigger,
        branch=branch,
        commit_sha=SHA_A,
        pipeline_source=PipelineSource.PLATFORM,
        pipeline_text="",
    )


def _slugs(body: dict[str, list[dict[str, str]]]) -> list[str]:
    return [item["slug"] for item in body["items"]]


# --- projects --------------------------------------------------------------------------------


@pytest.fixture
def many_projects(wired: AppContainer) -> None:
    service = ProjectService(wired.uow())
    for n in range(1, 31):
        service.create(f"app-{n:02d}", f"App {n:02d}", f"octo/app{n:02d}")
    service.create("billing", "Billing_100%", "octo/billing")


def test_projects_are_paged_with_totals(api: TestClient, many_projects: None) -> None:
    first = api.get("/api/projects").json()
    assert (first["total"], first["page"], first["per_page"], first["pages"]) == (32, 1, 25, 2)
    assert len(first["items"]) == 25
    second = api.get("/api/projects", params={"page": 2}).json()
    assert len(second["items"]) == 7
    assert not set(_slugs(first)) & set(_slugs(second))
    small = api.get("/api/projects", params={"per_page": 10, "page": 4}).json()
    assert (small["pages"], len(small["items"])) == (4, 2)
    beyond = api.get("/api/projects", params={"page": 99}).json()
    assert (beyond["items"], beyond["total"]) == ([], 32)


def test_project_search_matches_name_slug_and_repository(
    api: TestClient, many_projects: None
) -> None:
    by_name = api.get("/api/projects", params={"q": "app 07"}).json()
    assert _slugs(by_name) == ["app-07"]
    by_repo = api.get("/api/projects", params={"q": "octo/app1"}).json()
    assert by_repo["total"] == 10
    by_slug = api.get("/api/projects", params={"q": "DEMO"}).json()
    assert _slugs(by_slug) == ["demo"]
    assert api.get("/api/projects", params={"q": "nothing-here"}).json()["total"] == 0


def test_project_search_treats_wildcards_literally(api: TestClient, many_projects: None) -> None:
    assert _slugs(api.get("/api/projects", params={"q": "_100%"}).json()) == ["billing"]
    assert api.get("/api/projects", params={"q": "%"}).json()["total"] == 1
    assert api.get("/api/projects", params={"q": "_"}).json()["total"] == 1


def test_project_sort_and_order(api: TestClient, many_projects: None) -> None:
    default = api.get("/api/projects", params={"per_page": 3}).json()
    assert _slugs(default) == ["app-01", "app-02", "app-03"]
    reverse = api.get("/api/projects", params={"per_page": 2, "sort": "slug", "order": "desc"})
    assert _slugs(reverse.json()) == ["demo", "billing"]
    newest = api.get("/api/projects", params={"per_page": 1, "sort": "created"}).json()
    assert _slugs(newest) == ["billing"]


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "per_page=0",
        "per_page=101",
        "page=x",
        "order=sideways",
        "sort=bogus",
        "q=" + "x" * 101,
    ],
)
def test_project_list_rejects_bad_parameters(api: TestClient, query: str) -> None:
    response = api.get(f"/api/projects?{query}")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_legacy_limit_and_offset_still_select_rows(api: TestClient, many_projects: None) -> None:
    body = api.get("/api/projects", params={"limit": 2, "offset": 3}).json()
    assert _slugs(body) == ["app-04", "app-05"]
    assert (body["per_page"], body["page"]) == (2, 2)


# --- runs ------------------------------------------------------------------------------------


@pytest.fixture
def many_runs(wired: AppContainer, clock: FakeClock) -> None:
    ProjectService(wired.uow()).create("other", "Other", "octo/other")
    lifecycle = wired.run_lifecycle()
    for n in range(12):
        _queue(wired, trigger=RunTrigger.PUSH if n % 2 else RunTrigger.MANUAL)
        clock.advance(seconds=1)
        run = lifecycle.claim_next()
        assert run is not None
        status = RunStatus.FAILED if n % 3 == 0 else RunStatus.SUCCEEDED
        assert lifecycle.finish(run.id, status, "boom" if status is RunStatus.FAILED else None)
    _queue(wired, PROD, branch="production")
    _queue(wired, slug="other", branch="feature/login")


def test_global_runs_are_paged_newest_first(api: TestClient, many_runs: None) -> None:
    first = api.get("/api/runs", params={"per_page": 5}).json()
    assert (first["total"], first["pages"], len(first["items"])) == (14, 3, 5)
    assert first["items"][0]["project"] == "other"
    last = api.get("/api/runs", params={"per_page": 5, "page": 3}).json()
    assert len(last["items"]) == 4
    assert (last["limit"], last["offset"]) == (5, 10)  # the older fields stay in the envelope


def test_run_filters(api: TestClient, many_runs: None) -> None:
    failed = api.get("/api/runs", params={"status": "failed"}).json()
    assert failed["total"] == 4 and {r["status"] for r in failed["items"]} == {"failed"}
    manual = api.get("/api/runs", params={"trigger": "manual"}).json()
    assert manual["total"] == 6
    prod = api.get("/api/runs", params={"environment": "production"}).json()
    assert [r["environment"] for r in prod["items"]] == ["production"]
    other = api.get("/api/runs", params={"project": "other"}).json()
    assert [r["project"] for r in other["items"]] == ["other"]
    both = api.get("/api/runs", params={"status": "failed", "trigger": "manual"}).json()
    assert both["total"] == 2  # n = 0, 3, 6, 9 fail; only the even ones (0, 6) are manual


def test_run_search_matches_branch_message_and_number(api: TestClient, many_runs: None) -> None:
    assert api.get("/api/runs", params={"q": "feature/login"}).json()["total"] == 1
    assert api.get("/api/runs", params={"q": "boom"}).json()["total"] == 4
    assert api.get("/api/runs", params={"q": "other"}).json()["total"] == 1
    numbered = api.get("/api/runs", params={"q": "#3", "project": "demo"}).json()
    assert [r["number"] for r in numbered["items"]] == [3]
    assert api.get("/api/runs", params={"q": SHA_A[:7]}).json()["total"] == 14


def test_environment_runs_search_filter_and_sort(api: TestClient, many_runs: None) -> None:
    url = "/api/projects/demo/environments/development/runs"
    assert api.get(url).json()["total"] == 12
    assert api.get(url, params={"status": "failed"}).json()["total"] == 4
    ascending = api.get(url, params={"sort": "number", "order": "asc", "per_page": 3}).json()
    assert [r["number"] for r in ascending["items"]] == [1, 2, 3]
    assert api.get(url, params={"q": "feature"}).json()["total"] == 0
    assert api.get(url, params={"sort": "bogus"}).status_code == 422


@pytest.mark.parametrize("query", ["status=nope", "trigger=nope", "environment=staging"])
def test_run_filters_reject_unknown_values(api: TestClient, query: str) -> None:
    assert api.get(f"/api/runs?{query}").status_code == 422
