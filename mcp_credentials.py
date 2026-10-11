"""Console-owned opaque MCP credentials; only their SHA-256 digest is persisted.

Issued grants are immutable. Revocation is checked on every authentication and worker
boundary. Deployments with mutable owner roles must supply a current owner resolver.
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
import uuid

from application_operations import OperationError, Principal
import registry

SCOPES = frozenset({'projects:read', 'projects:create', 'services:read',
                    'services:create', 'services:start', 'operations:read'})
_FIELDS = {'label', 'scopes', 'allowed_projects', 'allowed_repository_ids',
           'allowed_environments', 'expires_at'}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS mcp_credentials(
        id TEXT PRIMARY KEY, token_digest TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
        label TEXT NOT NULL, scopes TEXT NOT NULL, allowed_projects TEXT NOT NULL,
        allowed_repository_ids TEXT NOT NULL, allowed_environments TEXT NOT NULL,
        expires_at REAL NOT NULL, created_at REAL NOT NULL, revoked_at REAL, last_used_at REAL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS mcp_credential_events(
        id INTEGER PRIMARY KEY, credential_id TEXT NOT NULL, actor TEXT NOT NULL,
        event TEXT NOT NULL CHECK(event IN ('created','revoked')), created_at REAL NOT NULL)''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS mcp_credentials_immutable_grants
        BEFORE UPDATE OF id,token_digest,owner,label,scopes,allowed_projects,
        allowed_repository_ids,allowed_environments,expires_at,created_at ON mcp_credentials
        BEGIN SELECT RAISE(ABORT,'Credential grants are immutable'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS mcp_credentials_irreversible_revocation
        BEFORE UPDATE OF revoked_at ON mcp_credentials WHEN OLD.revoked_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'Credential revocation is permanent'); END''')


def validate_owner_configuration():
    """Validate explicit runtime configuration without returning credential material."""
    try:
        path = os.environ.get('CREDENTIALS_FILE')
        if not path: raise ValueError()
        with open(path, encoding='utf-8') as source:
            document = source.read(65537)
        if len(document) > 65536: raise ValueError()
        values = json.loads(document)
        username = values.get('username') if isinstance(values, dict) else None
        if (not isinstance(username, str) or not username.strip() or len(username) > 120
                or any(ord(c) < 32 or ord(c) == 127 for c in username)):
            raise ValueError()
        return username
    except (OSError, ValueError, TypeError):
        raise ValueError('A valid console owner configuration is required for MCP access.') from None


def configured_owner(subject):
    """Resolve the currently configured single console owner at each boundary."""
    try:
        username = validate_owner_configuration()
    except ValueError:
        return None
    return Principal(username, 'console', SCOPES) if username == subject else None


def _manager(owner):
    if not isinstance(owner, Principal) or owner.actor_kind != 'console' or owner.credential_id is not None:
        raise OperationError('forbidden', 'Manage MCP access from an authenticated console session.', 403)
    if 'CREDENTIALS_FILE' in os.environ and configured_owner(owner.subject_id) is None:
        raise OperationError('forbidden', 'The current console owner could not be verified.', 403)


def _invalid():
    raise OperationError('invalid_credential_request', 'Choose a label, supported scopes, bounded project/repository/environment targets and an expiry within 366 days.', 400)


def _array(values, name, minimum=1):
    result = values.get(name)
    if not isinstance(result, list) or not minimum <= len(result) <= 100:
        _invalid()
    return result


def _subset(granted, allowed):
    return allowed is None or granted <= allowed


def _validate(owner, values, now):
    if not isinstance(values, dict) or set(values) != _FIELDS:
        _invalid()
    label = values['label']
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in label):
        _invalid()
    scopes = _array(values, 'scopes')
    if any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes): _invalid()
    projects = _array(values, 'allowed_projects')
    for project in projects:
        try: registry.identifier(project)
        except ValueError: _invalid()
    repos = _array(values, 'allowed_repository_ids')
    if any(type(value) is not int or not 1 <= value <= 2**63 - 1 for value in repos): _invalid()
    environments = _array(values, 'allowed_environments', 0)
    if any(not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
           or pair[0] not in projects or pair[1] not in registry.ENVIRONMENTS for pair in environments): _invalid()
    expiry = values['expires_at']
    if type(expiry) not in (int, float) or not now < expiry <= now + 366*86400 or not math.isfinite(expiry): _invalid()
    scopes, projects, repos = frozenset(scopes), frozenset(projects), frozenset(repos)
    environments = frozenset(tuple(pair) for pair in environments)
    if not (scopes <= owner.scopes and _subset(projects, owner.allowed_projects)
            and _subset(repos, owner.allowed_repository_ids) and _subset(environments, owner.allowed_environments)):
        raise OperationError('forbidden', 'Credential permissions must stay within the console owner permissions.', 403)
    if any(scope.startswith('services:') for scope in scopes) and not environments: _invalid()
    return label.strip(), scopes, projects, repos, environments, expiry


def _public(row):
    result = {key: row[key] for key in ('id', 'owner', 'label', 'expires_at', 'created_at', 'revoked_at', 'last_used_at')}
    for key in ('scopes', 'allowed_projects', 'allowed_repository_ids', 'allowed_environments'):
        result[key] = json.loads(row[key])
    return result


def create(db, owner, values):
    _manager(owner)
    now = time.time()
    label, scopes, projects, repos, environments, expiry = _validate(owner, values, now)
    initialize(db)
    identifier = str(uuid.uuid4())
    # 32 uniformly random bytes (256 bits); the ID is public and is not a secret.
    token = 'mcp_' + identifier + '_' + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    with registry.transaction(db):
        db.execute('INSERT INTO mcp_credentials VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                   (identifier, digest, owner.subject_id, label, registry.canonical(sorted(scopes)),
                    registry.canonical(sorted(projects)), registry.canonical(sorted(repos)),
                    registry.canonical(sorted(environments)), expiry, now))
        db.execute('INSERT INTO mcp_credential_events(credential_id,actor,event,created_at) VALUES(?,?,?,?)',
                   (identifier, owner.subject_id, 'created', now))
        result = _public(db.execute('SELECT * FROM mcp_credentials WHERE id=?', (identifier,)).fetchone())
    return {'credential': result, 'token': token}


def list_credentials(db, owner):
    _manager(owner); initialize(db)
    return [_public(row) for row in db.execute('SELECT * FROM mcp_credentials WHERE owner=? ORDER BY created_at DESC,id',
                                               (owner.subject_id,))]


def revoke(db, owner, credential_id):
    _manager(owner); initialize(db)
    if not isinstance(credential_id, str) or len(credential_id) > 80:
        raise OperationError('credential_not_found', 'MCP credential was not found.', 404)
    with registry.transaction(db):
        row = db.execute('SELECT * FROM mcp_credentials WHERE id=? AND owner=?', (credential_id, owner.subject_id)).fetchone()
        if row is None: raise OperationError('credential_not_found', 'MCP credential was not found.', 404)
        if row['revoked_at'] is None:
            now = time.time()
            db.execute('UPDATE mcp_credentials SET revoked_at=? WHERE id=?', (now, credential_id))
            db.execute('INSERT INTO mcp_credential_events(credential_id,actor,event,created_at) VALUES(?,?,?,?)',
                       (credential_id, owner.subject_id, 'revoked', now))
        return _public(db.execute('SELECT * FROM mcp_credentials WHERE id=?', (credential_id,)).fetchone())


def _denied():
    raise OperationError('invalid_credential', 'A valid, unexpired MCP access credential is required.', 401)


def _principal(row, owner_resolver):
    if row is None or row['revoked_at'] is not None or row['expires_at'] <= time.time(): _denied()
    grants = {key: frozenset(tuple(item) if isinstance(item, list) else item for item in json.loads(row[key]))
              for key in ('scopes', 'allowed_projects', 'allowed_repository_ids', 'allowed_environments')}
    if owner_resolver is None and 'CREDENTIALS_FILE' in os.environ:
        owner_resolver = configured_owner
    if owner_resolver is not None:
        owner = owner_resolver(row['owner'])
        if not isinstance(owner, Principal) or owner.subject_id != row['owner'] or owner.actor_kind != 'console': _denied()
        for key, issued in grants.items():
            current = getattr(owner, key)
            if current is not None: grants[key] = issued & current
    return Principal(row['owner'], 'mcp', credential_id=row['id'], **grants)


def principal_for_credential(db, credential_id, *, owner_resolver=None):
    """Recheck stored grants before queued work starts and at safe journal boundaries."""
    initialize(db)
    if not isinstance(credential_id, str) or len(credential_id) > 80: _denied()
    return _principal(db.execute('SELECT * FROM mcp_credentials WHERE id=?', (credential_id,)).fetchone(), owner_resolver)


def authenticate(db, token, *, owner_resolver=None):
    initialize(db)
    if not isinstance(token, str) or not re.fullmatch(r'mcp_([0-9a-f-]{36})_([A-Za-z0-9_-]{43})', token): _denied()
    identifier = token[4:40]
    row = db.execute('SELECT * FROM mcp_credentials WHERE id=?', (identifier,)).fetchone()
    digest = hashlib.sha256(token.encode('ascii')).hexdigest()
    if not hmac.compare_digest(digest, row['token_digest'] if row else '0'*64): _denied()
    principal = _principal(row, owner_resolver)
    # Bounded metadata only: no raw headers, addresses, clients or credential input.
    db.execute('UPDATE mcp_credentials SET last_used_at=? WHERE id=? AND revoked_at IS NULL', (time.time(), identifier))
    return principal
