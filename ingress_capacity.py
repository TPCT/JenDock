"""One retained infrastructure allocation per environment, shared by all routes.

Internal operations: caller supplies authenticated revision-checked environment
intent. Claims survive uncertain Docker outcomes and are included in application
acceptance and policy reductions. No runtime limits are inferred from these rows.
"""
import re
import time
import registry

_NO_OPERATION = object()
import resource_policy


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS ingress_capacity(
        project TEXT NOT NULL,environment TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,
        memory_mib REAL NOT NULL,cpu REAL NOT NULL,state TEXT NOT NULL,
        container_id TEXT,created REAL NOT NULL,PRIMARY KEY(project,environment))''')


def _identity(project,environment,operation_id=_NO_OPERATION):
    registry.identifier(project)
    if environment not in registry.ENVIRONMENTS:raise ValueError('Unknown environment.')
    if operation_id is not _NO_OPERATION and (not isinstance(operation_id,str) or not re.fullmatch('[A-Za-z0-9-]{16,100}',operation_id)):
        raise ValueError('A stable gateway operation identity is required.')


def get(db,project,environment):
    _identity(project,environment);initialize(db)
    row=db.execute('SELECT * FROM ingress_capacity WHERE project=? AND environment=?',(project,environment)).fetchone()
    return dict(row) if row else None


def totals(db,project,environment):
    row=get(db,project,environment)
    return {k:row[k] if row else 0 for k in ('memory_mib','cpu')}


def host_totals(db):
    initialize(db)
    row=db.execute("SELECT COALESCE(SUM(CASE WHEN state!='bound' THEN memory_mib ELSE 0 END),0), COALESCE(SUM(cpu),0) FROM ingress_capacity").fetchone()
    return {'memory_mib':row[0],'cpu':row[1]}


def reserve(db,project,environment,operation_id,resources,policy_revision):
    _identity(project,environment,operation_id)
    if not isinstance(resources,dict) or set(resources)!={'memory_mib','cpu'}:raise ValueError('Gateway RAM and CPU limits are required.')
    resource_policy.budget(resources)
    if type(policy_revision) is not int or policy_revision<0:raise ValueError('Invalid resource policy revision.')
    import managed_services
    managed_services.initialize(db);initialize(db)
    with registry.transaction(db):
        previous=get(db,project,environment)
        if previous:
            if previous['operation_id']!=operation_id or any(previous[k]!=v for k,v in resources.items()):
                raise ValueError('Gateway allocation belongs to different intent.')
            return previous
        registry.get_environment(db,project,environment)
        if db.execute('SELECT 1 FROM ingress_capacity WHERE operation_id=?',(operation_id,)).fetchone():raise ValueError('Operation belongs to another gateway.')
        policy=resource_policy.read(db,project)
        if policy['revision']!=policy_revision:raise ValueError('Resource policy changed; review it before reserving gateway capacity.')
        managed_services.validate_capacity(db,project,policy['document'])
        env=policy['document']['environments'][environment]
        held=managed_services.reservation_totals(db,project,environment)
        for key,value in resources.items():
            if held[key]+sum(item[key] for item in env['services'].values())+value>env['budget'].get(key,0)+1e-8:
                raise ValueError('Environment budget cannot cover the gateway '+key+' allocation.')
        managed_services.validate_host_capacity(db,resources)
        db.execute('INSERT INTO ingress_capacity VALUES(?,?,?,?,?,?,?,?)',(project,environment,operation_id,resources['memory_mib'],resources['cpu'],'reserved',None,time.time()))
        return get(db,project,environment)


def _owned(db,project,environment,operation_id):
    _identity(project,environment,operation_id)
    row=get(db,project,environment)
    if row is None or row['operation_id']!=operation_id:raise ValueError('Gateway allocation owner changed.')
    return row


def begin_creation(db,project,environment,operation_id):
    initialize(db)
    with registry.transaction(db):
        row=_owned(db,project,environment,operation_id)
        if row['state']=='reserved':db.execute("UPDATE ingress_capacity SET state='creating' WHERE project=? AND environment=?",(project,environment))
        return get(db,project,environment)


def mark_bound(db,project,environment,operation_id,container_id):
    if not isinstance(container_id,str) or not re.fullmatch('[a-f0-9]{64}',container_id):raise ValueError('Exact gateway identity is required.')
    initialize(db)
    with registry.transaction(db):
        row=_owned(db,project,environment,operation_id)
        if row['container_id'] not in (None,container_id):raise ValueError('Gateway container identity changed.')
        db.execute("UPDATE ingress_capacity SET state='bound',container_id=? WHERE project=? AND environment=?",(container_id,project,environment))
        return get(db,project,environment)


def release(db,project,environment,operation_id):
    initialize(db)
    with registry.transaction(db):
        row=_owned(db,project,environment,operation_id)
        if row['state']!='reserved' or row['container_id'] is not None:raise ValueError('Gateway creation must be reconciled before releasing capacity.')
        db.execute('DELETE FROM ingress_capacity WHERE project=? AND environment=?',(project,environment))
