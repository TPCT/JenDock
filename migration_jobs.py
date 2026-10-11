"""Durable migration intent; no helper execution or implicit uncertain replay.

Call capture inside enqueue's transaction. Source/proof persistence precedes
external effects. Applying survives process loss as a reconciliation fence.
"""
from contextlib import nullcontext
import json
import re
import stat
import time
from pathlib import Path
import database_bindings as common
import migration_bindings as bindings
import registry

MAX_BYTES=16*1024*1024


def initialize(db):
    bindings.initialize(db)
    db.execute('CREATE TABLE IF NOT EXISTS job_migration_targets(job_id INTEGER PRIMARY KEY,document TEXT NOT NULL,digest TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS job_migrations(job_id INTEGER PRIMARY KEY,state TEXT NOT NULL,sources TEXT,source_digest TEXT,proof TEXT,proof_digest TEXT,evidence TEXT,updated REAL NOT NULL)')
    db.execute("CREATE TRIGGER IF NOT EXISTS job_migration_target_update BEFORE UPDATE ON job_migration_targets BEGIN SELECT RAISE(ABORT,'Migration targets are immutable'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS job_migration_target_replace BEFORE INSERT ON job_migration_targets WHEN EXISTS(SELECT 1 FROM job_migration_targets WHERE job_id=NEW.job_id) BEGIN SELECT RAISE(ABORT,'Migration targets are immutable'); END")
    # Old unconditional deletion protection prevented terminal metadata expiry.
    db.execute('DROP TRIGGER IF EXISTS job_migration_target_delete')
    db.execute('DROP TRIGGER IF EXISTS job_migration_target_retained_delete')
    db.execute("CREATE TRIGGER job_migration_target_retained_delete BEFORE DELETE ON job_migration_targets WHEN EXISTS(SELECT 1 FROM jobs WHERE id=OLD.job_id) OR NOT EXISTS(SELECT 1 FROM job_migrations WHERE job_id=OLD.job_id AND state IN ('not-configured','verified','failed')) BEGIN SELECT RAISE(ABORT,'Migration recovery evidence is retained'); END")


def capture(db,job_id,project,environment,helper_image):
    initialize(db)
    if type(job_id) is not int or job_id<1:raise ValueError('Invalid migration execution identity.')
    with (nullcontext() if db.in_transaction else registry.transaction(db)):
        binding=bindings.capture(db,project,environment)
        if binding is not None and (not isinstance(helper_image,str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',helper_image)):raise ValueError('Migration helper image is not configured.')
        document={'version':1,'job_id':job_id,'project':project,'environment':environment,'helper_image':helper_image if binding else None,'binding':binding}
        row=db.execute('SELECT document,digest FROM job_migration_targets WHERE job_id=?',(job_id,)).fetchone()
        if row:
            if row['document']!=registry.canonical(document) or row['digest']!=common._digest(document):raise ValueError('Migration target changed.')
        else:
            db.execute('INSERT INTO job_migration_targets VALUES(?,?,?)',(job_id,registry.canonical(document),common._digest(document)))
            db.execute('INSERT INTO job_migrations(job_id,state,updated) VALUES(?,?,?)',(job_id,'captured' if document['binding'] else 'not-configured',time.time()))
    return document


def load(db,job_id):
    initialize(db)
    row=db.execute('SELECT * FROM job_migrations WHERE job_id=?',(job_id,)).fetchone()
    saved=db.execute('SELECT document,digest FROM job_migration_targets WHERE job_id=?',(job_id,)).fetchone()
    if row is None or saved is None:raise ValueError('Migration target is unavailable.')
    try:
        target=json.loads(saved['document'])
        if common._digest(target)!=saved['digest'] or target['job_id']!=job_id:raise ValueError()
        result=dict(row,target=target)
        for key in ('sources','proof','evidence'):
            result[key]=json.loads(row[key]) if row[key] is not None else None
        for key,digest in (('sources','source_digest'),('proof','proof_digest')):
            if result[key] is not None and common._digest(result[key])!=row[digest]:raise ValueError()
        return result
    except (ValueError,TypeError,KeyError):raise ValueError('Migration journal integrity is invalid.') from None


def optional(db,job_id):
    initialize(db)
    return load(db,job_id) if db.execute('SELECT 1 FROM job_migration_targets WHERE job_id=?',(job_id,)).fetchone() else None


def authorize(db,job_id):
    record=load(db,job_id)
    if record['target']['binding'] is None:raise ValueError('No migration configuration was captured.')
    bindings.validate_capture(db,record['target']['binding'])
    return record


def _sources(sources,definition):
    suffix='.py' if definition['framework']=='alembic' else '.php'
    if not isinstance(sources,dict) or not 0<=len(sources)<=1000:raise ValueError('Migration source count is invalid.')
    total=0
    for name,value in sources.items():
        if (not isinstance(name,str) or len(name)>512 or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*',name)
                or any(p in ('.','..','.git') for p in name.split('/')) or not name.endswith(suffix)
                or not any(name.startswith(p+'/') for p in definition['migration_paths']) or not isinstance(value,str) or '\x00' in value):raise ValueError('Migration source is outside the configured contract.')
        total+=len(value.encode('utf-8'))
        if total>MAX_BYTES:raise ValueError('Migration sources exceed their bound.')
    return sources


def collect_sources(root,definition):
    root=Path(root).resolve(strict=True);result={}
    for directory in definition['migration_paths']:
        current=root
        for part in directory.split('/'):
            current=current/part
            if current.is_symlink() or not current.is_dir():raise ValueError('Migration directory is missing or unsafe.')
        for file in sorted(current.rglob('*')):
            info=file.lstat()
            if stat.S_ISLNK(info.st_mode):raise ValueError('Migration source links are unsupported.')
            if stat.S_ISDIR(info.st_mode):continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Migration source is not a regular owned file.')
            suffix='.py' if definition['framework']=='alembic' else '.php'
            if file.suffix!=suffix:continue
            if info.st_size>MAX_BYTES:raise ValueError('Migration source exceeds its bound.')
            try:result[file.relative_to(root).as_posix()]=file.read_text(encoding='utf-8')
            except (OSError,UnicodeError):raise ValueError('Migration source could not be read.') from None
            _sources(result,definition)
    return _sources(result,definition)


def save_sources(db,job_id,sources):
    with registry.transaction(db):
        record=authorize(db,job_id);_sources(sources,record['target']['binding']['definition'])
        if record['sources'] is not None:
            if record['sources']!=sources:raise ValueError('Pinned migration source changed.')
            return
        if record['state']!='captured':raise ValueError('Migration source cannot change in this state.')
        db.execute("UPDATE job_migrations SET state='source-ready',sources=?,source_digest=?,updated=? WHERE job_id=?",(registry.canonical(sources),common._digest(sources),time.time(),job_id))


def record_plan(db,job_id,proof):
    # Proof semantics are validated by the trusted helper; this layer preserves
    # its exact bytes/digest and refuses replacement after any external intent.
    if not isinstance(proof,dict) or len(registry.canonical(proof).encode())>MAX_BYTES:raise ValueError('Migration proof is invalid or oversized.')
    with registry.transaction(db):
        record=authorize(db,job_id)
        if record['proof'] is not None:
            if record['proof']!=proof:raise ValueError('Migration proof cannot be replaced.')
            return
        if record['state']!='source-ready':raise ValueError('Migration sources are not ready.')
        db.execute("UPDATE job_migrations SET state='planned',proof=?,proof_digest=?,updated=? WHERE job_id=?",(registry.canonical(proof),common._digest(proof),time.time(),job_id))


def begin_apply(db,job_id):
    with registry.transaction(db):
        record=authorize(db,job_id)
        if record['state']!='planned':raise ValueError('Migration execution requires reconciliation, not replay.')
        db.execute("UPDATE job_migrations SET state='applying',updated=? WHERE job_id=?",(time.time(),job_id))
        return record


def mark_uncertain(db,job_id):
    with registry.transaction(db):
        record=load(db,job_id)
        if record['state'] not in ('applying','recovery-required'):raise ValueError('Migration execution has no uncertain intent.')
        db.execute("UPDATE job_migrations SET state='recovery-required',updated=? WHERE job_id=?",(time.time(),job_id))
