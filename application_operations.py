"""Shared authenticated application operations; no provider writes or provisioning."""
from dataclasses import dataclass
import hashlib
import json
import re
import time
import uuid

import integrations
import registry


class OperationError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Principal:
    subject_id: str
    actor_kind: str
    scopes: frozenset
    credential_id: str | None = None
    allowed_projects: frozenset | None = None
    allowed_repository_ids: frozenset | None = None

    def __post_init__(self):
        if (not isinstance(self.subject_id, str) or not 1 <= len(self.subject_id) <= 120
                or any(ord(c) < 32 for c in self.subject_id) or self.actor_kind not in ('console', 'mcp', 'internal')):
            raise OperationError('forbidden', 'Authenticated principal is invalid.', 403)
        for field in ('scopes', 'allowed_projects', 'allowed_repository_ids'):
            value = getattr(self, field)
            if value is not None:
                if not isinstance(value, (set, frozenset)):
                    raise OperationError('forbidden', 'Authenticated permissions are invalid.', 403)
                object.__setattr__(self, field, frozenset(value))
        if self.scopes is None:
            raise OperationError('forbidden', 'Authenticated permissions are required.', 403)


@dataclass(frozen=True)
class ProjectInput:
    project: str
    repository: str
    label: str | None = None
    repository_id: int | None = None
    integration_ref: str = 'github'


def initialize(db):
    registry.initialize(db); integrations.initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS application_operations(
        id TEXT PRIMARY KEY, subject TEXT NOT NULL, actor_kind TEXT NOT NULL, credential_id TEXT,
        operation TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
        project TEXT NOT NULL, repository_id INTEGER NOT NULL, result TEXT NOT NULL, created REAL NOT NULL,
        UNIQUE(subject,operation,request_key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS application_operation_events(
        id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, created REAL NOT NULL)''')


def _authorize(principal, scope, project, repository_id=None):
    if (not isinstance(principal, Principal) or scope not in principal.scopes
            or principal.allowed_projects is not None and project not in principal.allowed_projects
            or repository_id is not None and principal.allowed_repository_ids is not None and repository_id not in principal.allowed_repository_ids):
        raise OperationError('forbidden', 'This operation is not permitted for this principal.', 403)


def project_input(project, values, integration_ref='github'):
    if not isinstance(values, dict) or set(values) - {'repository', 'repository_id', 'label'}:
        raise OperationError('invalid_input', 'Only project name and repository identity may be supplied.')
    try:
        registry.identifier(project); registry.project_values(values, creating=True)
    except ValueError:
        raise OperationError('invalid_input', 'Enter a valid project ID, display name and owner/repository identity.') from None
    if integration_ref != 'github':
        raise OperationError('invalid_input', 'Choose the configured GitHub integration.')
    return ProjectInput(project, values['repository'], values.get('label'), values.get('repository_id'), integration_ref)


def _replay(db, principal, key, fingerprint):
    row = db.execute("SELECT * FROM application_operations WHERE subject=? AND operation='project-create' AND request_key=?",
                     (principal.subject_id, key)).fetchone()
    if row is None: return None
    _authorize(principal, 'projects:create', row['project'], row['repository_id'])
    if row['fingerprint'] != fingerprint:
        raise OperationError('idempotency_conflict', 'This request ID belongs to different project details. Start a new request.')
    return json.loads(row['result'])


def create_project(db, principal, input, idempotency_key, key_path, *, get=None):
    if not isinstance(input, ProjectInput):
        raise OperationError('invalid_input', 'Typed project input is required.')
    # Revalidate typed input too; callers cannot bypass the adapter validator.
    values = {'repository': input.repository}
    if input.label is not None: values['label'] = input.label
    if input.repository_id is not None: values['repository_id'] = input.repository_id
    project_input(input.project, values, input.integration_ref)
    _authorize(principal, 'projects:create', input.project, input.repository_id)
    if principal.allowed_repository_ids is not None and input.repository_id is None:
        raise OperationError('forbidden', 'A permitted repository ID is required for this principal.', 403)
    if not isinstance(idempotency_key, str) or not re.fullmatch('[A-Za-z0-9-]{16,100}', idempotency_key):
        raise OperationError('invalid_request_id', 'A stable creation request ID is required.')
    initialize(db)
    fingerprint = hashlib.sha256(registry.canonical({'project': input.project, 'values': values,
                                                    'integration_ref': input.integration_ref}).encode()).hexdigest()
    existing = _replay(db, principal, idempotency_key, fingerprint)
    if existing is not None: return existing
    try:
        with registry.transaction(db):
            token = integrations.credential_for_use(db, 'github', key_path)
            connection = db.execute("SELECT revision FROM integration_connections WHERE provider='github'").fetchone()['revision']
    except integrations.IntegrationError:
        raise OperationError('integration_unavailable', 'Connect and enable GitHub before creating a project.') from None
    try:
        # Official repository metadata endpoint; this verifies no clone or write rights.
        # https://docs.github.com/en/rest/repos/repos#get-a-repository
        metadata = (get or integrations.get_json)('github', '/repos/' + input.repository, token)
        name, identifier = metadata['full_name'], metadata['id']
        registry.project_values({'repository': name, 'repository_id': identifier}, creating=True)
        if name.casefold() != input.repository.casefold() or input.repository_id is not None and identifier != input.repository_id:
            raise ValueError()
    except Exception:
        raise OperationError('repository_unverified', 'GitHub could not verify this exact repository identity. Check its name and connection access.') from None
    finally:
        token = None
    _authorize(principal, 'projects:create', input.project, identifier)
    with registry.transaction(db):
        existing = _replay(db, principal, idempotency_key, fingerprint)
        if existing is not None: return existing
        current = db.execute("SELECT revision,enabled FROM integration_connections WHERE provider='github'").fetchone()
        if current is None or not current['enabled'] or current['revision'] != connection:
            raise OperationError('integration_changed', 'GitHub connection changed during verification. Retry the same request.')
        if db.execute('SELECT 1 FROM registry_projects WHERE id=?', (input.project,)).fetchone():
            raise OperationError('project_exists', 'This project ID already exists. Choose another ID.')
        verified = dict(values, repository=name, repository_id=identifier)
        registry._insert(db, input.project, verified)
        # Slots have no recipe/runtime binding; automatic defaults do not provision them.
        project = registry.get_project(db, input.project)
        operation_id = str(uuid.uuid4())
        operation = {'operation_id': operation_id, 'status': 'succeeded', 'resource_id': input.project,
                     'revision': project['revision'], 'detail': 'Repository identity verified; project and two inactive environments created.',
                     'links': {'project': '#project/' + input.project}}
        result = {'project': project, 'operation': operation,
                  'message': 'Project created. Development and Production are inactive; no services were provisioned.'}
        now = time.time()
        db.execute('INSERT INTO application_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (operation_id, principal.subject_id, principal.actor_kind, principal.credential_id, 'project-create',
                    idempotency_key, fingerprint, input.project, identifier, registry.canonical(result), now))
        db.execute('INSERT INTO application_operation_events(operation_id,actor,event,created) VALUES(?,?,?,?)',
                   (operation_id, principal.subject_id, 'project-created', now))
    return result


def get_operation(db, principal, operation_id):
    if not isinstance(principal, Principal) or 'operations:read' not in principal.scopes:
        raise OperationError('forbidden', 'Operation status is not available to this principal.', 403)
    initialize(db)
    row = db.execute('SELECT * FROM application_operations WHERE id=? AND subject=?', (operation_id, principal.subject_id)).fetchone()
    if row is None:
        raise OperationError('operation_not_found', 'Operation was not found.', 404)
    _authorize(principal, 'operations:read', row['project'], row['repository_id'])
    return json.loads(row['result'])['operation']
