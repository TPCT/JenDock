"""Private immutable managed-service environment revisions.

All values are encrypted, including values a caller considers non-secret. Public
service definitions carry references only. Selection and application are separate
CAS operations; a saved desired revision is not evidence of a running change.
Callers must hold the exclusive worker lock and supply current service authority.
"""
from contextlib import nullcontext
import hashlib
import json
import re
import time
import uuid
from cryptography.fernet import InvalidToken
import database_bindings
import registry


class RuntimeError(ValueError):
    pass


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS service_runtime_heads(
        project TEXT NOT NULL,environment TEXT NOT NULL,service_id TEXT NOT NULL,
        desired TEXT,applied TEXT,PRIMARY KEY(project,environment,service_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS service_runtime_revisions(
        id TEXT NOT NULL,revision INTEGER NOT NULL,project TEXT NOT NULL,
        environment TEXT NOT NULL,service_id TEXT NOT NULL,digest TEXT NOT NULL,
        ciphertext BLOB NOT NULL,created REAL NOT NULL,PRIMARY KEY(id,revision))''')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS service_runtime_{action.lower()} BEFORE {action} ON service_runtime_revisions BEGIN SELECT RAISE(ABORT,'Runtime revisions are immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS service_runtime_replace BEFORE INSERT ON service_runtime_revisions WHEN EXISTS(SELECT 1 FROM service_runtime_revisions WHERE id=NEW.id AND revision=NEW.revision) BEGIN SELECT RAISE(ABORT,'Runtime revisions are immutable'); END")


def _scope(project,environment,service_id):
    registry.identifier(project)
    if environment not in registry.ENVIRONMENTS or not isinstance(service_id,str) or not re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',service_id):
        raise RuntimeError('An exact managed service scope is required.')


def _reference(ref):
    if (not isinstance(ref,dict) or set(ref)!={'id','revision','digest'}
        or not isinstance(ref['id'],str) or not re.fullmatch('[0-9a-f]{32}',ref['id'])
        or type(ref['revision']) is not int or not 1<=ref['revision']<=2147483647
        or not isinstance(ref['digest'],str) or not re.fullmatch('[0-9a-f]{64}',ref['digest'])):
        raise RuntimeError('An immutable runtime reference is required.')


def _authorize(authorize):
    if not callable(authorize):raise RuntimeError('Current service authority is required.')
    authorize()


def _values(values):
    if (not isinstance(values,dict) or len(values)>200
        or any(not isinstance(k,str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,127}',k)
            or not isinstance(v,str) or len(v)>32768 or '\x00' in v for k,v in values.items())
        or len(registry.canonical(values).encode())>1024*1024):
        raise RuntimeError('Bounded environment variable names and string values are required.')


def status(db,project,environment,service_id):
    _scope(project,environment,service_id);initialize(db)
    row=db.execute('SELECT desired,applied FROM service_runtime_heads WHERE project=? AND environment=? AND service_id=?',
                   (project,environment,service_id)).fetchone()
    return {key:json.loads(row[key]) if row and row[key] else None for key in ('desired','applied')}


def save(db,project,environment,service_id,values,*,expected,key_path,authorize):
    """Persist the complete override map; caller merges authorized editor changes."""
    _scope(project,environment,service_id);_values(values);_authorize(authorize);initialize(db)
    if expected is not None:_reference(expected)
    cipher=database_bindings._cipher(key_path)
    with (nullcontext() if db.in_transaction else registry.transaction(db)):
        _authorize(authorize)
        current=status(db,project,environment,service_id)
        if current['desired']!=expected:raise RuntimeError('Desired runtime revision changed.')
        identifier=expected['id'] if expected else uuid.uuid4().hex
        revision=expected['revision']+1 if expected else 1
        document={'purpose':'managed-service-runtime','id':identifier,'revision':revision,
                  'project':project,'environment':environment,'service_id':service_id,'values':values}
        ciphertext=cipher.encrypt(registry.canonical(document).encode())
        # A digest of randomized ciphertext does not expose guesses about values.
        digest=hashlib.sha256(ciphertext).hexdigest()
        ref={'id':identifier,'revision':revision,'digest':digest}
        db.execute('INSERT INTO service_runtime_revisions VALUES(?,?,?,?,?,?,?,?)',
                   (identifier,revision,project,environment,service_id,digest,ciphertext,time.time()))
        db.execute('INSERT INTO service_runtime_heads VALUES(?,?,?,?,NULL) ON CONFLICT(project,environment,service_id) DO UPDATE SET desired=excluded.desired',
                   (project,environment,service_id,registry.canonical(ref)))
        _authorize(authorize)
        return ref


def _row(db,project,environment,service_id,ref):
    _scope(project,environment,service_id);_reference(ref);initialize(db)
    row=db.execute('SELECT * FROM service_runtime_revisions WHERE id=? AND revision=? AND project=? AND environment=? AND service_id=?',
                   (ref['id'],ref['revision'],project,environment,service_id)).fetchone()
    if row is None or row['digest']!=ref['digest'] or hashlib.sha256(row['ciphertext']).hexdigest()!=ref['digest']:
        raise RuntimeError('Accepted runtime identity is unavailable or changed.')
    return row


def resolve(db,project,environment,service_id,ref,*,key_path,authorize):
    """Historical reads require explicit authority for the accepted service pin."""
    _authorize(authorize)
    row=_row(db,project,environment,service_id,ref)
    try:
        document=json.loads(database_bindings._cipher(key_path).decrypt(row['ciphertext']))
        expected={'purpose':'managed-service-runtime','id':ref['id'],'revision':ref['revision'],
                  'project':project,'environment':environment,'service_id':service_id}
        if not isinstance(document,dict) or set(document)!=set(expected)|{'values'} or any(document[k]!=v for k,v in expected.items()):raise ValueError()
        _values(document['values'])
    except (InvalidToken,ValueError,TypeError,KeyError):
        raise RuntimeError('Runtime values cannot be resolved with the accepted identity and key.') from None
    _authorize(authorize)
    return document['values']


def mark_applied(db,project,environment,service_id,ref,*,expected_applied,authorize):
    """Only call after verified runtime and service binding CAS in the same tx.

    A newer desired revision may remain pending while an earlier captured revision
    finishes. This records what was actually applied without rewriting desire.
    """
    _authorize(authorize);_row(db,project,environment,service_id,ref)
    def update():
        _authorize(authorize)
        if status(db,project,environment,service_id)['applied']!=expected_applied:raise RuntimeError('Applied runtime revision changed.')
        changed=db.execute('UPDATE service_runtime_heads SET applied=? WHERE project=? AND environment=? AND service_id=?',
                   (registry.canonical(ref),project,environment,service_id)).rowcount
        if changed!=1:raise RuntimeError('Runtime head is unavailable.')
    if db.in_transaction:update()
    else:
        with registry.transaction(db):update()


def compose_environment(db,project,environment,service_id,ref,*,key_path,authorize):
    values=resolve(db,project,environment,service_id,ref,key_path=key_path,authorize=authorize)
    mapping={key:'DM_RUNTIME_'+hashlib.sha256((service_id+':'+ref['id']+':'+str(ref['revision'])+':'+key).encode()).hexdigest().upper() for key in values}
    return ({key:'${'+variable+':?Managed runtime value is unavailable}' for key,variable in mapping.items()},
            {mapping[key]:value for key,value in values.items()})


class Context:
    """Ephemeral worker-only resolver; never serialize this object or its values."""
    def __init__(self,db,authorize,key_path=None):
        import os
        self.db,self.authorize=db,authorize
        self.key_path=key_path or os.environ.get('DATABASE_INSPECTION_KEY_FILE','')

    def values(self,definition):
        runtime=definition.get('runtime')
        if runtime is None:return {}
        if not isinstance(runtime,dict) or set(runtime)-{'environment','database_attachment'} or 'environment' not in runtime:
            raise RuntimeError('Unsupported accepted runtime definition.')
        values=resolve(self.db,definition['project'],definition['environment'],definition['service_id'],runtime['environment'],key_path=self.key_path,authorize=self.authorize)
        if runtime.get('database_attachment') is not None:
            import application_database_bindings
            supplied=application_database_bindings.runtime_environment(self.db,definition['project'],definition['environment'],definition['service_id'],runtime['database_attachment'],self.key_path,authorize=self.authorize)
            values.update(supplied)
        return values

    def environment(self,definition):
        if not definition.get('runtime'):return {},{}
        values=self.values(definition)
        identity=hashlib.sha256(registry.canonical(definition['runtime']).encode()).hexdigest()
        mapping={key:'DM_RUNTIME_'+hashlib.sha256((definition['service_id']+':'+identity+':'+key).encode()).hexdigest().upper() for key in values}
        return ({key:'${'+variable+':?Managed runtime value is unavailable}' for key,variable in mapping.items()},
                {mapping[key]:value for key,value in values.items()})

    def recorded_attachment(self,definition):
        pin=(definition.get('runtime') or {}).get('database_attachment')
        if pin is None:return None
        import application_database_bindings as bindings
        import application_database_networks as networks
        metadata=bindings.resolve_runtime(self.db,definition['project'],definition['environment'],definition['service_id'],pin,authorize=self.authorize)
        return networks.recorded_attachment(self.db,metadata)

    def attachment(self,definition,docker,*,ensure=False,verify_application=None):
        pin=(definition.get('runtime') or {}).get('database_attachment')
        if pin is None:return None
        import os
        import application_database_bindings as bindings
        import application_database_networks as networks
        import database_candidates
        from database_inventory import DockerInventory
        import control
        metadata=bindings.resolve_runtime(self.db,definition['project'],definition['environment'],definition['service_id'],pin,authorize=self.authorize)
        inventory=DockerInventory(docker=docker,socket=os.environ.get('DOCKER_HOST','unix:///run/docker-isolation.sock'),timeout=8)
        def candidate(project,environment,reference,**options):
            return database_candidates.resolve(self.db,project,environment,reference,inventory=inventory,provenance_resolver=control.review_compose,**options)
        callback=networks.ensure_attachment if ensure else networks.verify_attachment
        return callback(self.db,metadata,docker=docker,authorize=self.authorize,resolve_candidate=candidate,verify_application=verify_application)

    def substitutions(self,definitions):
        result={}
        for definition in definitions:
            _,private=self.environment(definition)
            if set(result)&set(private) and any(result[k]!=v for k,v in private.items() if k in result):
                raise RuntimeError('Conflicting runtime substitution identity.')
            result.update(private)
        return result

    def verify(self,definition,actual,image_environment):
        if not definition.get('runtime'):return
        expected={}
        for entry in image_environment or []:
            if not isinstance(entry,str) or '=' not in entry:raise RuntimeError('Image environment is malformed.')
            key,value=entry.split('=',1);expected[key]=value
        expected.update(self.values(definition))
        entries=actual.get('Config',{}).get('Env') or []
        observed={}
        for entry in entries:
            if not isinstance(entry,str) or '=' not in entry:raise RuntimeError('Container environment is malformed.')
            key,value=entry.split('=',1)
            if key in observed:raise RuntimeError('Container environment is ambiguous.')
            observed[key]=value
        if observed!=expected:raise RuntimeError('Container environment differs from its accepted runtime revision.')


def bound_context(db,definition,key_path=None):
    """Read-only verification authority for an exactly current managed definition."""
    def authorize():
        import managed_services
        current=managed_services.get(db,definition['service_id'])
        if current['definition']!=definition or not current.get('binding'):
            raise RuntimeError('Current managed runtime authority changed.')
    return Context(db,authorize,key_path)


class StoppingContext(Context):
    """Verification-only authority for stopping the exact currently bound app.

    It cannot produce subprocess substitutions or return application credentials.
    Revocation blocks new use; comparing the already accepted bytes for an exact
    stop does not use the database account or start any dependency.
    """
    def _stopping(self,definition):
        ref=definition.get('runtime')
        if not ref:return {},None
        values=resolve(self.db,definition['project'],definition['environment'],definition['service_id'],ref['environment'],key_path=self.key_path,authorize=self.authorize)
        metadata=None
        if ref.get('database_attachment'):
            import application_database_bindings as bindings
            metadata=bindings.resolve_stopping(self.db,definition['project'],definition['environment'],definition['service_id'],ref['database_attachment'],authorize=self.authorize)
        return values,metadata

    def values(self,definition):
        raise RuntimeError('Stop verification cannot return runtime values.')

    def environment(self,definition):
        if not definition.get('runtime'):return {},{}
        values,metadata=self._stopping(definition)
        keys=set(values)
        if metadata:keys.update(metadata['definition']['environment_mapping'].values())
        identity=hashlib.sha256(registry.canonical(definition['runtime']).encode()).hexdigest()
        variables={key:'DM_RUNTIME_'+hashlib.sha256((definition['service_id']+':'+identity+':'+key).encode()).hexdigest().upper() for key in keys}
        return {key:'${'+variable+':?Managed runtime value is unavailable}' for key,variable in variables.items()},{}

    def substitutions(self,definitions):
        raise RuntimeError('Stop verification cannot authorize container creation.')

    def attachment(self,definition,docker,*,ensure=False,verify_application=None):
        if ensure:raise RuntimeError('Stop verification cannot attach a database.')
        _,metadata=self._stopping(definition)
        if metadata is None:return None
        import application_database_networks as networks
        return networks.verify_stopping_attachment(self.db,metadata,docker=docker,authorize=self.authorize)

    def verify(self,definition,actual,image_environment):
        if not definition.get('runtime'):return
        values,metadata=self._stopping(definition)
        expected={}
        for entry in image_environment or []:
            if not isinstance(entry,str) or '=' not in entry:raise RuntimeError('Image environment is malformed.')
            key,value=entry.split('=',1);expected[key]=value
        expected.update(values)
        entries=actual.get('Config',{}).get('Env') or []
        observed={}
        for entry in entries:
            if not isinstance(entry,str) or '=' not in entry:raise RuntimeError('Container environment is malformed.')
            key,value=entry.split('=',1)
            if key in observed:raise RuntimeError('Container environment is ambiguous.')
            observed[key]=value
        keys=set(metadata['definition']['environment_mapping'].values()) if metadata else set()
        if set(observed)!=set(expected)|keys or {k:v for k,v in observed.items() if k not in keys}!={k:v for k,v in expected.items() if k not in keys}:
            raise RuntimeError('Container environment differs from its accepted runtime revision.')
        if metadata:
            import application_database_bindings as bindings
            if not bindings.verify_stopping_environment(self.db,definition['project'],definition['environment'],definition['service_id'],definition['runtime']['database_attachment'],observed,self.key_path,authorize=self.authorize):
                raise RuntimeError('Accepted application database environment differs.')
