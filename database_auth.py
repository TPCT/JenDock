"""Worker-side console authentication for private database inspection requests.

The receiver supplies a session digest over its trusted control channel. This
module never accepts an actor, grants, or a credential ID as authentication.
"""
from dataclasses import dataclass
import json
import math
import os
import re
import sqlite3
import time
from application_operations import Principal, OperationError

SCOPES = frozenset({'databases:manage', 'databases:read', 'databases:data:read'})


def _denied():
    return OperationError('forbidden', 'A current authenticated console session is required.', 403)


def resolve_console(db, envelope, credentials_file=None):
    if (not isinstance(envelope, dict) or set(envelope) != {'kind', 'session_digest'}
            or envelope['kind'] != 'console' or not isinstance(envelope['session_digest'], str)
            or not re.fullmatch('[a-f0-9]{64}', envelope['session_digest'])):
        raise _denied()
    try:
        path = credentials_file if credentials_file is not None else os.environ.get('CREDENTIALS_FILE')
        if not path:
            raise ValueError()
        with open(path, encoding='utf-8') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        config = json.loads(raw)
        owner = config.get('username') if isinstance(config, dict) else None
        if (not isinstance(owner, str) or not owner.strip() or len(owner) > 120
                or any(ord(c) < 32 or ord(c) == 127 for c in owner)):
            raise ValueError()
        row = db.execute('SELECT username,created,touched FROM sessions WHERE token=?',
                         (envelope['session_digest'],)).fetchone()
        if row is None or row['username'] != owner:
            raise ValueError()
        now = time.time()
        created, touched = row['created'], row['touched']
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (now, created, touched))
                or not 0 <= created <= touched <= now
                or now - created >= 43200 or now - touched >= 3600):
            raise ValueError()
        return Principal(owner, 'console', SCOPES)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        raise _denied() from None


@dataclass(frozen=True, init=False)
class ConsoleAuthority:
    """Pin the authenticated owner, rereading live authority at every boundary."""
    db: object
    session_digest: str
    credentials_file: object
    principal: Principal

    def __init__(self, db, envelope, credentials_file=None):
        principal = resolve_console(db, envelope, credentials_file)
        object.__setattr__(self, 'db', db)
        object.__setattr__(self, 'session_digest', envelope['session_digest'])
        object.__setattr__(self, 'credentials_file', credentials_file)
        object.__setattr__(self, 'principal', principal)

    def recheck(self):
        current = resolve_console(self.db, {'kind': 'console', 'session_digest': self.session_digest}, self.credentials_file)
        if current != self.principal:
            raise _denied()
        return current

    def __call__(self):
        return self.recheck()


class MCPAuthority:
    """Private bearer proof, freshly authenticated at every worker boundary.

    The receiver's peer identity must already be checked. Never serialize this
    object or its envelope into public responses or diagnostics.
    """
    __slots__ = ('_db', '_token', '_credentials_file', '_principal')

    def __init__(self, db, envelope, credentials_file=None):
        if (not isinstance(envelope,dict) or set(envelope)!={'kind','token'}
                or envelope['kind']!='mcp' or not isinstance(envelope['token'],str)
                or len(envelope['token'])>512):
            raise _denied()
        self._db=db;self._token=envelope['token'];self._credentials_file=credentials_file
        self._principal=self._resolve()

    @property
    def principal(self):return self._principal

    def _resolve(self):
        import mcp_credentials
        def owner(subject):
            try:
                path=self._credentials_file if self._credentials_file is not None else os.environ.get('CREDENTIALS_FILE')
                if not path:raise ValueError()
                with open(path,encoding='utf-8') as source:raw=source.read(65537)
                if len(raw)>65536:raise ValueError()
                value=json.loads(raw)
                username=value.get('username') if isinstance(value,dict) else None
                if (not isinstance(username,str) or not username.strip() or len(username)>120
                        or any(ord(c)<32 or ord(c)==127 for c in username) or username!=subject):raise ValueError()
                return Principal(username,'console',mcp_credentials.SCOPES)
            except (OSError,ValueError,TypeError):return None
        try:
            principal=mcp_credentials.authenticate(self._db,self._token,owner_resolver=owner)
            if (not principal.allowed_environments or not principal.allowed_projects
                    or not principal.allowed_repository_ids or 'databases:read' not in principal.scopes):raise _denied()
            return principal
        except (ValueError,sqlite3.Error):raise _denied() from None

    def recheck(self):
        current=self._resolve()
        if current!=self._principal:raise _denied()
        return current

    def __call__(self):return self.recheck()
