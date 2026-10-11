"""Durable cleanup work; retries never rerun a build or deployment."""
import json
import re
import time
import uuid

KINDS=frozenset({'job-images','previous-images','build-cache','legacy-images'})

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS image_cleanup(
        job_id INTEGER NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
        lease TEXT,expires REAL,detail TEXT NOT NULL DEFAULT '',images TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(job_id,kind))''')

def enqueue(db,job_id,kind,*,images=()):
    if type(job_id) is not int or job_id<1 or kind not in KINDS:raise ValueError('Invalid cleanup request')
    if not isinstance(images,(tuple,list)) or len(images)>1000 or any(not isinstance(i,str) or not re.fullmatch('sha256:[a-f0-9]{64}',i) for i in images):raise ValueError('Invalid cleanup identity')
    if images and kind!='legacy-images':raise ValueError('Unexpected cleanup identity')
    initialize(db)
    document=json.dumps(sorted(set(images)))
    db.execute("INSERT OR IGNORE INTO image_cleanup(job_id,kind,state,images) VALUES(?,?,'pending',?)",(job_id,kind,document))
    old=db.execute('SELECT images FROM image_cleanup WHERE job_id=? AND kind=?',(job_id,kind)).fetchone()
    if old['images']!=document:raise ValueError('Cleanup request identity changed')

def process_once(db,callbacks,*,job_id=None,kind=None,now=None):
    if db.in_transaction:raise ValueError('Cleanup cannot run inside a database transaction')
    initialize(db)
    now=time.time() if now is None else now
    row=db.execute('''SELECT * FROM image_cleanup WHERE
        ((state='pending' AND next_attempt<=?) OR (state='running' AND expires<=?))
        AND (? IS NULL OR job_id=?) AND (? IS NULL OR kind=?)
        ORDER BY next_attempt,job_id,kind LIMIT 1''',(now,now,job_id,job_id,kind,kind)).fetchone()
    if not row:return False
    token=uuid.uuid4().hex
    changed=db.execute('''UPDATE image_cleanup SET state='running',lease=?,expires=?,attempts=attempts+1
        WHERE job_id=? AND kind=? AND ((state='pending' AND next_attempt<=?) OR (state='running' AND expires<=?))''',
        (token,now+1800,row['job_id'],row['kind'],now,now)).rowcount
    if not changed:return False
    # Caller serializes Docker effects under the same worker control lock.
    # No SQL transaction spans external commands.
    try:
        complete=callbacks[row['kind']](db,row['job_id'])
        if complete is False:raise ValueError('Deferred cleanup')
    except Exception:
        delay=min(300,30*2**min(row['attempts'],4))
        db.execute("UPDATE image_cleanup SET state='pending',next_attempt=?,lease=NULL,expires=NULL,detail=? WHERE job_id=? AND kind=? AND lease=?",
                   (time.time()+delay,'Unused image/cache cleanup is pending; deployment status is unchanged.',row['job_id'],row['kind'],token))
    else:
        db.execute("UPDATE image_cleanup SET state='succeeded',lease=NULL,expires=NULL,detail=? WHERE job_id=? AND kind=? AND lease=?",
                   ('Unused image/cache cleanup completed.',row['job_id'],row['kind'],token))
    return True


def reconcile_terminal_jobs(db):
    """Recover the success/finally crash gap using only exact job-labelled outputs."""
    initialize(db)
    rows=db.execute('''SELECT j.id,j.status FROM jobs j WHERE j.status IN
        ('succeeded','validated','failed','interrupted') AND
        (NOT EXISTS(SELECT 1 FROM image_cleanup c WHERE c.job_id=j.id AND c.kind='job-images')
         OR NOT EXISTS(SELECT 1 FROM image_cleanup c WHERE c.job_id=j.id AND c.kind='build-cache')
         OR (j.status='succeeded' AND NOT EXISTS(SELECT 1 FROM image_cleanup c WHERE c.job_id=j.id AND c.kind='previous-images')))
        ORDER BY j.id LIMIT 20''').fetchall()
    for row in rows:
        enqueue(db,row['id'],'job-images')
        enqueue(db,row['id'],'build-cache')
        if row['status']=='succeeded':enqueue(db,row['id'],'previous-images')
    return len(rows)
