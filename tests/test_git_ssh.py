"""The SSH transport: command construction, pinned host keys and how git is invoked."""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.adapters.errors import SourceError
from backend.adapters.git_access import (
    SshSettings,
    https_access,
    resolve_key_file,
    ssh_access,
    ssh_argv,
    ssh_command,
    ssh_url,
)
from backend.adapters.git_remote import GitBranchHeads
from backend.adapters.git_sources import GitSshSource, TransportRoutingSource
from backend.adapters.sources import CheckoutRequest, SourceProvider
from backend.domain.enums import GitTransport
from backend.domain.errors import ValidationError

KNOWN_HOSTS = Path(__file__).resolve().parent.parent / "docker" / "github_known_hosts"
# GitHub's published fingerprint of its ed25519 host key.
GITHUB_ED25519_FINGERPRINT = "+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
SHA = "a" * 40


def _settings(tmp_path: Path) -> SshSettings:
    key = tmp_path / "keys" / "deploy_key"
    key.parent.mkdir(exist_ok=True)
    key.write_text("not a real key\n")
    return SshSettings(key, KNOWN_HOSTS)


def _key(settings: SshSettings) -> Path:
    assert settings.key_file is not None
    return settings.key_file


def _options(argv: list[str]) -> dict[str, str]:
    pairs = [argv[i + 1] for i, word in enumerate(argv) if word == "-o"]
    return dict(pair.split("=", 1) for pair in pairs)


# --- command construction --------------------------------------------------------------------


def test_the_ssh_argv_has_the_security_options(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    argv = ssh_argv(settings, _key(settings))
    assert argv[0] == "ssh"
    assert argv[argv.index("-i") + 1] == str(_key(settings))
    assert argv[argv.index("-p") + 1] == "443"
    assert argv[argv.index("-F") + 1] == "/dev/null"  # no user or system ssh config
    options = _options(argv)
    assert options["IdentitiesOnly"] == "yes"
    assert options["IdentityAgent"] == "none"
    assert options["StrictHostKeyChecking"] == "yes"
    assert options["UserKnownHostsFile"] == str(KNOWN_HOSTS)
    assert options["GlobalKnownHostsFile"] == "/dev/null"
    assert options["BatchMode"] == "yes"
    assert options["PasswordAuthentication"] == "no"
    assert options["HostKeyAlias"] == "github.com"
    assert options["Hostname"] == "ssh.github.com"
    assert options["HostKeyAlgorithms"] == "ssh-ed25519"
    assert options["ClearAllForwardings"] == "yes"
    assert "StrictHostKeyChecking=no" not in " ".join(argv)


def test_the_command_string_round_trips_through_a_shell_split(tmp_path: Path) -> None:
    settings = SshSettings(
        tmp_path / "dir with space" / "key; touch pwned", tmp_path / "hosts$(id)", port=443
    )
    command = ssh_command(settings, _key(settings))
    assert shlex.split(command) == ssh_argv(settings, _key(settings))
    # git hands the string to ``sh -c``: nothing in it may be interpreted as shell syntax
    result = subprocess.run(  # noqa: S603 - fixed test command
        ["/bin/sh", "-c", f"printf '%s\\n' {command}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == ssh_argv(settings, _key(settings))
    assert not (tmp_path / "pwned").exists()


def test_the_clone_url_names_github_and_validates_the_repository() -> None:
    assert ssh_url("octo/demo") == "ssh://git@github.com/octo/demo.git"
    for bad in ("-oProxyCommand=x/y", "octo/demo.git", "a/b/c", "octo", "../x"):
        with pytest.raises(ValidationError):
            ssh_url(bad)


@pytest.mark.skipif(shutil.which("ssh") is None, reason="no ssh client")
def test_the_real_ssh_client_reads_our_options_the_way_we_mean(tmp_path: Path) -> None:
    """``ssh -G`` prints the effective configuration without connecting anywhere."""
    settings = _settings(tmp_path)
    shown = subprocess.run(  # noqa: S603 - fixed argv built by the code under test
        [
            shutil.which("ssh") or "ssh",
            *ssh_argv(settings, _key(settings))[1:],
            "-G",
            "git@github.com",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    config = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in shown.splitlines()}
    assert config["hostname"] == "ssh.github.com"
    assert config["port"] == "443"
    assert config["user"] == "git"
    assert config["identitiesonly"] == "yes"
    assert config["stricthostkeychecking"] == "true"
    assert config["userknownhostsfile"] == str(KNOWN_HOSTS)
    assert config["identityfile"] == str(_key(settings))
    assert config["hostkeyalias"] == "github.com"
    assert config["batchmode"] == "yes"
    assert config["identityagent"] == "none"


# --- pinned host keys ------------------------------------------------------------------------


def test_the_pinned_known_hosts_hold_githubs_ed25519_key() -> None:
    entries = [
        line.split()
        for line in KNOWN_HOSTS.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert {entry[0] for entry in entries} == {"github.com", "[ssh.github.com]:443"}
    for _host, key_type, key in entries:
        assert key_type == "ssh-ed25519"
        digest = hashlib.sha256(base64.b64decode(key)).digest()
        assert base64.b64encode(digest).decode().rstrip("=") == GITHUB_ED25519_FINGERPRINT


# --- access and sources ----------------------------------------------------------------------


def test_access_checks_the_files_exist(tmp_path: Path) -> None:
    settings = SshSettings(tmp_path / "missing_key", KNOWN_HOSTS)
    with pytest.raises(SourceError, match="deploy key file does not exist"):
        ssh_access(settings).check("octo/demo")
    no_hosts = SshSettings(_settings(tmp_path).key_file, tmp_path / "missing_hosts")
    with pytest.raises(SourceError, match="known_hosts file does not exist"):
        ssh_access(no_hosts).check("octo/demo")
    ssh_access(_settings(tmp_path)).check("octo/demo")


def test_the_access_environment_carries_the_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    access = ssh_access(settings)
    assert access.env_for("octo/demo") == {"GIT_SSH_COMMAND": ssh_command(settings, _key(settings))}
    assert access.resolve("octo/demo") == "ssh://git@github.com/octo/demo.git"


def test_without_a_deploy_key_ssh_explains_itself(tmp_path: Path) -> None:
    access = ssh_access(SshSettings(None, KNOWN_HOSTS, key_dir=tmp_path / "keys"))
    with pytest.raises(SourceError, match=r"deploy-key create octo/demo.*JENDOCK_GIT_SSH_KEY_FILE"):
        access.check("octo/demo")
    with pytest.raises(SourceError, match="JENDOCK_GIT_SSH_KEY_FILE"):
        access.env_for("octo/demo")


# --- per-repository deploy keys --------------------------------------------------------------


def _per_repo(tmp_path: Path, shared: bool) -> SshSettings:
    key_dir = tmp_path / "deploy_keys"
    key_dir.mkdir()
    shared_key = tmp_path / "shared_key"
    shared_key.write_text("shared\n")
    return SshSettings(shared_key if shared else None, KNOWN_HOSTS, key_dir=key_dir)


def test_a_per_repository_key_wins_over_the_shared_key(tmp_path: Path) -> None:
    settings = _per_repo(tmp_path, shared=True)
    assert settings.key_dir is not None
    own = settings.key_dir / "octo__demo"
    own.write_text("own\n")
    assert resolve_key_file(settings, "octo/demo") == own
    assert resolve_key_file(settings, "octo/other") == _key(settings)  # falls back
    ssh_access(settings).check("octo/demo")


def test_only_the_per_repository_key_works_without_a_shared_key(tmp_path: Path) -> None:
    settings = _per_repo(tmp_path, shared=False)
    assert settings.key_dir is not None
    (settings.key_dir / "octo__demo").write_text("own\n")
    ssh_access(settings).check("octo/demo")
    with pytest.raises(SourceError, match="SSH access is not configured"):
        ssh_access(settings).check("octo/other")


def test_two_repositories_use_two_different_identity_files(tmp_path: Path) -> None:
    settings = _per_repo(tmp_path, shared=False)
    assert settings.key_dir is not None
    for name in ("octo__one", "octo__two"):
        (settings.key_dir / name).write_text("k\n")
    access = ssh_access(settings)
    argv_one = shlex.split(access.env_for("octo/one")["GIT_SSH_COMMAND"])
    argv_two = shlex.split(access.env_for("octo/two")["GIT_SSH_COMMAND"])
    assert argv_one[argv_one.index("-i") + 1] == str(settings.key_dir / "octo__one")
    assert argv_two[argv_two.index("-i") + 1] == str(settings.key_dir / "octo__two")
    assert [a for a in argv_one if a != argv_one[argv_one.index("-i") + 1]] == [
        a for a in argv_two if a != argv_two[argv_two.index("-i") + 1]
    ]  # everything else is identical


def test_a_repository_name_cannot_traverse_out_of_the_key_directory(tmp_path: Path) -> None:
    settings = _per_repo(tmp_path, shared=True)
    (tmp_path / "secret").write_text("x")
    for bad in ("../secret", "octo/../../secret", "octo/..", "a/b/c", "/etc/passwd", "octo"):
        with pytest.raises(ValidationError):
            resolve_key_file(settings, bad)
        with pytest.raises(ValidationError):
            ssh_access(settings).env_for(bad)


def test_https_access_uses_the_template() -> None:
    access = https_access("https://example.test/{repository}.git")
    assert access.resolve("octo/demo") == "https://example.test/octo/demo.git"
    assert access.env_for("octo/demo") == {}
    access.check("octo/demo")


class _FakeGit:
    """A stand-in git executable that records its argv and the ssh environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.log = tmp_path / "calls.jsonl"
        self.path = tmp_path / "fake-git"
        self.path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            f"with open({str(self.log)!r}, 'a') as out:\n"
            "    out.write(json.dumps({'args': args, 'ssh': os.environ.get('GIT_SSH_COMMAND'),\n"
            "                          'home': os.environ.get('HOME'),\n"
            "                          'prompt': os.environ.get('GIT_TERMINAL_PROMPT')}) + '\\n')\n"
            "if 'rev-parse' in args:\n"
            f"    print({SHA!r})\n"
        )
        self.path.chmod(0o755)

    def calls(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def test_the_ssh_source_runs_git_without_a_shell_and_with_the_ssh_command(tmp_path: Path) -> None:
    fake = _FakeGit(tmp_path)
    settings = _settings(tmp_path)
    destination = tmp_path / "ws"
    destination.mkdir()
    GitSshSource(settings, git=str(fake.path)).checkout(
        CheckoutRequest("octo/demo", SHA, GitTransport.SSH), destination
    )
    calls = fake.calls()
    clone = calls[0]
    assert clone["args"][-3:] == ["--", "ssh://git@github.com/octo/demo.git", str(destination)]  # type: ignore[index]
    assert clone["ssh"] == ssh_command(settings, _key(settings))
    assert clone["home"] == "/dev/null"
    assert clone["prompt"] == "0"
    assert {call["ssh"] for call in calls} == {ssh_command(settings, _key(settings))}


def test_every_git_path_uses_the_key_of_its_repository(tmp_path: Path) -> None:
    fake = _FakeGit(tmp_path)
    key_dir = tmp_path / "deploy_keys"
    key_dir.mkdir()
    settings = SshSettings(None, KNOWN_HOSTS, key_dir=key_dir)
    for name in ("octo__one", "octo__two"):
        (key_dir / name).write_text("k\n")
    source = GitSshSource(settings, git=str(fake.path))
    for repository in ("octo/one", "octo/two"):
        destination = tmp_path / repository.replace("/", "_")
        destination.mkdir()
        source.checkout(CheckoutRequest(repository, SHA, GitTransport.SSH), destination)
        source.read_file(CheckoutRequest(repository, SHA, GitTransport.SSH), "Jenkinsfile")
        GitBranchHeads({GitTransport.SSH: ssh_access(settings)}, str(fake.path)).head(
            repository, GitTransport.SSH, "main", details=False
        )
    identities: dict[str, set[str]] = {}
    for call in fake.calls():
        command = shlex.split(str(call["ssh"]))
        repo_url = next((a for a in call["args"] if "github.com/" in a), "")  # type: ignore[attr-defined]
        if not repo_url:
            continue  # init, log, cat-file ... carry no URL
        identities.setdefault(repo_url, set()).add(command[command.index("-i") + 1])
    assert identities == {
        "ssh://git@github.com/octo/one.git": {str(key_dir / "octo__one")},
        "ssh://git@github.com/octo/two.git": {str(key_dir / "octo__two")},
    }


def test_the_ssh_source_refuses_to_run_without_its_files(tmp_path: Path) -> None:
    fake = _FakeGit(tmp_path)
    settings = SshSettings(tmp_path / "nope", KNOWN_HOSTS)
    source = GitSshSource(settings, git=str(fake.path))
    request = CheckoutRequest("octo/demo", SHA, GitTransport.SSH)
    with pytest.raises(SourceError, match="deploy key"):
        source.checkout(request, tmp_path)
    with pytest.raises(SourceError, match="deploy key"):
        source.read_file(request, "Jenkinsfile")
    assert not fake.log.exists()  # git was never started


class _Recorder:
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen: list[str] = []

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        self.seen.append(f"checkout:{request.repository}")

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:
        self.seen.append(f"read:{path}")
        return self.name


def test_routing_picks_the_provider_of_the_projects_transport(tmp_path: Path) -> None:
    ssh, https = _Recorder("ssh"), _Recorder("https")
    providers: dict[GitTransport, SourceProvider] = {
        GitTransport.SSH: ssh,
        GitTransport.HTTPS: https,
    }
    router = TransportRoutingSource(providers)
    assert router.read_file(CheckoutRequest("o/r", SHA, GitTransport.SSH), "Jenkinsfile") == "ssh"
    assert router.read_file(CheckoutRequest("o/r", SHA), "Jenkinsfile") == "https"
    router.checkout(CheckoutRequest("o/r", SHA, GitTransport.SSH), tmp_path)
    assert ssh.seen == ["read:Jenkinsfile", "checkout:o/r"]
    assert https.seen == ["read:Jenkinsfile"]
    only_https = TransportRoutingSource({GitTransport.HTTPS: https})
    with pytest.raises(SourceError, match="transport ssh"):
        only_https.checkout(CheckoutRequest("o/r", SHA, GitTransport.SSH), tmp_path)
