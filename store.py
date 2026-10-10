"""Durable, bounded delivery queue. No credentials or webhook bodies are retained."""
import json
import re
import sqlite3
import time
from pathlib import Path


def connect(path):
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=15000')
    db.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY, delivery TEXT UNIQUE NOT NULL,
        project TEXT NOT NULL, sha TEXT NOT NULL, created REAL NOT NULL,
        updated REAL NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')''')
    db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
        name TEXT PRIMARY KEY, updated REAL NOT NULL, value TEXT NOT NULL)''')
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


def enqueue(db, delivery, project, sha, enabled):
    now = time.time()
    db.execute('BEGIN IMMEDIATE')
    try:
        old = db.execute('SELECT id FROM jobs WHERE delivery=?', (delivery,)).fetchone()
        if old:
            db.execute('COMMIT')
            return old['id'], False
        status = 'queued' if enabled else 'awaiting-validation'
        cursor = db.execute('INSERT INTO jobs(delivery,project,sha,created,updated,status) VALUES(?,?,?,?,?,?)',
                            (delivery, project, sha, now, now, status))
        db.execute("UPDATE jobs SET status='superseded',updated=? WHERE project=? AND status='queued' AND id<>?",
                   (now, project, cursor.lastrowid))
        # Bound metadata while preserving in-flight work and recent deduplication.
        db.execute("DELETE FROM jobs WHERE updated<? AND status NOT IN ('queued','building','deploying','recovering')",
                   (now - 30 * 86400,))
        db.execute('COMMIT')
        return cursor.lastrowid, True
    except BaseException:
        db.execute('ROLLBACK')
        raise


def set_status(db, job_id, status, detail=''):
    db.execute('UPDATE jobs SET status=?,detail=?,updated=? WHERE id=?',
               (status, detail[:1000], time.time(), job_id))


def snapshot(db, name, value):
    db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?)',
               (name, time.time(), json.dumps(value)))
