"""Public environment workspace read model; never guesses runtime ownership."""
import json
import hashlib
import re
import time

import job_target
import registry
import provenance
from compose_identity import ordered_files

DISPLAY_FIELDS = ('ID', 'Names', 'Image', 'State', 'Status', 'CPUPerc', 'MemUsage', 'MemPerc', 'PIDs')


def _files(value):
    if isinstance(value, str):
        value = value.split(',')
    try:
        return ordered_files(value)
    except ValueError:
        return None


def _identity(row):
    if not isinstance(row, dict) or not isinstance(row.get('ID'), str) or not re.fullmatch('[0-9a-f]{64}', row['ID']):
        return None
    project, service = row.get('ComposeProject'), row.get('ComposeService')
    files = _files(row.get('ComposeConfigFiles'))
    if not isinstance(project, str) or not project or not isinstance(service, str) or not service or files is None:
        return None
    return project, service, files


def _recipe_matches(recipe, identity, require_service=True):
    if not isinstance(recipe, dict) or identity is None:
        return False
    if not require_service:
        # Legacy candidacy remains separate from verified environment membership.
        recipe = dict(recipe, owned_services=[identity[1]])
    return provenance.matches(recipe, identity[0], identity[1], list(identity[2]))


def _candidate(row, recipe, prefixes):
    """Scope missing observations; candidacy never grants service membership."""
    if not isinstance(row, dict):
        return False
    group = recipe.get('compose_project') if isinstance(recipe, dict) else None
    if group and row.get('ComposeProject') == group:
        return True
    name = row.get('Names')
    return isinstance(name, str) and any(isinstance(prefix, str) and prefix and name.startswith(prefix)
                                          for prefix in prefixes)


def _complete(observed, recipe, prefixes):
    return isinstance(observed, list) and all(_identity(row) is not None
        for row in observed if _candidate(row, recipe, prefixes))


def _snapshot(db, name):
    row = db.execute('SELECT updated,value FROM snapshots WHERE name=?', (name,)).fetchone()
    if row is None:
        return None, None
    try:
        return row['updated'], json.loads(row['value'])
    except (ValueError, TypeError):
        return row['updated'], None


def _bound_recipes(db):
    recipes = []
    for row in db.execute('''SELECT e.project,e.name,e.recipe_revision,r.document,r.digest
            FROM registry_environments e JOIN registry_recipes r ON r.id=e.recipe_revision AND r.project=e.project'''):
        try:
            recipe = json.loads(row['document'])
            if hashlib.sha256(registry.canonical(recipe).encode()).hexdigest() != row['digest']:
                continue
        except (TypeError, ValueError):
            continue
        if (isinstance(recipe, dict) and isinstance(recipe.get('compose_project'), str)
                and recipe['compose_project'] and _files(recipe.get('compose_files'))
                and isinstance(recipe.get('services'), list) and recipe['services']
                and all(isinstance(service, str) and service for service in recipe['services'])):
            recipes.append((row['project'], row['name'], recipe))
    return recipes


def _service(row, state, reason, manageable):
    result = {key: row.get(key) for key in DISPLAY_FIELDS}
    result['service'] = row.get('ComposeService')
    result.update(membership={'state': state, 'reason': reason},
                  capabilities={'manage': manageable, 'reason': '' if manageable else reason})
    return result


def _runs(db, project, environment):
    assigned, unassigned = [], []
    for row in db.execute('SELECT * FROM jobs WHERE project=? ORDER BY id DESC LIMIT 200', (project,)):
        result = dict(row)
        try:
            target = job_target.public(db, row['id'])
        except (ValueError, TypeError, KeyError):
            exists = db.execute('SELECT 1 FROM job_targets WHERE job_id=?', (row['id'],)).fetchone()
            result['target_state'] = 'invalid' if exists else 'unassigned'
            result['target'] = None
            unassigned.append(result)
            continue
        if target['project'] != project or any(target[field] != row[field] for field in ('branch', 'sha', 'mode')):
            result.update(target=None, target_state='invalid')
            unassigned.append(result)
        elif target['environment'] == environment:
            result.update(target=target, target_state='pinned')
            assigned.append(result)
        elif target['environment'] == 'legacy-unclassified':
            result.update(target=target, target_state='unassigned')
            unassigned.append(result)
    return assigned[:100], unassigned[:100]


def read(db, project, environment):
    registry.initialize(db)
    job_target.initialize(db)
    registry.identifier(project)
    if not isinstance(environment, str) or environment not in registry.ENVIRONMENTS:
        raise ValueError('Choose Development or Production.')
    owned_transaction = not db.in_transaction
    if owned_transaction:
        db.execute('BEGIN')
    try:
        record = registry.get_project(db, project)
        settings = record['environments'][environment]
        legacy_row = db.execute('SELECT legacy_document FROM registry_projects WHERE id=?', (project,)).fetchone()
        legacy = json.loads(legacy_row['legacy_document'] or '{}')
        updated, observed = _snapshot(db, 'containers')
        worker_updated, worker = _snapshot(db, 'worker')
        rows = observed if isinstance(observed, list) else []
        now = time.time()
        stale = (not updated or now - updated > 90 or not worker_updated or now - worker_updated > 90
                 or not isinstance(worker, dict) or worker.get('state') != 'running')
        bindings = _bound_recipes(db)
        selected_recipe = next((recipe for owner, name, recipe in bindings if (owner, name) == (project, environment)), None)
        prefixes = legacy.get('container_prefixes', [])
        complete = _complete(observed, selected_recipe, prefixes)
        legacy_complete = _complete(observed, legacy.get('recipe'), prefixes)
        services, unassigned = [], []
        conflicts = False
        observed_services = set()
        for row in rows:
            identity = _identity(row)
            owners = {(owner, name) for owner, name, recipe in bindings if _recipe_matches(recipe, identity)}
            legacy_candidate = _recipe_matches(legacy.get('recipe'), identity, require_service=False)
            if owners == {(project, environment)}:
                observed_services.add(identity[1])
                manageable = not stale and any(str(row.get('Names', '')).startswith(prefix) for prefix in legacy.get('container_prefixes', []))
                reason = 'Runtime observations are stale. Refresh before managing this service.' if stale else 'Persisted recipe and observed Compose ownership match.'
                if not manageable and not stale:
                    reason = 'The service is bound, but existing container control does not authorize this target.'
                services.append(_service(row, 'verified', reason, manageable))
            elif len(owners) > 1 and ((project, environment) in owners or legacy_candidate):
                conflicts = True
                unassigned.append(_service(row, 'conflict', 'Multiple environment records claim the same observed service. Reconcile ownership before management.', False))
            elif not owners and legacy_candidate:
                manageable = not stale and any(str(row.get('Names', '')).startswith(prefix) for prefix in legacy.get('container_prefixes', []))
                reason = 'Observed legacy Compose ownership matches this project, but no environment owns this service.'
                if stale:
                    reason = 'Legacy observations are stale. Refresh before managing this service.'
                unassigned.append(_service(row, 'unassigned', reason, manageable))
        missing = sorted(set(selected_recipe.get('owned_services', selected_recipe.get('services', []))) - observed_services) if selected_recipe else []
        if conflicts:
            state, reason = 'conflict', 'Service ownership conflicts require explicit reconciliation.'
        elif selected_recipe is None:
            state, reason = 'unassigned', 'No verified deployment recipe is bound to this environment. Existing legacy services remain separate.'
        elif not complete:
            state, reason = 'observation-incomplete', 'Full container IDs and Compose ownership observations are required before showing environment membership.'
        elif missing:
            state, reason = 'partial', 'Some registered services are missing from the current container observation.'
        else:
            state, reason = 'verified', 'The persisted recipe matches observed container ownership.'
        runs, unassigned_runs = _runs(db, project, environment)
        from delivery_target import readiness
        delivery = readiness(selected_recipe, services, {'stale': stale, 'ownership_complete': complete}, {'state': state}, settings)
        result = {
            'project': {key: record[key] for key in ('id', 'label', 'repository', 'revision')},
            'environment': settings,
            'binding': {'state': state, 'reason': reason, 'missing_services': missing},
            'observation': {'updated': updated, 'stale': stale, 'ownership_complete': complete},
            'services': services, 'unassigned_services': unassigned,
            'runs': runs, 'unassigned_runs': unassigned_runs,
            'legacy': {'present': record['legacy_settings'] is not None, 'binding': record['legacy_binding'],
                       'settings': record['legacy_settings'], 'inventory_available': legacy_complete},
            'capabilities': {'settings': {'enabled': True, 'reason': ''},
                             'services': {'enabled': bool(services), 'reason': '' if services else reason},
                             **delivery},
        }
        return result
    finally:
        if owned_transaction:
            db.execute('ROLLBACK')  # Read snapshot only; never persist bindings.
