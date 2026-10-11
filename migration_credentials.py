"""Purpose-bound migration accounts, separate from read-only inspection accounts.

Public operations return metadata only. The internal resolver is for the trusted
worker after current environment/binding authorization, never a public API.
"""
import json
import time
import uuid
from cryptography.fernet import InvalidToken
import database_bindings as bindings
import registry


def initialize(db):
    bindings.initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS migration_credentials(
        id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,
        revision INTEGER NOT NULL,enabled INTEGER NOT NULL,ciphertext BLOB NOT NULL,
        created REAL NOT NULL,updated REAL NOT NULL)''')


def _row(db,project,environment,identifier):
    row=db.execute('SELECT * FROM migration_credentials WHERE id=? AND project=? AND environment=?',
                   (bindings._id(identifier),project,environment)).fetchone()
    if row is None:raise bindings.BindingError('Migration credential is unavailable in this environment.')
    return row


def _values(values):
    if (not isinstance(values,dict) or set(values)!={'username','password'}
            or any(not isinstance(values[k],str) or not 1<=len(values[k])<=4096 or '\x00' in values[k] for k in values)
            or len(values['username'])>128 or any(ord(c)<32 for c in values['username'])):
        raise bindings.BindingError('A bounded migration username and password are required.')


def save_credential(db,principal,project,environment,values,*,expected_revision,key_path,
                    credential_id=None,revalidate=None,request_id=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate)
    initialize(db)
    context=bindings._request_context(principal,project,environment,'migration-credential-save',request_id)
    payload={'values':values,'expected_revision':expected_revision,'credential_id':credential_id}
    cipher=bindings._cipher(key_path)
    def perform():
        bindings._revision(expected_revision);_values(values)
        identifier=bindings._id(credential_id) if credential_id is not None else uuid.uuid4().hex
        prior=_row(db,project,environment,identifier) if credential_id is not None else None
        if expected_revision!=(prior['revision'] if prior else 0):
            raise bindings.BindingError('Migration credential revision changed.')
        revision=expected_revision+1;now=time.time()
        document={'purpose':'database-migration','id':identifier,'project':project,
                  'environment':environment,'revision':revision,**values}
        ciphertext=cipher.encrypt(registry.canonical(document).encode())
        if prior:
            db.execute('UPDATE migration_credentials SET revision=?,enabled=1,ciphertext=?,updated=? WHERE id=?',
                       (revision,ciphertext,now,identifier))
        else:
            db.execute('INSERT INTO migration_credentials VALUES(?,?,?,?,?,?,?,?)',
                       (identifier,project,environment,revision,1,ciphertext,now,now))
        bindings._event(db,principal,project,environment,identifier,revision,'migration-credential-saved')
        return bindings._public_credential(_row(db,project,environment,identifier))
    return bindings._save(db,principal,project,environment,context,payload,perform,cipher=cipher,revalidate=revalidate)


def get_credential(db,principal,project,environment,credential_id,*,revalidate=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    return bindings._public_credential(_row(db,project,environment,credential_id))


def list_credentials(db,principal,project,environment,*,revalidate=None,limit=25,cursor=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    limit,cursor=bindings._page(limit,cursor)
    return [bindings._public_credential(row) for row in db.execute(
        'SELECT * FROM migration_credentials WHERE project=? AND environment=? AND id>? ORDER BY id LIMIT ?',
        (project,environment,cursor,limit))]


def revoke_credential(db,principal,project,environment,credential_id,*,expected_revision,revalidate=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate)
    bindings._revision(expected_revision);initialize(db)
    with registry.transaction(db):
        bindings._authorize(db,principal,project,environment,'databases:manage',revalidate)
        row=_row(db,project,environment,credential_id)
        if row['revision']!=expected_revision:raise bindings.BindingError('Migration credential revision changed.')
        db.execute('UPDATE migration_credentials SET revision=revision+1,enabled=0,updated=? WHERE id=?',
                   (time.time(),credential_id))
        bindings._event(db,principal,project,environment,credential_id,expected_revision+1,'migration-credential-revoked')
        return bindings._public_credential(_row(db,project,environment,credential_id))


def credential_for_use(db,project,environment,credential_id,revision,key_path):
    if environment not in registry.ENVIRONMENTS:raise bindings.BindingError('An exact environment is required.')
    bindings._revision(revision);initialize(db)
    row=_row(db,project,environment,credential_id)
    if not row['enabled'] or row['revision']!=revision:raise bindings.BindingError('Migration credential is disabled or changed.')
    try:
        document=json.loads(bindings._cipher(key_path).decrypt(row['ciphertext']))
        expected={'purpose':'database-migration','id':credential_id,'project':project,'environment':environment,'revision':revision}
        if not isinstance(document,dict) or set(document)!=set(expected)|{'username','password'} or any(document[k]!=v for k,v in expected.items()):raise ValueError()
        values={k:document[k] for k in ('username','password')};_values(values)
        return values
    except (InvalidToken,TypeError,ValueError,KeyError):
        raise bindings.BindingError('Migration credential cannot be resolved with its current identity and key.') from None
