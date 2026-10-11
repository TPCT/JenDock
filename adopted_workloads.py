"""Immutable retained ownership of adopted workloads beside managed applications.

Retention uses the original verified adoption recipe, never a caller marker or a
recipe inferred after an environment has switched to managed application files.
"""
import json
import registry
import hashlib


def digest(value):return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS adopted_workload_owners(project TEXT NOT NULL,environment TEXT NOT NULL,recipe_revision INTEGER NOT NULL,recipe_digest TEXT NOT NULL,adoption_fingerprint TEXT NOT NULL,PRIMARY KEY(project,environment))')
    for action in ('UPDATE','DELETE'):
        db.execute(f"CREATE TRIGGER IF NOT EXISTS adopted_owner_{action.lower()} BEFORE {action} ON adopted_workload_owners BEGIN SELECT RAISE(ABORT,'Adopted owners are immutable'); END")


def _evidence(db,project,environment):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workload_adoptions'").fetchone():raise ValueError('Verified adoption is unavailable.')
    row=db.execute('SELECT * FROM workload_adoptions WHERE project=?',(project,)).fetchone()
    try:
        evidence=json.loads(row['evidence'])
        if row['environment']!=environment or evidence['project']!=project or evidence['environment']!=environment or evidence['fingerprint']!=row['fingerprint'] or digest({k:v for k,v in evidence.items() if k!='fingerprint'})!=row['fingerprint']:raise ValueError()
        return evidence
    except (ValueError,TypeError,KeyError):raise ValueError('Adopted ownership evidence is invalid.') from None


def _recipe(db,project,identifier):
    row=db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?',(identifier,project)).fetchone()
    try:
        recipe=json.loads(row['document'])
        if not isinstance(recipe,dict) or digest(recipe)!=row['digest']:raise ValueError()
        return recipe
    except (ValueError,TypeError,KeyError):raise ValueError('Adopted ownership recipe integrity is invalid.') from None


def _expected(db,project,evidence):
    row=db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?',(project,evidence.get('recipe_digest'))).fetchone()
    if row is None:raise ValueError('The original adoption recipe is unavailable.')
    recipe=_recipe(db,project,row['id'])
    recipe['owned_services']=sorted(r['service'] for r in evidence['containers'])
    if evidence.get('service_provenance'):recipe['service_provenance']=evidence['service_provenance']
    return recipe


def get(db,project,environment):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adopted_workload_owners'").fetchone():return None
    row=db.execute('SELECT * FROM adopted_workload_owners WHERE project=? AND environment=?',(project,environment)).fetchone()
    if row is None:return None
    evidence=_evidence(db,project,environment);recipe=_recipe(db,project,row['recipe_revision'])
    if evidence['fingerprint']!=row['adoption_fingerprint'] or digest(recipe)!=row['recipe_digest'] or recipe!=_expected(db,project,evidence):raise ValueError('Retained adopted ownership changed.')
    return dict(row,recipe=recipe,revision=registry.get_environment(db,project,environment)['revision'])


def current_adoption(db,project,environment):
    """Prove the currently registered recipe is exactly the adopted recipe."""
    evidence=_evidence(db,project,environment);env=registry.get_environment(db,project,environment)
    recipe=_recipe(db,project,env['recipe_revision'])
    if recipe!=_expected(db,project,evidence):raise ValueError('Current recipe is not the verified adopted workload.')
    return {'project':project,'environment':environment,'recipe_revision':env['recipe_revision'],'recipe_digest':digest(recipe),'adoption_fingerprint':evidence['fingerprint'],'recipe':recipe,'revision':env['revision']}


def retain(db,project,environment):
    if not db.in_transaction:raise ValueError('Ownership retention requires an atomic registry operation.')
    initialize(db)
    old=get(db,project,environment)
    if old is not None:return old
    current=current_adoption(db,project,environment)
    db.execute('INSERT INTO adopted_workload_owners VALUES(?,?,?,?,?)',tuple(current[k] for k in ('project','environment','recipe_revision','recipe_digest','adoption_fingerprint')))
    return get(db,project,environment)


def database_only(db,project,environment):
    try:
        owner=get(db,project,environment) or current_adoption(db,project,environment)
        evidence=_evidence(db,project,environment)
        rows=evidence['containers'];databases=evidence['databases']
        ids={r['id'] for r in rows};services={r['service'] for r in rows}
        proven={r['container_id'] for r in databases if r.get('engine') in ('pgsql','postgres','postgresql','mysql','mariadb') and r.get('storage')}
        return bool(ids) and len(ids)==len(rows) and len(services)==len(rows) and ids<=proven and set(owner['recipe'].get('owned_services',[]))==services
    except (ValueError,KeyError,TypeError):return False


def all_owners(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adopted_workload_owners'").fetchone():return []
    return [get(db,r['project'],r['environment']) for r in db.execute('SELECT project,environment FROM adopted_workload_owners')]
