"""Independent workload authority for private console and MCP requests.

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

SCOPES = frozenset({'workloads:read', 'workloads:retry'})


def _denied():
    return OperationError('forbidden', 'A current authenticated console session is required.', 403)


def resolve_console(db, envelope, credentials_file=None):
    # Reuse the actual session/owner identity bridge, never its database grants.
    from database_auth import resolve_console as current_console_identity
    owner = current_console_identity(db, envelope, credentials_file)
    return Principal(owner.subject_id, 'console', SCOPES)


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
                    or not principal.allowed_repository_ids or 'workloads:read' not in principal.scopes):raise _denied()
            return principal
        except (ValueError,sqlite3.Error):raise _denied() from None

    def recheck(self):
        current=self._resolve()
        if current!=self._principal:raise _denied()
        return current

    def __call__(self):return self.recheck()


def authorize(db, authority, actor, request, action):
    """Live grants only. Transport separately validates exact definition/container pins."""
    from application_operations import _service_authorize
    if action not in {'read','replay','accept','dispatch','commit','reconcile','reconcile_commit'}:
        raise _denied()
    principal = authority.recheck()
    if actor != actor_identity(principal) or not isinstance(request,dict):
        raise _denied()
    _service_authorize(db, principal, 'workloads:read', request.get('project'), request.get('environment'))
    if action not in {'read','reconcile','reconcile_commit'}:
        _service_authorize(db, principal, 'workloads:retry', request.get('project'), request.get('environment'))
    return principal


def from_peer(db, envelope, peer_uid, credentials_file=None):
    """Only kernel-supplied SO_PEERCRED UID; both channels disabled unless configured."""
    if not isinstance(envelope,dict): raise _denied()
    kind=envelope.get('kind')
    variable={'console':'WORKLOAD_RECEIVER_UID','mcp':'WORKLOAD_MCP_UID'}.get(kind)
    raw=os.environ.get(variable) if variable else None
    if raw is None or not re.fullmatch(r'[0-9]+',raw) or type(peer_uid) is not int or peer_uid != int(raw):
        raise _denied()
    return (ConsoleAuthority if kind=='console' else MCPAuthority)(db,envelope,credentials_file)


def actor_identity(principal):
    # Credential identity is immutable and prevents same-owner clients sharing receipts.
    if principal.actor_kind == 'console' and principal.credential_id is None:
        return 'console:' + principal.subject_id
    if not principal.credential_id: raise _denied()
    return 'mcp:' + principal.credential_id


class Authority:
    def __init__(self,db,inner):
        self.db=db
        self.inner=inner
        self.principal=inner.principal
        self.actor=actor_identity(self.principal)
    def recheck(self): return self.inner.recheck()
    def authorize(self,actor,request,action):
        return authorize(self.db,self.inner,actor,request,action)


def resolve(db,envelope,credentials_file=None):
    """Caller must validate kernel peer separately with from_peer or equivalent."""
    if not isinstance(envelope,dict) or envelope.get('kind') not in {'console','mcp'}: raise _denied()
    inner=(ConsoleAuthority if envelope['kind']=='console' else MCPAuthority)(db,envelope,credentials_file)
    return Authority(db,inner)
