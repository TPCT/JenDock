"""CAS advancement of private provenance after an authorized runtime mutation.

Prepared transitions are journal serializable and contain no recipe/environment
values. Callers restore runtime before rolling back this registry transition.
"""
import copy
import hashlib
import json
import time

import registry
from provenance import ProvenanceError, validate, validate_proof
from compose_identity import ordered_files
import re


def _digest(value):
    return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


def _state(db, project, environment):
    row = db.execute('SELECT p.revision AS project_revision,e.revision AS environment_revision,e.recipe_revision '
                     'FROM registry_projects p JOIN registry_environments e ON e.project=p.id '
                     'WHERE p.id=? AND e.name=?', (project, environment)).fetchone()
    if row is None:
        raise ProvenanceError('The provenance environment binding is unavailable.')
    return dict(row)


def prepare(db, project, recipe, resolver, *, recreated=None):
    entries = recipe.get('service_provenance') if isinstance(recipe, dict) else None
    if entries is None or entries == {}:
        return None
    if not isinstance(entries, dict):
        raise ProvenanceError('Compose provenance mapping is invalid.')
    recreated = {} if recreated is None else recreated
    if not isinstance(recreated, dict) or set(recreated) - set(recipe.get('owned_services', recipe.get('services', []))):
        raise ProvenanceError('Recreated services are outside the deployment recipe.')
    for service, actual in recreated.items():
        try:
            labels = actual['Config']['Labels']
            if (not re.fullmatch('[a-f0-9]{64}', actual['Id'])
                    or labels['com.docker.compose.project'] != recipe['compose_project']
                    or labels['com.docker.compose.service'] != service
                    or ordered_files(labels['com.docker.compose.project.config_files'].split(',')) != ordered_files(recipe['compose_files'])):
                raise ValueError()
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ProvenanceError('Recreated container identity does not match the canonical invocation.') from None
    rebased = sorted(set(recreated) & set(entries))
    digest = _digest(recipe)
    rows = db.execute('SELECT e.name,e.recipe_revision,e.revision AS environment_revision,p.revision AS project_revision '
                      'FROM registry_environments e JOIN registry_projects p ON p.id=e.project '
                      'JOIN registry_recipes r ON r.id=e.recipe_revision AND r.project=e.project '
                      'WHERE e.project=? AND r.digest=?', (project, digest)).fetchall()
    if len(rows) != 1:
        raise ProvenanceError('Exactly one current environment must own the reviewed recipe.')
    current = dict(rows[0]); proofs = {}
    for service in entries:
        old = validate(recipe, service)
        try:
            expected_files = recipe['compose_files'] if service in rebased else old['observed_files']
            result = resolver(copy.deepcopy(recipe), service, list(expected_files))
            if isinstance(result, dict) and 'proof' in result:
                if result.get('equivalent') is not True or result.get('mismatches') not in ([], None):
                    raise ValueError()
                result = result['proof']
            proof = validate_proof(recipe, service, result)
            if proof['observed_files'] != expected_files:
                raise ValueError()
            proofs[service] = proof
        except Exception:
            raise ProvenanceError('Updated Compose provenance could not be verified.') from None
    updated = copy.deepcopy(recipe); updated['service_provenance'] = proofs
    new_digest = _digest(updated)
    if new_digest == digest:
        return None
    return {'schema_version': 1, 'project': project, 'environment': current['name'],
            'old_recipe_id': current['recipe_revision'], 'old_digest': digest, 'new_digest': new_digest,
            'project_revision': current['project_revision'], 'environment_revision': current['environment_revision'],
            'proofs': proofs, 'rebased': rebased}


def _documents(db, transition):
    if not isinstance(transition, dict) or transition.get('schema_version') != 1:
        raise ProvenanceError('Provenance transition is invalid.')
    row = db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?',
                     (transition['old_recipe_id'], transition['project'])).fetchone()
    if row is None or row['digest'] != transition['old_digest']:
        raise ProvenanceError('Original provenance recipe is unavailable.')
    old = json.loads(row['document'])
    if _digest(old) != transition['old_digest']:
        raise ProvenanceError('Original provenance recipe is inconsistent.')
    proofs = transition['proofs']
    if not isinstance(proofs, dict) or set(proofs) != set(old.get('service_provenance', {})):
        raise ProvenanceError('Provenance transition changes service ownership.')
    rebased = transition.get('rebased', [])
    if (not isinstance(rebased, list) or not all(isinstance(service, str) for service in rebased)
            or len(set(rebased)) != len(rebased) or set(rebased) - set(proofs)):
        raise ProvenanceError('Provenance transition rebase selection is invalid.')
    for service, proof in proofs.items():
        validate_proof(old, service, proof)
        expected_files = old['compose_files'] if service in rebased else validate(old, service)['observed_files']
        if proof['observed_files'] != expected_files:
            raise ProvenanceError('Provenance transition changes invocation identity.')
    updated = copy.deepcopy(old); updated['service_provenance'] = proofs
    if _digest(updated) != transition['new_digest']:
        raise ProvenanceError('Provenance transition fingerprint is inconsistent.')
    return updated


def commit(db, transition):
    if transition is None:
        return None
    with registry.transaction(db):
        updated = _documents(db, transition)
        current = _state(db, transition['project'], transition['environment'])
        existing = db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?',
                              (transition['project'], transition['new_digest'])).fetchone()
        if (existing is not None and current['recipe_revision'] == existing['id']
                and current['project_revision'] == transition['project_revision'] + 1
                and current['environment_revision'] == transition['environment_revision'] + 1):
            return transition
        if current != {'recipe_revision': transition['old_recipe_id'], 'project_revision': transition['project_revision'],
                       'environment_revision': transition['environment_revision']}:
            raise ProvenanceError('Environment settings changed before provenance could be committed.')
        db.execute('INSERT OR IGNORE INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                   (transition['project'], transition['new_digest'], registry.canonical(updated), time.time()))
        recipe_id = db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?',
                               (transition['project'], transition['new_digest'])).fetchone()['id']
        db.execute('UPDATE registry_environments SET recipe_revision=?,revision=revision+1 WHERE project=? AND name=?',
                   (recipe_id, transition['project'], transition['environment']))
        db.execute('UPDATE registry_projects SET revision=revision+1,updated=? WHERE id=?', (time.time(), transition['project']))
    return transition


def rollback(db, transition):
    if transition is None:
        return
    with registry.transaction(db):
        _documents(db, transition)
        current = _state(db, transition['project'], transition['environment'])
        if current['recipe_revision'] == transition['old_recipe_id']:
            return  # Never committed or already restored; preserve current preferences.
        row = db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?',
                         (transition['project'], transition['new_digest'])).fetchone()
        if (row is None or current['recipe_revision'] != row['id']
                or current['project_revision'] != transition['project_revision'] + 1
                or current['environment_revision'] != transition['environment_revision'] + 1):
            raise ProvenanceError('Environment settings changed; provenance rollback requires reconciliation.')
        db.execute('UPDATE registry_environments SET recipe_revision=?,revision=revision+1 WHERE project=? AND name=?',
                   (transition['old_recipe_id'], transition['project'], transition['environment']))
        db.execute('UPDATE registry_projects SET revision=revision+1,updated=? WHERE id=?', (time.time(), transition['project']))
