"""Exclusive-worker replacement of a verified managed application artifact.

The delivery owner holds the host control lock and persists the returned binding
with a CAS. Interrupted mutations are never replayed automatically.
"""
import copy
import json
from pathlib import Path
import time

import managed_services
import registry
import service_provisioning as provision


class RolloutError(ValueError):
    def __init__(self, message, *, restored_binding=None):
        super().__init__(message)
        self.restored_binding = restored_binding


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS managed_rollout_journals(
        operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        document TEXT NOT NULL, updated REAL NOT NULL)''')


def rollout(db, operation_id, item, *, docker, compose, authorize, health_timeout=35):
    """Replace one pinned running service; stopped services remain untouched.

    item carries definition, binding, resources, artifact, next_definition, and
    operation_id (the stable service ownership label). authorize rechecks the
    delivery lease, grants, selected target and resource-policy revisions.
    A RolloutError.restored_binding requires a CAS rollback metadata commit.
    """
    if db.in_transaction or not callable(authorize):
        raise RolloutError('Rollout requires exclusive worker authorization outside a transaction.')
    authorize()
    initialize(db)
    fingerprint = managed_services.digest(item)
    if db.execute('SELECT 1 FROM managed_rollout_journals WHERE operation_id=?', (operation_id,)).fetchone():
        raise RolloutError('Recorded rollout requires reconciliation; mutations are never repeated automatically.')
    old, binding = item['definition'], item['binding']
    new, resources = item['next_definition'], item['resources']
    for key in ('project', 'environment', 'name', 'service_id'):
        if old[key] != new[key]: raise RolloutError('Rollout cannot change service ownership.')
    if managed_services.artifact_for_service(db, old['project'], item['artifact']['id']) != item['artifact'] or new['artifact'] != item['artifact']:
        raise RolloutError('Verified delivery artifact changed.')
    if len(binding['compose_files']) != 1:
        raise RolloutError('Managed rollout requires one canonical environment manifest.')
    path = Path(binding['compose_files'][0])
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise RolloutError('Managed manifest path is not canonical.')
    before_bytes = path.read_bytes()
    try: before = json.loads(before_bytes)
    except ValueError: raise RolloutError('Managed manifest is not canonical JSON.') from None
    service = binding['compose_service']
    if (binding['compose_project'] != old['project'] or service != provision._names(old)[0]
            or binding['network_name'] != provision._names(old)[1] or len(binding['network_ids']) != 1):
        raise RolloutError('Pinned service ownership is inconsistent.')
    peers = managed_services.environment_definitions(db, old['project'], old['environment'])
    if (before.get('name') != old['project'] or set(before) != {'name', 'services', 'networks'}
            or before['networks'] != {'environment': {'external': True, 'name': binding['network_name']}}
            or set(before['services']) != {provision._names(p['definition'])[0] for p in peers}):
        raise RolloutError('Managed environment manifest drifted.')
    # Direct caps can legitimately be newer than the manifest. Only resource
    # fields may differ; untouched peer entries are retained exactly.
    cap_keys = {'mem_limit', 'memswap_limit', 'cpus', 'deploy'}
    for peer in peers:
        definition = peer['definition']; key = provision._names(definition)[0]
        expected = provision.service_config(definition, peer['operation_id'], peer['effective_resources'])
        existing = before['services'][key]
        if {k:v for k,v in existing.items() if k not in cap_keys} != {k:v for k,v in expected.items() if k not in cap_keys}:
            raise RolloutError('Managed service manifest drifted.')
    selected = [p for p in peers if p['id'] == old['service_id']]
    if len(selected) != 1 or selected[0]['binding'] != binding or selected[0]['definition'] != old or selected[0]['effective_resources'] != resources:
        raise RolloutError('Selected service changed before rollout.')
    label_operation = item['operation_id']
    if label_operation != selected[0]['operation_id']:
        raise RolloutError('Stable service operation identity changed.')

    def network():
        row = provision._json([docker, 'network', 'inspect', binding['network_ids'][0]])[0]
        provision._network(row, old, binding['network_ids'][0])
        known = {p['binding']['container_id'] for p in peers if p.get('binding')}
        known.update(journal.get('replacement_ids', []))
        if set(row.get('Containers') or {}) - known:
            raise RolloutError('An unknown container joined the managed environment network.')

    def inspect(definition, owned, operation, caps):
        row = provision._json([docker, 'inspect', owned['container_id']])[0]
        if row.get('Id') != owned['container_id']: raise RolloutError('Pinned container identity changed.')
        provision._verify(row, definition, operation, owned['image_id'], binding['network_ids'][0], owned['container_name'], path, caps)
        return row

    def guard(document_bytes, check_selected=True):
        authorize()
        if path.read_bytes() != document_bytes: raise RolloutError('Managed manifest changed during rollout.')
        network()
        for peer in peers:
            if not check_selected and peer['id'] == old['service_id']: continue
            if not peer.get('binding'): raise RolloutError('Pending peer provisioning prevents rollout.')
            row = inspect(peer['definition'], peer['binding'], peer['operation_id'], peer['effective_resources'])
            if bool(row['State']['Running']) != (peer['binding']['state'] == 'running'):
                raise RolloutError('Pinned service runtime state changed.')

    journal = {'service_id':old['service_id'], 'old_binding':binding, 'old_definition':old,
               'next_definition':new, 'resources':resources, 'before_manifest':before,
               'replacement_ids':[]}
    def persist(phase, **values):
        journal.update(values, phase=phase)
        db.execute('INSERT INTO managed_rollout_journals VALUES(?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET document=excluded.document,updated=excluded.updated',
                   (operation_id, fingerprint, registry.canonical(journal), time.time()))

    guard(before_bytes)
    if binding['state'] == 'stopped': return copy.deepcopy(binding)
    if binding['state'] != 'running': raise RolloutError('Only verified runtime states are supported.')
    for artifact in (old['artifact'], new['artifact']):
        image = provision._json([docker, 'image', 'inspect', artifact['image']])[0]
        if image.get('Config', {}).get('Volumes') or image['Id'] != artifact['image']:
            raise RolloutError('Rollout requires a local immutable image without inherited volumes.')
    after = copy.deepcopy(before)
    after['services'][service] = provision.service_config(new, label_operation, resources)
    after_bytes = registry.canonical(after).encode()
    persist('manifest-intent', after_manifest=after)
    guard(before_bytes)
    provision._atomic(path, after)

    def replace(definition, expected_bytes, phase, previous_definition, previous_binding):
        guard(expected_bytes, check_selected=False)
        previous = inspect(previous_definition, previous_binding, label_operation, resources)
        if phase == 'delivery' and not previous['State'].get('Running'):
            raise RolloutError('Selected service stopped before replacement; it was not restarted.')
        persist(phase + '-create-intent')
        provision.command([compose, '-p', old['project'], '-f', str(path), 'create', '--no-build', '--pull', 'never', '--force-recreate', service])
        row = provision._lookup(docker, 'container', binding['container_name'])
        if row is None: raise RolloutError('Replacement outcome is uncertain; reconciliation is required.')
        owned = dict(binding, container_id=row['Id'], image_id=definition['artifact']['image'],
                     definition_digest=managed_services.digest(definition), state='running', observed=time.time())
        inspect(definition, owned, label_operation, resources)
        journal['replacement_ids'].append(row['Id'])
        persist(phase + '-created', candidate_binding=owned)
        guard(expected_bytes, check_selected=False)
        inspect(definition, owned, label_operation, resources)
        persist(phase + '-start-intent')
        provision.command([docker, 'start', owned['container_id']])
        deadline = time.monotonic() + health_timeout
        while True:
            guard(expected_bytes, check_selected=False)
            row = inspect(definition, owned, label_operation, resources)
            if row['State'].get('Running') and row['State'].get('Health', {}).get('Status') == 'healthy':
                owned['observed'] = time.time()
                persist(phase + '-verified', candidate_binding=owned)
                return owned
            if not row['State'].get('Running') or time.monotonic() >= deadline:
                return None
            time.sleep(.25)

    # An ambiguous Docker response deliberately escapes without further mutation.
    result = replace(new, after_bytes, 'delivery', old, binding)
    if result is not None: return result
    failed = journal['candidate_binding']
    guard(after_bytes, check_selected=False)
    inspect(new, failed, label_operation, resources)
    rollback = copy.deepcopy(before)
    rollback['services'][service] = provision.service_config(old, label_operation, resources)
    persist('rollback-manifest-intent', rollback_manifest=rollback)
    provision._atomic(path, rollback)
    restored = replace(old, registry.canonical(rollback).encode(), 'rollback', new, failed)
    if restored is None:
        raise RolloutError('New application and rollback health were not verified. Recovery is required.')
    raise RolloutError('New application health failed; the previous image was restored and verified.', restored_binding=restored)
