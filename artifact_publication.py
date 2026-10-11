"""Durable, atomic catalog publication after a verified successful build."""
import copy
import json
import re
import time
import uuid

import managed_services as services
import registry
from store import set_status


def initialize(db):
    services.initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS artifact_publications(
        job_id INTEGER PRIMARY KEY, image_refs TEXT NOT NULL, recipe_digest TEXT NOT NULL,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        attempted REAL, detail TEXT NOT NULL)''')


def pending(db, job_id):
    initialize(db)
    return db.execute("SELECT 1 FROM artifact_publications WHERE job_id=? AND status!='succeeded'", (job_id,)).fetchone() is not None


def record(db, job_id, recipe, images, *, status=None, detail=None):
    """Commit successful source state and retry intent together, before catalog IO."""
    initialize(db)
    with registry.transaction(db):
        if status is not None:
            if status not in ('validated', 'succeeded', 'deploying'): raise ValueError('Invalid successful source state.')
            if status == 'deploying' and recipe.get('schema_kind') != 'managed-build':
                raise ValueError('Only a pinned managed build can await runtime verification.')
            set_status(db, job_id, status, detail or 'Verified build artifact publication recorded.')
        refs, fingerprint = registry.canonical(images), services.digest(recipe)
        old = db.execute('SELECT * FROM artifact_publications WHERE job_id=?', (job_id,)).fetchone()
        if old:
            if old['image_refs'] != refs or old['recipe_digest'] != fingerprint:
                raise ValueError('Publication evidence differs from the recorded build.')
        else:
            db.execute('INSERT INTO artifact_publications(job_id,image_refs,recipe_digest,status,detail) VALUES(?,?,?,?,?)',
                       (job_id, refs, fingerprint, 'pending', 'Service artifact publication is pending.'))


def process(db, inspect, job_id=None, *, verify_role=None):
    """Resolve all images before one atomic batch; failures remain durable/retryable."""
    if db.in_transaction:raise ValueError('Artifact verification cannot run inside a transaction.')
    initialize(db)
    rows = db.execute("SELECT * FROM artifact_publications WHERE status='pending'" +
                      (' AND job_id=?' if job_id is not None else ' AND (attempted IS NULL OR attempted<?)'),
                      (job_id,) if job_id is not None else (time.time()-30,)).fetchall()
    for row in rows:
        identifier = row['job_id']
        db.execute('UPDATE artifact_publications SET attempts=attempts+1,attempted=? WHERE job_id=?', (time.time(), identifier))
        try:
            target = services._source(db, identifier)
            if services.digest(target['recipe']) != row['recipe_digest']: raise ValueError('Recipe mismatch')
            profiles = target['recipe'].get('service_profiles', {})
            if not isinstance(profiles, dict): raise ValueError('Invalid profiles')
            refs = json.loads(row['image_refs']); documents = []
            for template in profiles:
                profile = services._profile(target, template)
                image = inspect(refs[profile['image_name']])
                if not isinstance(image, str) or not re.fullmatch('sha256:[a-f0-9]{64}', image): raise ValueError('Invalid image')
                documents.append({'project': target['project'], 'source_job': identifier,
                    'recipe_revision': target['recipe_revision'], 'source_sha': target['sha'],
                    'source_environment': target['environment'], 'target_fingerprint': target['fingerprint'],
                    'template_id': template, 'template_revision': profile['revision'],
                    'image_name': profile['image_name'], 'image': image, 'command': profile['command'],
                    'healthcheck': profile['healthcheck'], 'user': profile['user'],
                    **({'http_endpoint': copy.deepcopy(profile['http_endpoint'])} if 'http_endpoint' in profile else {}),
                    **({'workload':copy.deepcopy(profile['workload'])} if 'workload' in profile else {})})
            evidence={}
            for document in documents:
                if 'workload' in document:
                    if not callable(verify_role):raise ValueError('Trusted role verification is required.')
                    observed=verify_role(copy.deepcopy(document))
                    services._validate_role_evidence(document,observed)
                    evidence[services.digest(document)]=copy.deepcopy(observed)
            with registry.transaction(db):
                if services._source(db, identifier)!=target: raise ValueError('Source mismatch')
                current=db.execute('SELECT * FROM artifact_publications WHERE job_id=?',(identifier,)).fetchone()
                if current['image_refs']!=row['image_refs'] or current['recipe_digest']!=row['recipe_digest']:raise ValueError('Publication intent changed')
                if current['status']=='succeeded':continue
                if current['status']!='pending':raise ValueError('Publication state changed')
                for document in documents:
                    fingerprint = services.digest(document)
                    db.execute('INSERT OR IGNORE INTO service_artifacts VALUES(?,?,?,?,?,?)',
                               (str(uuid.uuid4()), target['project'], identifier, registry.canonical(document), fingerprint, time.time()))
                    saved=db.execute('SELECT * FROM service_artifacts WHERE project=? AND digest=?',(target['project'],fingerprint)).fetchone()
                    if not saved or saved['document']!=registry.canonical(document) or saved['job_id']!=identifier:raise ValueError('Artifact identity differs')
                    if 'workload' in document:
                        services._insert_role_evidence(db,dict(document,id=saved['id'],digest=fingerprint),evidence[fingerprint])
                db.execute("UPDATE artifact_publications SET status='succeeded',detail=? WHERE job_id=?",
                           ('All service artifacts were published.', identifier))
                db.execute('INSERT INTO events(job_id,created,status,detail) VALUES(?,?,?,?)',
                           (identifier, time.time(), 'artifacts-published', 'All service artifacts were published.'))
        except Exception:
            # Never persist upstream output, image configuration or exception values.
            detail = 'Service artifact publication failed; automatic retry is pending.'
            with registry.transaction(db):
                previous = db.execute('SELECT status,detail FROM artifact_publications WHERE job_id=?', (identifier,)).fetchone()
                if previous['status']=='succeeded':continue
                db.execute("UPDATE artifact_publications SET detail=? WHERE job_id=?", (detail, identifier))
                if previous['detail'] != detail:
                    db.execute('INSERT INTO events(job_id,created,status,detail) VALUES(?,?,?,?)',
                               (identifier, time.time(), 'artifacts-pending', detail))
    return not (job_id is not None and pending(db, job_id))
