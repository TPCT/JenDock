"""Private selection of a unique registered environment from actual Compose identity."""
import copy
import hashlib
import json
import re

import provenance
import registry
from compose_identity import ordered_files


def resolve(db, project, group, service, files):
    """Return an exact registered owner, or None only for unclaimed legacy identity.

    A broken immutable registry record is an integrity failure, not permission to
    fall back to a less specific legacy prefix. No runtime mutation occurs here.
    """
    registry.initialize(db)
    observed = ordered_files(files)
    matches = []
    claimed = False
    rows = db.execute('''SELECT e.project,e.name,e.revision,e.recipe_revision,
        r.document,r.digest FROM registry_environments e LEFT JOIN registry_recipes r
        ON r.id=e.recipe_revision AND r.project=e.project WHERE e.recipe_revision IS NOT NULL''')
    for row in rows:
        try:
            recipe = json.loads(row['document'])
            if not isinstance(recipe, dict) or hashlib.sha256(registry.canonical(recipe).encode()).hexdigest() != row['digest']:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Registered environment recipe integrity is invalid. Repair its binding before control.') from None
        if not isinstance(recipe.get('compose_project'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', recipe['compose_project']):
            raise ValueError('Registered environment Compose ownership is invalid.')
        if recipe.get('compose_project') != group:
            continue
        allowed = recipe.get('owned_services', recipe.get('services'))
        if (not isinstance(allowed, list) or not allowed or any(not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', item) for item in allowed)
                or len(set(allowed)) != len(allowed)):
            raise ValueError('Registered environment service ownership is invalid.')
        canonical = ordered_files(recipe.get('compose_files'))
        entries = recipe.get('service_provenance', {})
        if not isinstance(entries, dict):
            raise ValueError('Registered environment provenance is invalid.')
        for name, proof in entries.items():
            if name not in allowed:
                raise ValueError('Compose provenance claims an unowned service.')
            provenance.validate_proof(recipe, name, proof)
        claimed = claimed or service in allowed or observed == canonical
        if provenance.matches(recipe, group, service, list(observed)):
            matches.append({'project': row['project'], 'environment': row['name'],
                            'revision': row['revision'], 'recipe_revision': row['recipe_revision'],
                            'recipe': copy.deepcopy(recipe)})
    if len(matches) > 1:
        raise ValueError('Container ownership is ambiguous across registered environments.')
    if matches:
        if matches[0]['project'] != project:
            raise ValueError('Container belongs to another registered project.')
        return matches[0]
    if claimed:
        raise ValueError('Container configuration does not match its registered environment ownership.')
    return None
