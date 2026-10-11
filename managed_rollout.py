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


def manifest_networks(peers,descriptors,environment_name):
    result={'environment':{'external':True,'name':environment_name}}
    for peer in peers:
        descriptor=descriptors.get(managed_services.digest(peer['definition']))
        if descriptor:result[descriptor['name']]={'external':True,'name':descriptor['name']}
    return result


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
    import service_runtime
    runtime_context=service_runtime.Context(db,authorize)
    descriptors={}
    def descriptor(d):return descriptors.get(managed_services.digest(d))
    def config(d,o,r=None):return provision.service_config(d,o,r,runtime_context=runtime_context,database_network=descriptor(d))
    def verify(*args):return provision._verify(*args,runtime_context=runtime_context,docker=docker,database_network=descriptor(args[1]))
    initialize(db)
    fingerprint = managed_services.digest(item)
    if db.execute('SELECT 1 FROM managed_rollout_journals WHERE operation_id=?', (operation_id,)).fetchone():
        raise RolloutError('Recorded rollout requires reconciliation; mutations are never repeated automatically.')
    old, binding = item['definition'], item['binding']
    new, resources = item['next_definition'], item['resources']
    next_resources=item.get('next_resources',resources)
    if item.get('runtime_change') and old['artifact']!=new['artifact']:
        raise RolloutError('Runtime settings transitions cannot change the application image.')
    import ingress_bindings
    snapshot = item.get('ingress_current', item.get('ingress_snapshot'))
    if ingress_bindings.capture(db, old['project'], old['environment'], old['service_id']) != snapshot:
        raise RolloutError('Gateway route identity changed before rollout.')
    ingress_bindings.preflight(snapshot, alias=binding['compose_service'], http_endpoint=new['artifact'].get('http_endpoint'))
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
    for peer in peers:
        d=peer['definition'];descriptors[managed_services.digest(d)]=runtime_context.attachment(d,docker)
    if (before.get('name') != old['project'] or set(before) != {'name', 'services', 'networks'}
            or before['networks'] != manifest_networks(peers,descriptors,binding['network_name'])
            or set(before['services']) != {provision._names(p['definition'])[0] for p in peers}):
        raise RolloutError('Managed environment manifest drifted.')
    # Direct caps can legitimately be newer than the manifest. Only resource
    # fields may differ; untouched peer entries are retained exactly.
    cap_keys = {'mem_limit', 'memswap_limit', 'cpus', 'deploy'}
    for peer in peers:
        definition = peer['definition']; key = provision._names(definition)[0]
        expected = config(definition, peer['operation_id'], peer['effective_resources'])
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
        from ingress_runtime import verified_network_members
        known.update(verified_network_members(db, old['project'], old['environment'],
            docker=docker, application_network_id=binding['network_ids'][0], authorize=authorize))
        if set(row.get('Containers') or {}) - known:
            raise RolloutError('An unknown container joined the managed environment network.')
        def verify_application(actual):
            if actual.get('Id') not in known:return False
            for d in [p['definition'] for p in peers]+[new]:
                if (actual.get('Config',{}).get('Labels') or {}).get(provision.PREFIX+'definition')!=managed_services.digest(d):continue
                peer=next(p for p in peers if p['id']==d['service_id'])
                provision._verify(actual,d,peer['operation_id'],d['artifact']['image'],binding['network_ids'][0],peer['binding']['container_name'],path,next_resources if d==new else peer['effective_resources'],runtime_context=runtime_context,docker=docker,database_network=descriptor(d))
                return True
            return False
        for d in [p['definition'] for p in peers]+([new] if managed_services.digest(new) in descriptors else []):
            if descriptor(d) and runtime_context.attachment(d,docker,verify_application=verify_application)!=descriptor(d):raise RolloutError('Database network authority changed.')

    def inspect(definition, owned, operation, caps):
        row = provision._json([docker, 'inspect', owned['container_id']])[0]
        if row.get('Id') != owned['container_id']: raise RolloutError('Pinned container identity changed.')
        verify(row, definition, operation, owned['image_id'], binding['network_ids'][0], owned['container_name'], path, caps)
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
               'replacement_ids':[], 'peers':copy.deepcopy(peers)}
    def persist(phase, **values):
        journal.update(values, phase=phase)
        db.execute('INSERT INTO managed_rollout_journals VALUES(?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET document=excluded.document,updated=excluded.updated',
                   (operation_id, fingerprint, registry.canonical(journal), time.time()))

    guard(before_bytes)
    if binding['state'] == 'stopped' and not item.get('runtime_change'): return copy.deepcopy(binding)
    if binding['state'] not in ('running','stopped'): raise RolloutError('Only verified runtime states are supported.')
    for artifact in (old['artifact'], new['artifact']):
        image = provision._json([docker, 'image', 'inspect', artifact['image']])[0]
        if image.get('Config', {}).get('Volumes') or image['Id'] != artifact['image']:
            raise RolloutError('Rollout requires a local immutable image without inherited volumes.')
    descriptors[managed_services.digest(new)]=runtime_context.attachment(new,docker,ensure=True)
    after = copy.deepcopy(before)
    after['services'][service] = config(new, label_operation, next_resources)
    after_peers=[dict(p,definition=new) if p['id']==old['service_id'] else p for p in peers]
    after['networks']=manifest_networks(after_peers,descriptors,binding['network_name'])
    after_bytes = registry.canonical(after).encode()
    persist('manifest-intent', after_manifest=after)
    guard(before_bytes)
    provision._atomic(path, after)

    def replace(definition, expected_bytes, phase, previous_definition, previous_binding):
        caps=next_resources if phase=='delivery' else resources
        previous_caps=resources if phase=='delivery' else next_resources
        guard(expected_bytes, check_selected=False)
        previous = inspect(previous_definition, previous_binding, label_operation, previous_caps)
        if phase == 'delivery' and binding['state']=='running' and not previous['State'].get('Running'):
            raise RolloutError('Selected service stopped before replacement; it was not restarted.')
        persist(phase + '-create-intent')
        definitions=[p['definition'] for p in peers if p['id']!=old['service_id']]+[definition]
        provision.command([compose, '-p', old['project'], '-f', str(path), 'create', '--no-build', '--pull', 'never', '--force-recreate', service], **({'environment':runtime_context.substitutions(definitions)} if any(d.get('runtime') for d in definitions) else {}))
        row = provision._lookup(docker, 'container', binding['container_name'])
        if row is None: raise RolloutError('Replacement outcome is uncertain; reconciliation is required.')
        owned = dict(binding, container_id=row['Id'], image_id=definition['artifact']['image'],
                     definition_digest=managed_services.digest(definition), state=binding['state'], observed=time.time())
        if descriptor(definition):owned['database_network']=descriptor(definition)
        else:owned.pop('database_network',None)
        inspect(definition, owned, label_operation, caps)
        journal['replacement_ids'].append(row['Id'])
        persist(phase + '-created', candidate_binding=owned)
        if binding['state']=='stopped':
            if row['State'].get('Running') or row['State'].get('StartedAt','') not in ('','0001-01-01T00:00:00Z'):
                raise RolloutError('Stopped runtime replacement unexpectedly ran.')
            guard(expected_bytes,check_selected=False)
            persist(phase+'-verified',candidate_binding=owned)
            return owned
        guard(expected_bytes, check_selected=False)
        inspect(definition, owned, label_operation, caps)
        persist(phase + '-start-intent')
        provision.command([docker, 'start', owned['container_id']])
        deadline = time.monotonic() + health_timeout
        while True:
            guard(expected_bytes, check_selected=False)
            row = inspect(definition, owned, label_operation, caps)
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
    inspect(new, failed, label_operation, next_resources)
    rollback = copy.deepcopy(before)
    rollback['services'][service] = config(old, label_operation, resources)
    persist('rollback-manifest-intent', rollback_manifest=rollback)
    provision._atomic(path, rollback)
    restored = replace(old, registry.canonical(rollback).encode(), 'rollback', new, failed)
    if restored is None:
        raise RolloutError('New application and rollback health were not verified. Recovery is required.')
    raise RolloutError('New application health failed; the previous image was restored and verified.', restored_binding=restored)


def reconcile(db, operation_id, item, *, docker, authorize):
    """Read-only evidence for a caller-held lock and subsequent metadata CAS.

    authorize must pin every environment peer to the accepted delivery plan,
    including already committed sibling replacements. It is checked before and
    after observation. A missing/ambiguous candidate remains unknown; no Docker
    mutation, journal initialization, rollback, or retry is performed here.
    """
    if db.in_transaction or not callable(authorize):
        raise RolloutError('Reconciliation requires exclusive worker authorization outside a transaction.')
    authorize()
    import service_runtime
    runtime_context=service_runtime.Context(db,authorize)
    descriptors={}
    def descriptor(d):return descriptors.get(managed_services.digest(d))
    def config(d,o,r=None):return provision.service_config(d,o,r,runtime_context=runtime_context,database_network=descriptor(d))
    def verify(*args):return provision._verify(*args,runtime_context=runtime_context,docker=docker,database_network=descriptor(args[1]))
    unknown = lambda reason: {'outcome': 'unknown', 'binding': None, 'reason': reason}
    old, new, binding = item['definition'], item.get('next_definition', item['definition']), item['binding']
    resources = item['resources']
    next_resources=item.get('next_resources',resources)
    try:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_rollout_journals'").fetchone()
        record = db.execute('SELECT fingerprint,document FROM managed_rollout_journals WHERE operation_id=?', (operation_id,)).fetchone() if exists else None
        journal = json.loads(record['document']) if record else None
        original_item = copy.deepcopy(item)
        original_item.pop('verified_binding', None)
        original_item.pop('ingress_current', None)
        if original_item.get('state') in ('succeeded', 'rolled-back', 'unchanged'):
            original_item['state'] = 'pending'
        if record and record['fingerprint'] != managed_services.digest(original_item):
            return unknown('Rollout definition fingerprint changed.')
        if len(binding['compose_files']) != 1 or len(binding['network_ids']) != 1:
            return unknown('Ambiguous manifest or network ownership.')
        path = Path(binding['compose_files'][0])
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            return unknown('Noncanonical manifest path.')
        service, network_name = provision._names(old)
        if binding['compose_project'] != old['project'] or binding['compose_service'] != service or binding['network_name'] != network_name:
            return unknown('Binding ownership differs.')
        peers = managed_services.environment_definitions(db, old['project'], old['environment'])
        definitions=[p['definition'] for p in peers]+[old,new]
        for d in definitions:descriptors[managed_services.digest(d)]=runtime_context.recorded_attachment(d)
        allowed_ids={p['binding']['container_id'] for p in peers if p.get('binding')} | set((journal or {}).get('replacement_ids',[]))
        def verify_application(actual):
            if actual.get('Id') not in allowed_ids:return False
            for d in definitions:
                if (actual.get('Config',{}).get('Labels') or {}).get(provision.PREFIX+'definition')!=managed_services.digest(d):continue
                peer=next(p for p in peers if p['id']==d['service_id'])
                caps=next_resources if d==new else peer['effective_resources']
                provision._verify(actual,d,peer['operation_id'],d['artifact']['image'],binding['network_ids'][0],peer['binding']['container_name'],path,caps,runtime_context=runtime_context,docker=docker,database_network=descriptor(d))
                return True
            return False
        for d in definitions:
            if runtime_context.attachment(d,docker,verify_application=verify_application)!=descriptor(d):return unknown('Database network evidence changed.')
        selected = [p for p in peers if p['id'] == old['service_id']]
        if len(selected) != 1 or selected[0]['operation_id'] != item['operation_id'] or selected[0]['effective_resources'] != resources:
            return unknown('Selected service ownership or resources changed.')
        manifest_bytes = path.read_bytes()
        current = json.loads(manifest_bytes)
        if journal:
            if any(journal[k] != value for k, value in [('old_definition', old), ('next_definition', new), ('old_binding', binding), ('resources', resources), ('service_id', old['service_id'])]):
                return unknown('Journal ownership differs.')
            before = journal['before_manifest']
            after = copy.deepcopy(before)
            after['services'][service] = config(new, item['operation_id'], next_resources)
            after['networks']=manifest_networks([dict(p,definition=new) if p['id']==old['service_id'] else p for p in peers],descriptors,network_name)
            if journal['after_manifest'] != after:
                return unknown('Journal delivery manifest differs.')
            phase = journal['phase']
            if phase == 'manifest-intent' and not journal['replacement_ids']:
                definition, owned, expected, outcome = old, binding, before, 'verified-old'
            elif phase in ('delivery-created', 'delivery-start-intent', 'delivery-verified'):
                definition, owned, expected, outcome = new, journal['candidate_binding'], after, 'verified-new'
            elif phase in ('rollback-created', 'rollback-start-intent', 'rollback-verified'):
                expected = copy.deepcopy(before)
                expected['services'][service] = config(old, item['operation_id'], resources)
                if journal['rollback_manifest'] != expected:
                    return unknown('Journal rollback manifest differs.')
                definition, owned, outcome = old, journal['candidate_binding'], 'verified-old'
            else:
                return unknown('Phase has no recorded runtime candidate.')
            if phase != 'manifest-intent' and (owned['container_id'] not in journal['replacement_ids'] or owned['container_id'] == binding['container_id']):
                return unknown('Candidate is not an exact recorded replacement.')
            recorded_peers = journal.get('peers')
            if recorded_peers is None:
                return unknown('Journal has no peer identity snapshot.')
            strip_selected = lambda entries: [p for p in entries if p['id'] != old['service_id']]
            if {p['id'] for p in strip_selected(peers)} != {p['id'] for p in strip_selected(recorded_peers)}:
                return unknown('Peer membership changed.')
            # authorize() pins all current peer metadata to the delivery plan;
            # a previous successful sibling replacement can legitimately differ
            # from this journal snapshot. Every current identity is inspected below.
        else:
            definition, owned, expected, outcome = old, binding, current, 'verified-old'
        if selected[0]['definition'] not in (old, definition) or selected[0]['binding']['container_id'] not in (binding['container_id'], owned['container_id']):
            return unknown('Selected metadata no longer matches the operation.')
        if any(old[k] != new[k] for k in ('project', 'environment', 'name', 'service_id')):
            return unknown('Definition ownership changed.')
        stable = set(binding) - {'container_id', 'image_id', 'definition_digest', 'state', 'observed','database_network'}
        if owned.get('database_network')!=descriptor(definition):return unknown('Database network binding differs.')
        if (any(owned.get(k) != binding[k] for k in stable) or owned['image_id'] != definition['artifact']['image']
                or owned['definition_digest'] != managed_services.digest(definition) or owned['state'] != binding['state']):
            return unknown('Candidate binding differs from its immutable definition.')
        expected = copy.deepcopy(expected)
        for peer in peers:
            if peer['id'] == old['service_id']: continue
            key = provision._names(peer['definition'])[0]
            expected['services'][key] = config(peer['definition'], peer['operation_id'], peer['effective_resources'])
            for cap in ('mem_limit', 'memswap_limit', 'cpus', 'deploy'):
                if cap in current['services'].get(key, {}): expected['services'][key][cap] = current['services'][key][cap]
                else: expected['services'][key].pop(cap, None)
        if current != expected or set(current) != {'name', 'services', 'networks'} or current['name'] != old['project'] or current['networks'] != manifest_networks([dict(p,definition=definition) if p['id']==old['service_id'] else p for p in peers],descriptors,network_name):
            return unknown('Current manifest differs.')
        if set(current['services']) != {provision._names(p['definition'])[0] for p in peers}:
            return unknown('Manifest service membership differs.')
        observations = [(definition, owned, item['operation_id'], next_resources if outcome=='verified-new' else resources)]
        observations += [(p['definition'], p['binding'], p['operation_id'], p['effective_resources']) for p in peers if p['id'] != old['service_id']]
        cap_keys = {'mem_limit', 'memswap_limit', 'cpus', 'deploy'}
        for d, pinned, operation, caps in observations:
            spec = config(d, operation, caps)
            actual = current['services'][provision._names(d)[0]]
            if {k:v for k,v in spec.items() if k not in cap_keys} != {k:v for k,v in actual.items() if k not in cap_keys}:
                return unknown('Manifest configuration differs.')
            row = provision._json([docker, 'inspect', pinned['container_id']])[0]
            if row.get('Id') != pinned['container_id']:
                return unknown('Exact container identity changed.')
            verify(row, d, operation, pinned['image_id'], binding['network_ids'][0], pinned['container_name'], path, caps)
            if pinned['state'] not in ('running', 'stopped') or bool(row['State']['Running']) != (pinned['state'] == 'running'):
                return unknown('Runtime state changed.')
            if pinned['state'] == 'running' and row['State'].get('Health', {}).get('Status') != 'healthy':
                return unknown('Runtime health is not verified.')
        network = provision._json([docker, 'network', 'inspect', binding['network_ids'][0]])[0]
        provision._network(network, old, binding['network_ids'][0])
        from ingress_runtime import verified_network_members
        known = {p['container_id'] for _, p, _, _ in observations}
        known.update(verified_network_members(db, old['project'], old['environment'],
            docker=docker, application_network_id=binding['network_ids'][0], authorize=authorize))
        if set(network.get('Containers') or {}) - known:
            return unknown('Unknown network peer identity.')
        if path.read_bytes() != manifest_bytes:
            return unknown('Manifest changed during observation.')
        result = dict(owned, observed=time.time())
    except (ValueError, KeyError, TypeError, IndexError, OSError) as error:
        return unknown('Reconciliation evidence unavailable: ' + str(error))
    authorize()
    return {'outcome': outcome, 'binding': result, 'reason': 'Exact manifest, runtime, health and peer identities verified.'}
