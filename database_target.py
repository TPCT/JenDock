"""Reusable database ownership resolution, independent of credential privileges.

Callers authorize their operation and load a current binding first. The supplied
resolver must obtain fresh container/storage observations. This module neither
loads credentials nor verifies engine-side identity; inspection and migrations
retain their separate adapters and account policies.
"""
import hashlib
import registry


class TargetError(ValueError):
    pass


def resolve_ownership(row, *, resolve_candidate):
    """Resolve a binding's owned endpoint; return a newly fingerprinted snapshot."""
    definition=row['definition'];ownership=definition['ownership']
    candidate=resolve_candidate(row['project'],row['environment'],ownership['reference'],kind=ownership['kind'])
    # Laravel's mysql connection name can identify MariaDB. The caller's engine
    # adapter must still authenticate the precise dialect on the live connection.
    engine_matches=(candidate['engine']==definition['engine'] or
                    candidate['engine']=='mysql' and definition['engine']=='mariadb')
    if (candidate['recipe_revision']!=ownership['revision'] or not engine_matches
            or candidate['database']!=definition['database_name'] or candidate['port']!=definition['database_port']):
        raise TargetError('Database configuration does not match registered ownership.')
    candidate=dict(candidate,engine=definition['engine'])
    candidate['fingerprint']=hashlib.sha256(registry.canonical({k:v for k,v in candidate.items() if k!='fingerprint'}).encode()).hexdigest()
    return candidate


def check_registered(db, candidate):
    """Recheck registry revisions against a resolved snapshot without credentials."""
    project=registry.get_project(db,candidate['project'])
    env=registry.get_environment(db,candidate['project'],candidate['environment'])
    if (project['revision']!=candidate['project_revision'] or env['revision']!=candidate['environment_revision']
            or env['recipe_revision']!=candidate['recipe_revision']):
        raise TargetError('Registered database ownership changed.')
