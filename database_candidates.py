"""Private adopted database candidate resolution; no execution or credential discovery.

The worker supplies fresh Docker/volume/filesystem observations. This module does
not authorize a principal or establish engine identity; callers must do both.
"""
import hashlib
import json
import posixpath
import re
import time

import ownership
import provenance
import registry


class CandidateError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Database candidate unavailable: ' + code + '.')


def digest(value):
    return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


def _fail(code):
    raise CandidateError(code)


def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        _fail('invalid_inventory')
    return value


def _mounts(row):
    result = []
    for m in row.get('Mounts', []):
        if m.get('Type') not in ('volume', 'bind'):
            continue
        source, destination = m.get('Source'), m.get('Destination')
        if any(not isinstance(p, str) or not p.startswith('/') or posixpath.normpath(p) != p for p in (source, destination)):
            _fail('invalid_storage')
        result.append({'type': m['Type'], 'source': source, 'destination': destination})
    return sorted(result, key=lambda m: m['destination'])


def resolve(db, project, environment, reference, *, inventory, provenance_resolver=None,
            kind='adopted-compose', now=None):
    if environment not in registry.ENVIRONMENTS:
        _fail('invalid_environment')
    if kind == 'managed-service':
        # Existing managed artifacts define applications, not an authenticated DB role.
        _fail('unsupported_database_role')
    if kind != 'adopted-compose' or not isinstance(reference, str):
        _fail('unsupported_ownership')
    try:
        return _resolve(db, project, environment, reference, inventory,
                        provenance_resolver, time.time() if now is None else now)
    except CandidateError:
        raise
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, OSError):
        raise CandidateError('identity_unavailable') from None


def _resolve(db, project, environment, reference, inventory, resolver, now):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workload_adoptions'").fetchone():
        _fail('adoption_required')
    record = db.execute('SELECT * FROM workload_adoptions WHERE project=?', (project,)).fetchone()
    if not record or record['environment'] != environment:
        _fail('adoption_required')
    evidence = json.loads(record['evidence'])
    if (evidence.get('project') != project or evidence.get('environment') != environment
            or evidence.get('fingerprint') != record['fingerprint']
            or digest({k:v for k,v in evidence.items() if k != 'fingerprint'}) != record['fingerprint']):
        _fail('adoption_integrity')
    saved = [r for r in evidence['containers'] if r['service'] == reference]
    if len(saved) != 1:
        _fail('candidate_ambiguous')
    historical = [r for r in evidence['databases'] if r.get('container_id') == saved[0]['id']]
    if not historical:
        _fail('database_evidence_required')
    # Several application clients may refer to the same database; deduplicate identical evidence.
    databases = {registry.canonical({k:v for k,v in r.items() if k not in ('client','host')}) for r in historical}
    if len(databases) != 1:
        _fail('database_ambiguous')
    database = json.loads(next(iter(databases)))
    if database.get('engine') not in ('pgsql','postgres','postgresql'):
        _fail('unsupported_engine')
    if (type(database.get('port')) is not int or not 1 <= database['port'] <= 65535
            or not isinstance(database.get('database'), str) or not 1 <= len(database['database']) <= 128):
        _fail('invalid_database_evidence')
    observed = inventory()
    stamp = observed.get('observed')
    if type(stamp) not in (int,float) or not 0 <= now-stamp <= 30:
        _fail('stale_inventory')
    rows = observed['containers']
    if not isinstance(rows,list) or len(rows)>1000:
        _fail('inventory_limit')
    env = registry.get_environment(db, project, environment)
    recipe_row = db.execute('SELECT document FROM registry_recipes WHERE id=? AND project=?', (env['recipe_revision'], project)).fetchone()
    if not recipe_row: _fail('registered_owner_required')
    group = json.loads(recipe_row['document']).get('compose_project')
    matches=[]
    for row in rows:
        labels=row.get('Config',{}).get('Labels') or {}
        if labels.get('com.docker.compose.service') != reference or labels.get('com.docker.compose.project') != group:
            continue
        files=(labels.get('com.docker.compose.project.config_files') or '').split(',')
        owner=ownership.resolve(db, project, labels.get('com.docker.compose.project'), reference, files)
        if owner and owner['environment']==environment:
            matches.append((row,owner))
    if len(matches)!=1:
        _fail('candidate_ambiguous')
    row, owner=matches[0]
    identity=_id(row['Id'])
    if identity!=saved[0]['id'] or row['Image']!=saved[0]['image_id']:
        _fail('identity_changed')
    if not re.fullmatch('sha256:[a-f0-9]{64}',row['Image']):
        _fail('invalid_inventory')
    if row['State'].get('Running') is not True or row['State'].get('Paused') or row['State'].get('Restarting'):
        _fail('target_stopped')
    if not isinstance(row['State'].get('StartedAt'),str) or not row['State']['StartedAt']:
        _fail('namespace_unverified')
    mode=row['HostConfig']['NetworkMode']
    if mode in ('host','none') or mode.startswith('container:'):
        _fail('unsupported_namespace')
    provenance.revalidate(owner['recipe'],reference,resolver)
    destinations=[m.get('Destination') for m in row.get('Mounts',[])]
    if len(set(destinations))!=len(destinations) or any(type(m.get('RW')) is not bool for m in row.get('Mounts',[])):
        _fail('invalid_storage')
    mounts=_mounts(row)
    storage=database['storage']
    if not storage or any(m not in mounts for m in storage):
        _fail('storage_changed')
    pinned=[]
    for mount in storage:
        destination=mount['destination']; source=mount['source']
        if any(m['destination']!=destination and m['destination'].startswith(destination.rstrip('/')+'/') for m in mounts):
            _fail('overlapping_storage')
        for peer in rows:
            if peer['Id']==identity:continue
            for other in _mounts(peer):
                a,b=source.rstrip('/'),other['source'].rstrip('/')
                if a==b or a.startswith(b+'/') or b.startswith(a+'/'):
                    _fail('shared_storage')
        actual=next(m for m in row['Mounts'] if m.get('Source')==source and m.get('Destination')==destination)
        if mount['type']=='volume':
            name=actual.get('Name'); volume=observed.get('volumes',{}).get(name)
            if not volume or volume.get('Name')!=name or volume.get('Mountpoint')!=source or not volume.get('CreatedAt') or not volume.get('Driver'):
                _fail('volume_identity_required')
            proof={k:volume.get(k) for k in ('Name','Driver','Mountpoint','CreatedAt')}
            proof['options_digest']=digest(volume.get('Options'))
        else:
            proof=observed.get('bind_sources',{}).get(source)
            if not isinstance(proof,dict) or proof.get('realpath')!=source or any(type(proof.get(k)) is not int for k in ('device','inode')):
                _fail('bind_identity_required')
        pinned.append({**mount,'rw':actual.get('RW'),'identity':proof})
    networks=[]
    for name, net in sorted(row['NetworkSettings']['Networks'].items()):
        networks.append({'name':name,'id':_id(net['NetworkID']), 'endpoint_id':_id(net['EndpointID']),
                         'ip':net.get('IPAddress'),'aliases':sorted(net.get('Aliases') or [])})
    if not networks:_fail('namespace_unverified')
    value={'schema_version':1,'project':project,'environment':environment,'kind':'adopted-compose',
           'reference':reference,'project_revision':registry.get_project(db,project)['revision'],'environment_revision':owner['revision'],'recipe_revision':owner['recipe_revision'],
           'recipe_digest':digest(owner['recipe']),'container_id':identity,'image_id':row['Image'],
           'started_at':row['State'].get('StartedAt'),'network_mode':mode,'networks':networks,'storage':pinned,
           'engine':'postgresql','database':database['database'],'port':database['port'],
           'adoption_fingerprint':record['fingerprint']}
    value['fingerprint']=digest(value)
    return value


def public(candidate):
    return {k:candidate[k] for k in ('project','environment','reference','engine','database','port',
        'container_id','image_id','fingerprint','recipe_revision','environment_revision')} | {'verified':False}
