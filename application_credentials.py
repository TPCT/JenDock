"""Versioned application accounts, separate from inspection and migration accounts.

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
    db.execute('''CREATE TABLE IF NOT EXISTS application_credentials(
        id TEXT PRIMARY KEY,project TEXT NOT NULL,environment TEXT NOT NULL,
        revision INTEGER NOT NULL,enabled INTEGER NOT NULL,minimum_revision INTEGER NOT NULL,
        created REAL NOT NULL,updated REAL NOT NULL)''')
    db.execute('CREATE TABLE IF NOT EXISTS application_credential_revisions(id TEXT NOT NULL,revision INTEGER NOT NULL,ciphertext BLOB NOT NULL,created REAL NOT NULL,PRIMARY KEY(id,revision))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS application_credential_revision_{action.lower()} BEFORE {action} ON application_credential_revisions BEGIN SELECT RAISE(ABORT,'Application credential revisions are immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS application_credential_revision_replace BEFORE INSERT ON application_credential_revisions WHEN EXISTS(SELECT 1 FROM application_credential_revisions WHERE id=NEW.id AND revision=NEW.revision) BEGIN SELECT RAISE(ABORT,'Application credential revisions are immutable'); END")


def _row(db,project,environment,identifier):
    row=db.execute('SELECT * FROM application_credentials WHERE id=? AND project=? AND environment=?',
                   (bindings._id(identifier),project,environment)).fetchone()
    if row is None:raise bindings.BindingError('Application credential is unavailable in this environment.')
    return row


def _values(values):
    if (not isinstance(values,dict) or set(values)!={'username','password'}
            or any(not isinstance(values[k],str) or not 1<=len(values[k])<=4096 or '\x00' in values[k] for k in values)
            or len(values['username'])>128 or any(ord(c)<32 for c in values['username'])):
        raise bindings.BindingError('A bounded application username and password are required.')


def save_credential(db,principal,project,environment,values,*,expected_revision,key_path,
                    credential_id=None,revalidate=None,request_id=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate)
    initialize(db)
    context=bindings._request_context(principal,project,environment,'application-credential-save',request_id)
    payload={'values':values,'expected_revision':expected_revision,'credential_id':credential_id}
    cipher=bindings._cipher(key_path)
    def perform():
        bindings._revision(expected_revision);_values(values)
        identifier=bindings._id(credential_id) if credential_id is not None else uuid.uuid4().hex
        prior=_row(db,project,environment,identifier) if credential_id is not None else None
        if expected_revision!=(prior['revision'] if prior else 0):
            raise bindings.BindingError('Application credential revision changed.')
        revision=expected_revision+1;now=time.time()
        document={'purpose':'database-application','id':identifier,'project':project,
                  'environment':environment,'revision':revision,**values}
        ciphertext=cipher.encrypt(registry.canonical(document).encode())
        if prior:
            db.execute('UPDATE application_credentials SET revision=?,enabled=1,updated=? WHERE id=?',
                       (revision,now,identifier))
        else:
            db.execute('INSERT INTO application_credentials VALUES(?,?,?,?,?,?,?,?)',
                       (identifier,project,environment,revision,1,1,now,now))
        db.execute('INSERT INTO application_credential_revisions VALUES(?,?,?,?)',(identifier,revision,ciphertext,now))
        bindings._event(db,principal,project,environment,identifier,revision,'application-credential-saved')
        return bindings._public_credential(_row(db,project,environment,identifier))
    return bindings._save(db,principal,project,environment,context,payload,perform,cipher=cipher,revalidate=revalidate)


def get_credential(db,principal,project,environment,credential_id,*,revalidate=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    return bindings._public_credential(_row(db,project,environment,credential_id))


def list_credentials(db,principal,project,environment,*,revalidate=None,limit=25,cursor=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate);initialize(db)
    limit,cursor=bindings._page(limit,cursor)
    return [bindings._public_credential(row) for row in db.execute(
        'SELECT * FROM application_credentials WHERE project=? AND environment=? AND id>? ORDER BY id LIMIT ?',
        (project,environment,cursor,limit))]


def revoke_credential(db,principal,project,environment,credential_id,*,expected_revision,revalidate=None):
    bindings._authorize(db,principal,project,environment,'databases:manage',revalidate)
    bindings._revision(expected_revision);initialize(db)
    with registry.transaction(db):
        bindings._authorize(db,principal,project,environment,'databases:manage',revalidate)
        row=_row(db,project,environment,credential_id)
        if row['revision']!=expected_revision:raise bindings.BindingError('Application credential revision changed.')
        db.execute('UPDATE application_credentials SET minimum_revision=revision+1,revision=revision+1,enabled=0,updated=? WHERE id=?',
                   (time.time(),credential_id))
        bindings._event(db,principal,project,environment,credential_id,expected_revision+1,'application-credential-revoked')
        return bindings._public_credential(_row(db,project,environment,credential_id))


def _resolve(db,project,environment,credential_id,revision,key_path,*,historical,stopping=False):
    if environment not in registry.ENVIRONMENTS:raise bindings.BindingError('An exact environment is required.')
    bindings._revision(revision);initialize(db)
    row=_row(db,project,environment,credential_id)
    if not stopping and (not row['enabled'] or revision<row['minimum_revision'] or revision>row['revision']
            or (not historical and row['revision']!=revision)):
        raise bindings.BindingError('Application credential is disabled or changed.')
    saved=db.execute('SELECT ciphertext FROM application_credential_revisions WHERE id=? AND revision=?',(credential_id,revision)).fetchone()
    if saved is None:raise bindings.BindingError('Application credential revision is unavailable.')
    try:
        document=json.loads(bindings._cipher(key_path).decrypt(saved['ciphertext']))
        expected={'purpose':'database-application','id':credential_id,'project':project,'environment':environment,'revision':revision}
        if not isinstance(document,dict) or set(document)!=set(expected)|{'username','password'} or any(document[k]!=v for k,v in expected.items()):raise ValueError()
        values={k:document[k] for k in ('username','password')};_values(values)
        return values,dict(row)
    except (InvalidToken,TypeError,ValueError,KeyError):
        raise bindings.BindingError('Application credential cannot be resolved with its accepted identity and key.') from None


def credential_for_selection(db,project,environment,credential_id,revision,key_path):
    """Internal new-selection resolver: only the enabled current head is eligible.

    Caller owns current binding/principal authorization. Never expose this through
    a public response or pass the values to candidate application build processes.
    """
    return _resolve(db,project,environment,credential_id,revision,key_path,historical=False)[0]


def credential_for_runtime(db,project,environment,credential_id,revision,key_path,*,authorize):
    """Resolve an already accepted service revision for startup/rollback only.

    Trusted callback must recheck the exact service attachment, environment,
    credential id/revision, current authority, and permitted lifecycle operation.
    Rotation preserves accepted revisions; revocation invalidates them permanently.
    This does not revoke credentials already delivered to running containers or
    change the external database account: lifecycle enforcement remains required.
    """
    if not callable(authorize):raise bindings.BindingError('Current runtime authority is required.')
    authorize()
    values,head=_resolve(db,project,environment,credential_id,revision,key_path,historical=True)
    authorize()
    if dict(_row(db,project,environment,credential_id))!=head:
        raise bindings.BindingError('Application credential authority changed during resolution.')
    return values
