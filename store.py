"""Durable, bounded delivery queue. No credentials or webhook bodies are retained."""
import json
import re
import sqlite3
import time
from pathlib import Path


def connect(path):
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    for attempt in range(20):
        try:
            db.execute('PRAGMA journal_mode=WAL')
            break
        except sqlite3.OperationalError as error:
            if 'locked' not in str(error).lower() or attempt == 19:
                db.close()
                raise
            time.sleep(0.05)
    db.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY, delivery TEXT UNIQUE NOT NULL,
        project TEXT NOT NULL, sha TEXT NOT NULL, created REAL NOT NULL,
        updated REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')''')
    db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
        name TEXT PRIMARY KEY, updated REAL NOT NULL, value TEXT NOT NULL)''')
    db.execute('BEGIN IMMEDIATE')
    try:
        columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
        for name, definition in [('branch', "TEXT NOT NULL DEFAULT ''"), ('mode', "TEXT NOT NULL DEFAULT 'deploy'"), ('actor', "TEXT NOT NULL DEFAULT 'github'"), ('retry_of', 'INTEGER')]:
            if name not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + definition)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    db.executescript("""
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, job_id INTEGER, created REAL, status TEXT, detail TEXT);
      CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, csrf TEXT NOT NULL, username TEXT NOT NULL, created REAL, touched REAL);
      CREATE TABLE IF NOT EXISTS settings(project TEXT PRIMARY KEY, branch TEXT NOT NULL, automatic INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS requests(project TEXT PRIMARY KEY, created REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS control_runs(id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, created REAL, updated REAL, actor TEXT, project TEXT, container TEXT, operation TEXT, status TEXT, detail TEXT);
      CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, created REAL, actor TEXT, action TEXT, project TEXT, detail TEXT);
    """)
    return db


def projects(path):
    data = json.loads(Path(path).read_text())
    for name, p in data.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
            raise ValueError('Invalid project identifier')
        if not re.fullmatch(r'TPCT/[A-Za-z0-9_.-]+', p['repository']):
            raise ValueError('Repository outside approved organization')
        if not p['branch'] or any(c in p['branch'] for c in '\n\r\x00'):
            raise ValueError('Invalid branch')
    return data


def enqueue(db, delivery, project, sha, enabled, branch='', mode='deploy', actor='github', retry_of=None):
    now = time.time()
    db.execute('BEGIN IMMEDIATE')
    try:
        old = db.execute('SELECT id FROM jobs WHERE delivery=?', (delivery,)).fetchone()
        if old:
            db.execute('COMMIT')
            return old['id'], False
        if actor != 'github' and db.execute("SELECT id FROM jobs WHERE project=? AND status IN ('queued','validation-queued','building','testing','deploying','recovering')",(project,)).fetchone():
            raise ValueError('This project already has an active or queued run.')
        status = ('validation-queued' if mode == 'validate' else 'queued') if enabled else 'awaiting-validation'
        cursor = db.execute('INSERT INTO jobs(delivery,project,sha,created,updated,status,branch,mode,actor,retry_of) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (delivery, project, sha, now, now, status, branch, mode, actor, retry_of))
        if actor == 'github':
            db.execute("UPDATE jobs SET status='superseded',updated=? WHERE project=? AND branch=? AND actor='github' AND status='queued' AND id<>?", (now, project, branch, cursor.lastrowid))
        # Bound metadata while preserving in-flight work and recent deduplication.
        db.execute("DELETE FROM jobs WHERE updated<? AND status NOT IN ('queued','building','deploying','recovering')",
                   (now - 30 * 86400,))
        db.execute('DELETE FROM events WHERE job_id NOT IN (SELECT id FROM jobs)')
        db.execute('COMMIT')
        return cursor.lastrowid, True
    except BaseException:
        db.execute('ROLLBACK')
        raise


def set_status(db, job_id, status, detail=''):
    db.execute('INSERT INTO events(job_id,created,status,detail) VALUES(?,?,?,?)', (job_id,time.time(),status,detail[:1000]))
    db.execute('UPDATE jobs SET status=?,detail=?,updated=? WHERE id=?',
               (status, detail[:1000], time.time(), job_id))


def snapshot(db, name, value):
    db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)',
               (name, time.time(), json.dumps(value)))


def effective_projects(db, path):
    data = projects(path)
    for row in db.execute('SELECT * FROM settings'):
        if row['project'] in data:
            p = data[row['project']]
            p['branch'] = row['branch']
            p['automatic'] = bool(row['automatic']) and p.get('enabled', False)
    for p in data.values():
        p.setdefault('automatic', p.get('enabled', False))
    return data


def audit(db, actor, action, project='', detail=''):
    db.execute('INSERT INTO audit(created,actor,action,project,detail) VALUES(?,?,?,?,?)',
               (time.time(),actor,action,project,detail[:1000]))
    db.execute('DELETE FROM audit WHERE created<?', (time.time()-90*86400,))


def valid_branch(value):
    return isinstance(value,str) and 0 < len(value) <= 200 and not value.startswith(('-', '/', '.')) and not value.endswith(('/', '.', '.lock')) and not any(x in value for x in ('..', '@{', '//', '\\')) and not re.search(r'[\x00-\x20\x7f~^:?*\[]',value)
