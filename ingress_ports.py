"""Durable per-environment gateway port intent; no listeners or routes created.

The host adapter must supply current Docker-owned ports, then recheck all socket/
Docker ownership immediately before publication. A reservation is not an OS lease.
Bound ports cannot be freed by a request that lacks verified gateway removal.
"""
import re
import socket
import time
import registry


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS ingress_ports(
        project TEXT NOT NULL, environment TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE, port INTEGER NOT NULL UNIQUE,
        container_id TEXT, created REAL NOT NULL, creation_started INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(project,environment))''')


def _identity(project, environment, operation_id=None):
    registry.identifier(project)
    if environment not in ('development','production'):raise ValueError('Choose Development or Production.')
    if operation_id is not None and (not isinstance(operation_id,str) or not re.fullmatch('[A-Za-z0-9-]{16,100}',operation_id)):
        raise ValueError('A stable gateway operation identity is required.')


def get(db, project, environment):
    _identity(project,environment);initialize(db)
    row=db.execute('SELECT * FROM ingress_ports WHERE project=? AND environment=?',(project,environment)).fetchone()
    return dict(row) if row else None


def loopback_available(port):
    """Host adapter only: container-local loopback is not host port evidence."""
    if type(port) is not int or not 1024<=port<=65535:raise ValueError('Invalid gateway port.')
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
        try:listener.bind(('127.0.0.1',port))
        except OSError:return False
    return True


def reserve(db, project, environment, operation_id, *, first, last, occupied_ports, available=loopback_available):
    _identity(project,environment,operation_id)
    if type(first) is not int or type(last) is not int or not 1024<=first<=last<=65535 or last-first>4095:
        raise ValueError('Choose a bounded gateway port range.')
    if not isinstance(occupied_ports,(set,frozenset)) or any(type(p) is not int or not 1<=p<=65535 for p in occupied_ports):
        raise ValueError('Current Docker-owned ports are required.')
    initialize(db)
    with registry.transaction(db):
        existing=get(db,project,environment)
        if existing:
            if existing['operation_id']!=operation_id:raise ValueError('Environment gateway is owned by another operation.')
            return existing
        if db.execute('SELECT 1 FROM ingress_ports WHERE operation_id=?',(operation_id,)).fetchone():
            raise ValueError('Gateway operation belongs to another environment.')
        reserved={r[0] for r in db.execute('SELECT port FROM ingress_ports')}
        for port in range(first,last+1):
            if port in occupied_ports or port in reserved or available(port) is not True:continue
            db.execute('INSERT INTO ingress_ports(project,environment,operation_id,port,container_id,created) VALUES(?,?,?,?,?,?)',(project,environment,operation_id,port,None,time.time()))
            return get(db,project,environment)
        raise ValueError('No gateway port is available in the configured range.')


def begin_creation(db, project, environment, operation_id):
    """Fence release before Docker create; an interrupted outcome retains the port."""
    _identity(project,environment,operation_id);initialize(db)
    with registry.transaction(db):
        row=get(db,project,environment)
        if not row or row['operation_id']!=operation_id:
            raise ValueError('Gateway reservation owner changed.')
        db.execute('UPDATE ingress_ports SET creation_started=1 WHERE project=? AND environment=?',(project,environment))
        return get(db,project,environment)


def bind(db, project, environment, operation_id, container_id):
    _identity(project,environment,operation_id)
    if not isinstance(container_id,str) or not re.fullmatch('[a-f0-9]{64}',container_id):raise ValueError('Verified gateway container identity is required.')
    initialize(db)
    with registry.transaction(db):
        row=get(db,project,environment)
        if not row or row['operation_id']!=operation_id or row['container_id'] not in (None,container_id):
            raise ValueError('Gateway reservation owner or container identity changed.')
        db.execute('UPDATE ingress_ports SET container_id=? WHERE project=? AND environment=?',(container_id,project,environment))
        return get(db,project,environment)


def release(db, project, environment, operation_id):
    """Release only unbound intent. Runtime removal needs a separate identity proof."""
    _identity(project,environment,operation_id);initialize(db)
    with registry.transaction(db):
        row=get(db,project,environment)
        if row is None:return
        if row['operation_id']!=operation_id or row['container_id'] is not None or row['creation_started']:
            raise ValueError('Only the owning unbound gateway intent can release this port.')
        db.execute('DELETE FROM ingress_ports WHERE project=? AND environment=?',(project,environment))
