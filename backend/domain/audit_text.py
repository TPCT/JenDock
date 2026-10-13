"""Human-readable descriptions of audit events, one per action.

Descriptions are built only from the event's (secret-free) details; a missing detail never
raises, the sentence just gets shorter. Unknown actions fall back to the action name.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

Details = Mapping[str, object]


def _text(details: Details, key: str) -> str:
    value = details.get(key)
    return "" if value is None else str(value)


def _quoted(details: Details, key: str, fallback: str) -> str:
    value = _text(details, key)
    return f'"{value}"' if value else fallback


def _of(details: Details, key: str) -> str:
    value = _text(details, key)
    return f" in {value}" if value else ""


def _count(value: object, noun: str) -> str:
    number = value if isinstance(value, int) and not isinstance(value, bool) else 0
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _webhook(details: Details) -> str:
    outcome = _text(details, "outcome")
    repo = _text(details, "repository") or "a repository"
    branch = _text(details, "branch")
    where = f"{repo}@{branch}" if branch else repo
    reason = _text(details, "reason")
    suffix = f" ({reason})" if reason else ""
    if outcome == "queued":
        return f"GitHub push to {where} queued {_count(len(_list(details, 'runs')), 'run')}"
    return (
        f"GitHub {_text(details, 'event') or 'event'} for {where}: {outcome or 'handled'}{suffix}"
    )


def _list(details: Details, key: str) -> list[object]:
    value = details.get(key)
    return list(value) if isinstance(value, list) else []


def _environment_update(details: Details) -> str:
    project = _text(details, "project")
    env = _text(details, "environment")
    auto = details.get("auto_deploy")
    state = ""
    if isinstance(auto, Mapping) and auto.get("from") != auto.get("to"):
        state = f"; auto-deploy {'on' if auto.get('to') else 'off'}"
    target = f"{project} / {env}" if project and env else env or project or "an environment"
    return f"Changed the settings of {target}{state}"


def _project_update(details: Details) -> str:
    changes = details.get("changes")
    fields = ", ".join(sorted(changes)) if isinstance(changes, Mapping) else ""
    return f"Updated the project ({fields})" if fields else "Updated the project"


_DESCRIBERS: dict[str, Callable[[Details], str]] = {
    "auth.login": lambda d: (
        f"Signed in with {_text(d, 'two_factor').replace('_', ' ')}"
        if d.get("two_factor")
        else "Signed in"
    ),
    "auth.logout": lambda _d: "Signed out",
    "auth.login_failed": lambda d: f"Failed sign-in attempt for {_quoted(d, 'username', 'a user')}",
    "auth.two_factor_failed": lambda d: (
        f"Wrong two-factor code for {_quoted(d, 'username', 'a user')}"
    ),
    "auth.recovery_code_used": lambda _d: "Signed in with a recovery code",
    "user.create": lambda d: f"Created the user {_quoted(d, 'username', '')}".strip(),
    "user.reset_password": lambda d: (
        f"Reset the password of {_quoted(d, 'username', 'a user')} and ended their sessions"
    ),
    "user.password_change": lambda d: (
        f"Changed the password; ended {_count(d.get('sessions_ended'), 'other session')}"
    ),
    "user.profile_update": lambda d: (
        "Updated the profile (" + ", ".join(str(x) for x in _list(d, "changed")) + ")"
    ),
    "session.revoke": lambda _d: "Revoked a session",
    "session.revoke_others": lambda d: (
        f"Revoked {_count(d.get('sessions_ended'), 'other session')}"
    ),
    "two_factor.enable": lambda _d: "Turned on two-factor authentication",
    "two_factor.disable": lambda _d: "Turned off two-factor authentication",
    "two_factor.recovery_regenerate": lambda _d: "Generated new recovery codes",
    "token.create": lambda d: (
        f"Created the API token {_quoted(d, 'name', '')} "
        f"with scopes {', '.join(str(s) for s in _list(d, 'scopes')) or 'none'}"
    ),
    "token.revoke": lambda d: f"Revoked the API token {_quoted(d, 'name', '')}".strip(),
    "project.create": lambda d: (
        f"Created the project {_text(d, 'slug')} for {_text(d, 'repository')}".strip()
    ),
    "project.update": _project_update,
    "environment.update": _environment_update,
    "pipeline.save": lambda d: f"Saved pipeline version {_text(d, 'version')}",
    "run.create": lambda d: (
        f"Started run #{_text(d, 'number')} ({_text(d, 'trigger')}) on "
        f"{_text(d, 'environment')} at {_text(d, 'commit')[:7]}"
    ),
    "run.cancel": lambda d: f"Cancelled run #{_text(d, 'number')}",
    "deployment.redeploy": lambda d: (
        f"Redeployed an earlier version as run #{_text(d, 'run_number')}"
    ),
    "secret.create": lambda d: f"Created the secret {_text(d, 'name')}",
    "secret.rotate": lambda d: f"Rotated the secret {_text(d, 'name')}",
    "secret.delete": lambda d: f"Deleted the secret {_text(d, 'name')}",
    "secret.reveal": lambda d: f"Revealed the secret {_text(d, 'name')}",
    "secret.resolve": lambda d: f"A run read {_count(len(_list(d, 'names')), 'secret')}",
    "domain.create": lambda d: f"Added the domain {_text(d, 'hostname')}",
    "domain.delete": lambda d: f"Deleted the domain {_text(d, 'hostname')}",
    "domain.apply": lambda d: f"Applied the ingress of {_text(d, 'hostname')}",
    "domain.remove": lambda d: f"Removed the ingress of {_text(d, 'hostname')}",
    "certificate.issue": lambda d: f"Issued a certificate for {_text(d, 'hostname')}",
    "certificate.renew": lambda d: f"Renewed the certificate of {_text(d, 'hostname')}",
    "certificate.issue_failed": lambda d: f"Certificate issuance failed for {_text(d, 'hostname')}",
    "database.connection.create": lambda d: (
        f"Added the database connection {_quoted(d, 'name', '')}{_of(d, 'database')}"
    ),
    "database.connection.update": lambda _d: "Changed a database connection",
    "database.connection.delete": lambda d: (
        f"Deleted the database connection {_quoted(d, 'name', '')}".strip()
    ),
    "database.connection.test": lambda d: (
        f"Tested the database connection {_quoted(d, 'name', '')}: "
        f"{'reachable' if d.get('ok') else 'failed'}"
    ),
    "database.rows.read": lambda d: (
        f"Browsed {_text(d, 'schema')}.{_text(d, 'table')} on {_quoted(d, 'name', 'a database')}"
    ),
    "database.query": lambda d: f"Ran a query on {_quoted(d, 'name', 'a database')}",
    "github.webhook": _webhook,
}


def known_actions() -> list[str]:
    """Every action that has a description, sorted (for the filter drop-down)."""
    return sorted(_DESCRIBERS)


def describe_audit(action: str, details: Details) -> str:
    """A one-line description of an event, always secret-free (details are)."""
    describer = _DESCRIBERS.get(action)
    if describer is None:
        return action
    text = describer(details).strip()
    return text or action
