import pytest

from shiftcodes.domain.audit import validate_audit_details
from shiftcodes.domain.errors import ValidationError
from shiftcodes.domain.values import (
    BranchName,
    CommitSha,
    GitHubRepository,
    ProjectSlug,
    SecretName,
    validate_compose_env_files,
    validate_compose_files,
    validate_compose_project,
    validate_display_name,
    validate_service_names,
)


@pytest.mark.parametrize("value", ["a", "demo", "my-app-2", "a" * 64, "9lives"])
def test_slug_valid(value: str) -> None:
    assert str(ProjectSlug(value)) == value


@pytest.mark.parametrize("value", ["", "Demo", "-a", "a-", "a_b", "a b", "a" * 65, "a/b"])
def test_slug_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        ProjectSlug(value)


@pytest.mark.parametrize("value", ["octo/demo", "a/b", "Octo-Cat/my.repo_1", "o/.github"])
def test_repository_valid(value: str) -> None:
    assert str(GitHubRepository(value)) == value


@pytest.mark.parametrize(
    "value",
    ["", "demo", "/demo", "octo/", "octo/demo/x", "-o/demo", "octo/..", "octo/a.git", "o c/d"],
)
def test_repository_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        GitHubRepository(value)


@pytest.mark.parametrize("value", ["main", "feature/x-1", "release/1.2.3", "a.b", "v1", "_x"])
def test_branch_valid(value: str) -> None:
    assert str(BranchName(value)) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-x",
        "a..b",
        "/a",
        "a/",
        "a//b",
        ".hidden",
        "a/.b",
        "a.lock",
        "a/b.lock/c",
        "a.",
        "a b",
        "a~1",
        "a^",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "a@{b",
        "@",
        "a\nb",
        "x" * 201,
    ],
)
def test_branch_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        BranchName(value)


@pytest.mark.parametrize("value", ["A", "_X", "DATABASE_URL", "A1", "A" * 128])
def test_secret_name_valid(value: str) -> None:
    assert str(SecretName(value)) == value


@pytest.mark.parametrize("value", ["", "a", "1A", "A-B", "A B", "A" * 129, "Ab"])
def test_secret_name_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        SecretName(value)


def test_commit_sha() -> None:
    assert str(CommitSha("a" * 40)) == "a" * 40
    for bad in ["", "a" * 39, "A" * 40, "g" * 40, "a" * 41]:
        with pytest.raises(ValidationError):
            CommitSha(bad)


def test_display_name() -> None:
    assert validate_display_name("  Demo  ") == "Demo"
    for bad in ["", "   ", "x" * 121]:
        with pytest.raises(ValidationError):
            validate_display_name(bad)


def test_service_names() -> None:
    assert validate_service_names(["web", "db_1", "a.b-c"]) == ["web", "db_1", "a.b-c"]
    for bad in [["-web"], [""], ["a b"], ["web", "web"]]:
        with pytest.raises(ValidationError):
            validate_service_names(bad)


def test_compose_files() -> None:
    assert validate_compose_files(["/srv/app/compose.yml"]) == ["/srv/app/compose.yml"]
    for bad in [["compose.yml"], ["/srv/../etc/x"], ["/a", "/a"], ["/a\0b"]]:
        with pytest.raises(ValidationError):
            validate_compose_files(bad)


def test_compose_env_files() -> None:
    assert validate_compose_env_files(["/opt/p/ports.env"]) == ["/opt/p/ports.env"]
    for bad in [["ports.env"], ["/srv/../etc/x"], ["/a", "/a"], ["/a\0b"]]:
        with pytest.raises(ValidationError):
            validate_compose_env_files(bad)


def test_compose_project() -> None:
    assert validate_compose_project(None) is None
    assert validate_compose_project("demo-dev") == "demo-dev"
    for bad in ["Demo", "-x", "a b", ""]:
        with pytest.raises(ValidationError):
            validate_compose_project(bad)


def test_audit_details_reject_sensitive_keys_at_any_depth() -> None:
    assert validate_audit_details({"name": "X", "ids": [1, 2]}) == {"name": "X", "ids": [1, 2]}
    bad_details: list[dict[str, object]] = [
        {"password": "x"},
        {"API_KEY": "x"},
        {"nested": {"auth_token": "x"}},
        {"items": [{"Authorization": "x"}]},
        {"secret_value": "x"},
        {"cookie": "x"},
    ]
    for bad in bad_details:
        with pytest.raises(ValidationError):
            validate_audit_details(bad)
