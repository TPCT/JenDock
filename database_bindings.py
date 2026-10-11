"""Scoped database configuration and secrets; saving is never verification.

Runtime observations belong to a trusted worker verifier, not caller-supplied flags.
No Docker, database connection, permission grant, or credential discovery occurs here.
"""
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
import application_operations as ops
import registry


class BindingError(ValueError):
    pass


class SaveRejected(BindingError):
    """A committed tombstone guarantees this exact request cannot mutate later."""
    outcome = "not_committed"


def initialize(db):
    for sql in (
        'CREATE TABLE IF NOT EXISTS database_save_requests(owner TEXT NOT NULL,actor_kind TEXT NOT NULL,project TEXT NOT NULL,environment TEXT NOT NULL,action TEXT NOT NULL,request_id TEXT NOT NULL,payload BLOB NOT NULL,result TEXT NOT NULL,PRIMARY KEY(owner,actor_kind,project,environment,action,request_id))',
        'CREATE TABLE IF NOT EXISTS database_credentials(id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,revision INTEGER NOT NULL,enabled INTEGER NOT NULL,ciphertext BLOB NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)',
        'CREATE TABLE IF NOT EXISTS database_binding_definitions(id TEXT PRIMARY KEY,document TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)',
        'CREATE TABLE IF NOT EXISTS database_bindings(id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,revision INTEGER NOT NULL,enabled INTEGER NOT NULL,definition_id TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)',
        'CREATE TABLE IF NOT EXISTS database_binding_events(id INTEGER PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,object_id TEXT NOT NULL,revision INTEGER NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL)',
    ):db.execute(sql)
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS database_definition_immutable_{action.lower()} BEFORE {action} ON database_binding_definitions BEGIN SELECT RAISE(ABORT,'Database definitions are immutable'); END")


def _authorize(db,principal,project,environment,scope,revalidate=None):
    if revalidate is not None and revalidate()!=principal:raise BindingError('Database access changed. Authenticate again.')
    ops._service_authorize(db,principal,scope,project,environment)
    ops.recheck_credential(db,principal)


def _revision(value):
    if type(value) is not int or not 0<=value<9223372036854775807:raise BindingError('Invalid expected revision.')


def _id(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise BindingError('Invalid database reference.')
    return value


def _digest(document):
    import hashlib
    return hashlib.sha256(registry.canonical(document).encode()).hexdigest()


def _key_material(key_path):
    try:
        fd=os.open(Path(key_path),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid not in (0,os.geteuid()):raise ValueError()
            value=stream.read(4097).strip()
        if len(value)>4096:raise ValueError()
        Fernet(value)
        return value
    except (OSError,TypeError,ValueError):raise BindingError('Database encryption key is unavailable or insecure.') from None


def _cipher(key_path):
    return Fernet(_key_material(key_path))


def _credential(db,project,environment,identifier):
    row=db.execute('SELECT * FROM database_credentials WHERE id=? AND project=? AND environment=?',(_id(identifier),project,environment)).fetchone()
    if row is None:raise BindingError('Database credential is unavailable in this environment.')
    return row


def _public_credential(row):
    return {k:row[k] for k in ('id','project','environment','revision','created','updated')} | {'enabled':bool(row['enabled'])}


def _event(db,principal,project,environment,identifier,revision,action):
    db.execute('INSERT INTO database_binding_events(project,environment,object_id,revision,action,actor,created) VALUES(?,?,?,?,?,?,?)',
        (project,environment,identifier,revision,action,principal.subject_id,time.time()))



def _request_context(principal,project,environment,action,request_id):
    if request_id is None:return None
    if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9_.:-]{8,128}',request_id):raise BindingError('Invalid database request ID.')
    return (principal.subject_id,principal.actor_kind,project,environment,action,request_id)


def _replay(db,context,payload,cipher=None):
    if context is None:return None
    row=db.execute('SELECT payload,result FROM database_save_requests WHERE owner=? AND actor_kind=? AND project=? AND environment=? AND action=? AND request_id=?',context).fetchone()
    if row is None:return None
    try:
        if cipher is not None:
            saved=json.loads(cipher.decrypt(row['payload']))
            if saved!={'purpose':'database-save-replay','context':list(context),'request':payload}:raise ValueError()
        elif row['payload']!=_digest(payload):raise ValueError()
        result=json.loads(row['result'])
        if not isinstance(result,dict):raise ValueError()
        return result
    except (InvalidToken,ValueError,TypeError,KeyError):raise BindingError('Database request ID conflicts with its saved request or encryption key.') from None


def _remember(db,context,payload,result,cipher=None):
    if context is None:return result
    saved=cipher.encrypt(registry.canonical({'purpose':'database-save-replay','context':list(context),'request':payload}).encode()) if cipher is not None else _digest(payload)
    db.execute('INSERT INTO database_save_requests VALUES(?,?,?,?,?,?,?,?)',(*context,saved,registry.canonical(result)))
    return result


def _save(db,principal,project,environment,context,payload,perform,*,cipher=None,revalidate=None):
    """Serialize replay, mutation and durable rejection in one write transaction.

    A missing receipt is never proof of noncommit. Rejections are persisted under
    the same request key as success, after rolling back every attempted mutation.
    Authorization/key failures and conflicting request payloads remain ambiguous.
    """
    if len(registry.canonical(payload).encode())>65536:
        raise BindingError('Database save payload is too large.')
    rejected=None
    with registry.transaction(db):
        _authorize(db,principal,project,environment,'databases:manage',revalidate)
        result=_replay(db,context,payload,cipher)
        if result is None:
            db.execute('SAVEPOINT database_save_attempt')
            try:
                result=perform()
            except BindingError as error:
                db.execute('ROLLBACK TO database_save_attempt')
                if context is None:raise
                result={'_database_save_rejected':True,'error':str(error)}
            finally:
                db.execute('RELEASE database_save_attempt')
            _remember(db,context,payload,result,cipher)
        if result.get('_database_save_rejected') is True:
            rejected=result['error']
    # Expose final noncommit only after the tombstone transaction committed.
    if rejected is not None:
        _authorize(db,principal,project,environment,'databases:manage',revalidate)
        raise SaveRejected(rejected)
    return result


def save_credential(db,principal,project,environment,values,*,expected_revision,key_path,credential_id=None,revalidate=None,request_id=None):
    _authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    context=_request_context(principal,project,environment,'credential-save',request_id)
    payload={'values':values,'expected_revision':expected_revision,'credential_id':credential_id}
    cipher=_cipher(key_path)
    def perform():
        _revision(expected_revision)
        if (not isinstance(values,dict) or set(values)!={'username','password'}
                or any(not isinstance(values[k],str) or not 1<=len(values[k])<=4096 or '\x00' in values[k] for k in values)
                or len(values['username'])>128 or any(ord(c)<32 for c in values['username'])):
            raise BindingError('A bounded database username and password are required.')
        identifier=_id(credential_id) if credential_id is not None else uuid.uuid4().hex
        prior=_credential(db,project,environment,identifier) if credential_id is not None else None
        if expected_revision!=(prior['revision'] if prior else 0):raise BindingError('Database credential revision changed.')
        revision=expected_revision+1;now=time.time()
        document={'purpose':'database-inspection','id':identifier,'project':project,'environment':environment,'revision':revision,**values}
        ciphertext=cipher.encrypt(registry.canonical(document).encode())
        if prior:
            db.execute('UPDATE database_credentials SET revision=?,enabled=1,ciphertext=?,updated=? WHERE id=?',(revision,ciphertext,now,identifier))
        else:db.execute('INSERT INTO database_credentials VALUES(?,?,?,?,?,?,?,?)',(identifier,project,environment,revision,1,ciphertext,now,now))
        _event(db,principal,project,environment,identifier,revision,'credential-saved')
        return _public_credential(_credential(db,project,environment,identifier))

    return _save(db,principal,project,environment,context,payload,perform,cipher=cipher,revalidate=revalidate)


def revoke_credential(db,principal,project,environment,credential_id,*,expected_revision,revalidate=None):
    _authorize(db,principal,project,environment,'databases:manage',revalidate);_revision(expected_revision);initialize(db)
    with registry.transaction(db):
        _authorize(db,principal,project,environment,'databases:manage',revalidate)
        row=_credential(db,project,environment,credential_id)
        if row['revision']!=expected_revision:raise BindingError('Database credential revision changed.')
        db.execute('UPDATE database_credentials SET revision=revision+1,enabled=0,updated=? WHERE id=?',(time.time(),credential_id))
        _event(db,principal,project,environment,credential_id,expected_revision+1,'credential-revoked')
        return _public_credential(_credential(db,project,environment,credential_id))


def credential_for_use(db,project,environment,credential_id,revision,key_path):
    """Internal-only secret resolver; caller must freshly authorize the operation."""
    if environment not in registry.ENVIRONMENTS:raise BindingError('An exact environment is required.')
    _revision(revision);row=_credential(db,project,environment,credential_id)
    if not row['enabled'] or row['revision']!=revision:raise BindingError('Database credential is disabled or changed.')
    try:
        document=json.loads(_cipher(key_path).decrypt(row['ciphertext']))
        expected={'purpose':'database-inspection','id':credential_id,'project':project,'environment':environment,'revision':revision}
        if set(document)!=set(expected)|{'username','password'} or any(document[k]!=v for k,v in expected.items()):raise ValueError()
        if any(not isinstance(document[k],str) or not 1<=len(document[k])<=4096 or '\x00' in document[k] for k in ('username','password')):raise ValueError()
        return {k:document[k] for k in ('username','password')}
    except (InvalidToken,TypeError,ValueError,KeyError):raise BindingError('Database credential cannot be resolved with its current identity and key.') from None


def _page(limit,cursor):
    if type(limit) is not int or not 1<=limit<=100:raise BindingError('Choose a page size between 1 and 100.')
    if cursor is not None:_id(cursor)
    return limit,cursor or ''


def get_credential(db,principal,project,environment,credential_id,*,revalidate=None):
    _authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    return _public_credential(_credential(db,project,environment,credential_id))


def list_credentials(db,principal,project,environment,*,revalidate=None,limit=25,cursor=None):
    _authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    limit,cursor=_page(limit,cursor)
    return [_public_credential(r) for r in db.execute('SELECT * FROM database_credentials WHERE project=? AND environment=? AND id>? ORDER BY id LIMIT ?',(project,environment,cursor,limit))]


def _definition(values,project,environment):
    fields={'engine','adapter_version','ownership','database_name','database_port','secret_id','secret_revision','allowed_schemas','policy_revision'}
    if not isinstance(values,dict) or not fields<=set(values) or set(values)-fields-{'row_columns'}:raise BindingError('Database binding fields are invalid.')
    if values['engine'] not in ('postgresql','mysql','mariadb'):raise BindingError('Database engine is unsupported.')
    for key in ('adapter_version','secret_revision','policy_revision'):
        if type(values[key]) is not int or values[key]<1:raise BindingError('Database binding revisions must be positive integers.')
    if type(values['database_port']) is not int or not 1<=values['database_port']<=65535:raise BindingError('Database port is invalid.')
    ownership=values['ownership']
    if (not isinstance(ownership,dict) or set(ownership)!={'kind','reference','revision'}
            or ownership['kind'] not in ('adopted-compose','managed-service')
            or not isinstance(ownership['reference'],str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',ownership['reference'])
            or type(ownership['revision']) is not int or ownership['revision']<1):raise BindingError('An explicit database ownership reference is required.')
    schemas=values['allowed_schemas']
    def name(value):return isinstance(value,str) and 1<=len(value)<=128 and not any(ord(c)<32 or ord(c)==127 for c in value)
    if not name(values['database_name']) or not isinstance(schemas,list) or not 1<=len(schemas)<=100 or not all(name(v) for v in schemas) or len(set(schemas))!=len(schemas):raise BindingError('Database and schema identifiers are invalid.')
    # Omitted policy is metadata-only. A revision without a policy document
    # never grants row access; permitted columns are part of the immutable digest.
    rows=values.get('row_columns',{})
    if not isinstance(rows,dict) or set(rows)-set(schemas):raise BindingError('Row policy schemas are invalid.')
    total=0
    for schema,tables in rows.items():
        if not isinstance(tables,dict):raise BindingError('Row policy tables are invalid.')
        total+=len(tables)
        for table,columns in tables.items():
            if (not name(table) or not isinstance(columns,list) or not 1<=len(columns)<=20
                    or not all(name(column) for column in columns) or len(set(columns))!=len(columns)):
                raise BindingError('Row policy columns are invalid.')
    if total>100:raise BindingError('Row policy exceeds the table limit.')
    _id(values['secret_id'])
    return json.loads(registry.canonical({'schema_version':1,'project':project,'environment':environment,**values}))


def _binding(db,project,environment,identifier):
    row=db.execute('SELECT * FROM database_bindings WHERE id=? AND project=? AND environment=?',(_id(identifier),project,environment)).fetchone()
    if row is None:raise BindingError('Database binding is unavailable in this environment.')
    return row


def _public_binding(db,row):
    saved=db.execute('SELECT document,digest FROM database_binding_definitions WHERE id=?',(row['definition_id'],)).fetchone()
    try:
        document=json.loads(saved['document'])
        if _digest(document)!=saved['digest'] or document!=_definition({k:v for k,v in document.items() if k not in ('schema_version','project','environment')},row['project'],row['environment']):raise ValueError()
    except (TypeError,KeyError,ValueError):raise BindingError('Database binding integrity is invalid.') from None
    secret=db.execute('SELECT revision,enabled FROM database_credentials WHERE id=? AND project=? AND environment=?',(document['secret_id'],row['project'],row['environment'])).fetchone()
    status='unverified' if secret and secret['enabled'] and secret['revision']==document['secret_revision'] else 'credential-stale'
    if not row['enabled']:status='disabled'
    acceptance=None
    # Historical acceptance is not a current runtime health assertion. Runtime
    # reads independently revalidate credentials, ownership, storage and engine.
    if status=='unverified' and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_accepted_bindings'").fetchone():
        accepted=db.execute('SELECT binding_revision,definition_digest,accepted FROM database_accepted_bindings WHERE binding_id=?',(row['id'],)).fetchone()
        if accepted and accepted['binding_revision']==row['revision'] and accepted['definition_digest']==saved['digest']:
            acceptance={'binding_revision':row['revision'],'definition_digest':saved['digest'],
                'accepted_at':accepted['accepted'],'runtime_revalidation_required':True}
    return {k:row[k] for k in ('id','project','environment','revision','definition_id','created','updated')} | {'enabled':bool(row['enabled']),'definition':document,'definition_digest':saved['digest'],'status':status,'verified':False,'acceptance':acceptance}


def save_binding(db,principal,project,environment,values,*,expected_revision,binding_id=None,revalidate=None,request_id=None):
    _authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    context=_request_context(principal,project,environment,'binding-save',request_id)
    payload={'values':values,'expected_revision':expected_revision,'binding_id':binding_id}
    def perform():
        _revision(expected_revision)
        definition=_definition(values,project,environment)
        identifier=_id(binding_id) if binding_id is not None else uuid.uuid4().hex
        prior=_binding(db,project,environment,identifier) if binding_id is not None else None
        if expected_revision!=(prior['revision'] if prior else 0):raise BindingError('Database binding revision changed.')
        secret=_credential(db,project,environment,definition['secret_id'])
        if not secret['enabled'] or secret['revision']!=definition['secret_revision']:raise BindingError('Database credential is disabled or changed.')
        definition_id=uuid.uuid4().hex;now=time.time();revision=expected_revision+1
        db.execute('INSERT INTO database_binding_definitions VALUES(?,?,?,?)',(definition_id,registry.canonical(definition),_digest(definition),now))
        if prior:db.execute('UPDATE database_bindings SET revision=?,enabled=1,definition_id=?,updated=? WHERE id=?',(revision,definition_id,now,identifier))
        else:db.execute('INSERT INTO database_bindings VALUES(?,?,?,?,?,?,?,?)',(identifier,project,environment,revision,1,definition_id,now,now))
        _event(db,principal,project,environment,identifier,revision,'binding-saved')
        return _public_binding(db,_binding(db,project,environment,identifier))

    return _save(db,principal,project,environment,context,payload,perform,revalidate=revalidate)


def get_binding(db,principal,project,environment,binding_id,*,revalidate=None):
    _authorize(db,principal,project,environment,'databases:read',revalidate);initialize(db)
    return _public_binding(db,_binding(db,project,environment,binding_id))


def list_bindings(db,principal,project,environment,*,revalidate=None,limit=25,cursor=None):
    _authorize(db,principal,project,environment,'databases:read',revalidate);initialize(db)
    limit,cursor=_page(limit,cursor)
    return [_public_binding(db,row) for row in db.execute('SELECT * FROM database_bindings WHERE project=? AND environment=? AND id>? ORDER BY id LIMIT ?',(project,environment,cursor,limit))]


def disable_binding(db,principal,project,environment,binding_id,*,expected_revision,revalidate=None):
    _authorize(db,principal,project,environment,'databases:manage',revalidate);_revision(expected_revision);initialize(db)
    with registry.transaction(db):
        _authorize(db,principal,project,environment,'databases:manage',revalidate)
        row=_binding(db,project,environment,binding_id)
        if row['revision']!=expected_revision:raise BindingError('Database binding revision changed.')
        db.execute('UPDATE database_bindings SET revision=revision+1,enabled=0,updated=? WHERE id=?',(time.time(),binding_id))
        _event(db,principal,project,environment,binding_id,expected_revision+1,'binding-disabled')
        return _public_binding(db,_binding(db,project,environment,binding_id))
