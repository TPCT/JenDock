"""Verified legacy workload adoption; input inventory must come from the worker.

This binds existing objects only. It never provisions, starts, moves or deletes
containers/data. Endpoint evidence identifies configuration, not database integrity.
"""
import hashlib
from contextlib import nullcontext
import json
import posixpath
import re
import time
from urllib.parse import urlsplit

import registry
from compose_identity import ordered_files


def initialize(db):
    with (nullcontext() if db.in_transaction else registry.transaction(db)):
        columns = list(db.execute('PRAGMA table_info(workload_adoptions)'))
        if columns and [(r['name'], r['pk']) for r in columns if r['pk']] == [('project', 1)]:
            # Preserve byte-identical evidence and all receipts atomically. No
            # environment or ownership is inferred during the legacy migration.
            db.execute('ALTER TABLE workload_adoptions RENAME TO workload_adoptions_legacy')
            db.execute("""CREATE TABLE workload_adoptions(
                project TEXT NOT NULL, environment TEXT NOT NULL, fingerprint TEXT NOT NULL,
                evidence TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(project,environment))""")
            db.execute('INSERT INTO workload_adoptions SELECT * FROM workload_adoptions_legacy')
            db.execute('DROP TABLE workload_adoptions_legacy')
        elif not columns:
            db.execute("""CREATE TABLE workload_adoptions(
                project TEXT NOT NULL, environment TEXT NOT NULL, fingerprint TEXT NOT NULL,
                evidence TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(project,environment))""")
        elif [(r['name'], r['pk']) for r in columns if r['pk']] != [('project', 1), ('environment', 2)]:
            raise ValueError('Adoption schema requires an explicit supported migration.')


def _conflicts(db, project, environment, evidence):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workload_adoptions'").fetchone():return
    rows=db.execute('SELECT * FROM workload_adoptions LIMIT 1001').fetchall()
    if len(rows)>1000:raise ValueError('Adoption ownership inventory exceeds its bound.')
    identities={r['id'] for r in evidence['containers']}
    storage=[m['source'].rstrip('/') for d in evidence['databases'] for m in d['storage']]
    for row in rows:
        if row['project']==project and row['environment']==environment:continue
        try:
            prior=json.loads(row['evidence'])
            if prior['project']!=row['project'] or prior['environment']!=row['environment'] or prior['fingerprint']!=row['fingerprint'] or _digest({k:v for k,v in prior.items() if k!='fingerprint'})!=row['fingerprint']:raise ValueError()
            if identities.intersection(r['id'] for r in prior['containers']):raise ValueError()
            other=[m['source'].rstrip('/') for d in prior['databases'] for m in d['storage']]
            if any(a==b or a.startswith(b+'/') or b.startswith(a+'/') for a in storage for b in other):raise ValueError()
        except (ValueError,KeyError,TypeError):raise ValueError('Another adopted environment has conflicting or invalid ownership evidence.') from None


def _digest(value):
    return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


def options(db, project, environment):
    """Public catalog of registered choices; never return executable recipes or paths."""
    current = registry.get_project(db, project)
    target = registry.get_environment(db, project, environment)
    rows = db.execute('SELECT id,digest,document FROM registry_recipes WHERE project=? ORDER BY id DESC LIMIT 201', (project,)).fetchall()
    if len(rows) > 200:
        raise ValueError('Registered recipe catalog exceeds the supported review size.')

    def summary(document, revision, digest):
        services = document.get('services')
        group = document.get('compose_project')
        if not isinstance(group, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', group):
            return None
        if not isinstance(services, list) or not services or len(services) > 100 or any(not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', value) for value in services) or len(set(services)) != len(services):
            return None
        return {'recipe_revision': revision, 'digest': digest, 'compose_project': group, 'services': sorted(services)}

    recipes = []
    for row in rows:
        try:
            document = json.loads(row['document'])
            if not isinstance(document, dict) or _digest(document) != row['digest']:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Registered recipe integrity could not be verified.') from None
        item = summary(document, row['id'], row['digest'])
        if item is not None:
            recipes.append(item)
    try:
        legacy = json.loads(db.execute('SELECT legacy_document FROM registry_projects WHERE id=?', (project,)).fetchone()[0] or '{}')
        document = legacy.get('recipe')
        default = summary(document, None, _digest(document)) if isinstance(document, dict) else None
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Default registered recipe could not be verified.') from None
    return {'project': project, 'environment': environment, 'project_revision': current['revision'],
            'environment_revision': target['revision'], 'default': default, 'recipes': recipes}


def collect(docker, command):
    """Bounded read-only inspection. Never emit raw environment or Docker labels."""
    observed = time.time()
    identifiers = command([docker, 'ps', '-aq', '--no-trunc']).split()
    if len(identifiers) > 1000:
        raise ValueError('Inventory exceeds the supported adoption review size.')
    rows = json.loads(command([docker, 'inspect', *identifiers])) if identifiers else []
    result = []
    for row in rows:
        labels = row['Config'].get('Labels') or {}
        values = dict(value.split('=', 1) for value in row['Config'].get('Env', []) if '=' in value)
        endpoint = {key: values[key] for key in ('DB_CONNECTION', 'DB_HOST', 'DB_PORT', 'DB_DATABASE', 'DB_NAME', 'DATABASE_HOST', 'DATABASE_PORT', 'DATABASE_NAME') if values.get(key)}
        for key in ('DATABASE_URL', 'DB_URL'):
            if values.get(key) and values[key].lower() not in ('null', '(null)', 'none'):
                try:
                    url = urlsplit(values[key])
                    endpoint[key] = {'scheme': url.scheme, 'host': url.hostname, 'port': url.port, 'database': url.path.lstrip('/')}
                except ValueError:
                    endpoint[key] = {'unparseable': True}
        engine = None
        image_name = row['Config'].get('Image', '').rsplit('/', 1)[-1].split(':')[0]
        if 'postgres' in image_name: engine = 'pgsql'
        elif image_name in ('mysql', 'mariadb'): engine = 'mysql'
        directory, port, direct_database = None, None, None
        if engine and row['State']['Running']:
            script = ('exec psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}" -Atc "SELECT current_setting(\'data_directory\'), current_setting(\'port\'), current_database()"'
                      if engine == 'pgsql' else 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}" exec mariadb -uroot -Nse "SELECT @@datadir, @@port"')
            try:
                output = command([docker, 'exec', row['Id'], 'sh', '-c', script]).strip().replace('|', '\t').split('\t')
                if len(output) in (2,3) and output[0].startswith('/'):
                    directory, port = posixpath.normpath(output[0]), int(output[1])
                    if engine == 'pgsql' and len(output)==3 and 1<=len(output[2])<=128 and not any(ord(c)<32 for c in output[2]):direct_database=output[2]
            except Exception:
                # No stderr, command output or credential value becomes evidence.
                directory, port = None, None
        result.append({'id': row['Id'], 'name': row['Name'].lstrip('/'), 'image_id': row['Image'],
                       'database_data_directory': directory, 'database_port': port,
                       'direct_database': {'engine':'pgsql','database':direct_database,'port':port} if direct_database is not None else None,
                       'running': bool(row['State']['Running']), 'compose_project': labels.get('com.docker.compose.project'),
                       'compose_service': labels.get('com.docker.compose.service'),
                       'compose_files': labels.get('com.docker.compose.project.config_files', '').split(','),
                       'database_endpoint': endpoint,
                       'networks': {name: {'id': net.get('NetworkID'), 'aliases': net.get('Aliases') or [], 'ip': net.get('IPAddress')}
                                    for name, net in row.get('NetworkSettings', {}).get('Networks', {}).items()},
                       'mounts': [{'type': m['Type'], 'source': m.get('Source'), 'destination': m['Destination']}
                                  for m in row.get('Mounts', [])]})
    return {'observed': observed, 'containers': result}


def public(evidence):
    # Host storage paths remain worker-private; browser gets verifiable identities.
    return {key: evidence[key] for key in ('project', 'environment', 'project_revision', 'environment_revision',
             'fingerprint', 'state', 'containers')} | {'databases': [{key: value for key, value in d.items() if key != 'storage'}
                                                                  for d in evidence['databases']]} | {key:evidence[key] for key in ('selected_services','selected_recipe_revision') if key in evidence}


def _files(values):
    return ordered_files(values)


def _endpoint(row):
    values = row.get('database_endpoint') or {}
    for key in ('DATABASE_URL', 'DB_URL'):
        url = values.get(key)
        if url and url.get('scheme'):
            engine = url.get('scheme')
            return engine, url.get('host'), url.get('database'), url.get('port') or (5432 if engine.startswith('postgres') else 3306 if engine.startswith('mysql') else None)
        if url and url.get('unparseable'):
            raise ValueError('A database endpoint cannot be parsed.')
    engine = values.get('DB_CONNECTION')
    raw_port = values.get('DB_PORT') or values.get('DATABASE_PORT') or (5432 if engine in ('pgsql', 'postgresql') else 3306)
    try: port = int(raw_port)
    except (ValueError, TypeError): raise ValueError('Invalid database port.') from None
    if not 0 < port < 65536: raise ValueError('Invalid database port.')
    return (engine, values.get('DB_HOST') or values.get('DATABASE_HOST'),
            values.get('DB_DATABASE') or values.get('DATABASE_NAME') or values.get('DB_NAME'), port)


def _resolve(rows, client, host, port):
    return [other for other in rows if other.get('database_port') == port and any(
        name in other.get('networks', {}) and net.get('id') == other['networks'][name].get('id')
        and host in (other['networks'][name].get('aliases') or []) + [other['name'], other['networks'][name].get('ip')]
        for name, net in client.get('networks', {}).items())]


def _storage(row, path=None):
    mounts = [m for m in row.get('mounts', []) if m.get('type') in ('volume', 'bind') and m.get('source')]
    if path:
        if not path.startswith('/') or posixpath.normpath(path) != path:
            raise ValueError('SQLite must have an absolute normalized persistent file path.')
        mounts = [m for m in mounts if path == m.get('destination') or path.startswith(m.get('destination', '').rstrip('/') + '/')]
    return sorted([{'type': m['type'], 'source': m['source'], 'destination': m['destination']} for m in mounts], key=lambda m: m['destination'])


def preview(db, project, environment, inventory, *, now=None, resolver=None, services=None, recipe_revision=None):
    now = time.time() if now is None else now
    registered = registry.get_project(db, project)
    target = registry.get_environment(db, project, environment)
    # Accepted provisioning owns its environment even before a runtime recipe exists.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_services'").fetchone():
        if db.execute("SELECT 1 FROM managed_services WHERE project=? AND environment=? AND state IN ('queued','provisioning','recovery-required')", (project, environment)).fetchone():
            raise ValueError('Service creation or recovery already owns this environment. Complete it before adopting another workload.')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_reservations'").fetchone():
        if db.execute("SELECT 1 FROM service_reservations WHERE project=? AND environment=? AND state='held'", (project, environment)).fetchone():
            raise ValueError('Accepted service capacity is reserved in this environment. Reconcile it before adoption.')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workload_adoptions'").fetchone() and db.execute('SELECT 1 FROM workload_adoptions WHERE project=? AND environment=?',(project,environment)).fetchone():
        raise ValueError('This environment workload is already adopted.')
    if target['recipe_revision'] is not None or target['state'] != 'not-provisioned':
        raise ValueError('This environment already owns a workload.')
    observed = inventory.get('observed')
    if not isinstance(observed, (float, int)) or not 0 <= now - observed <= 90:
        raise ValueError('Refresh live Docker inventory before adopting a workload.')
    projects = registry.legacy_projects(db)
    legacy = json.loads(db.execute('SELECT legacy_document FROM registry_projects WHERE id=?',(project,)).fetchone()[0] or '{}')
    recipe = legacy.get('recipe')
    if recipe_revision is not None:
        registry.revision_number(recipe_revision)
        record=db.execute('SELECT document,digest FROM registry_recipes WHERE id=? AND project=?',(recipe_revision,project)).fetchone()
        if record is None:raise ValueError('Select an existing recipe owned by this project.')
        recipe=json.loads(record['document'])
        if not isinstance(recipe,dict) or _digest(recipe)!=record['digest']:raise ValueError('Selected recipe integrity is invalid.')
    if not recipe:
        raise ValueError('The existing workload has no deployment recipe.')
    prefixes = legacy.get('container_prefixes', [])
    rows = inventory.get('containers', [])
    members = [r for r in rows if any(r.get('name', '').startswith(p) for p in prefixes)]
    if recipe_revision is not None:
        members=[r for r in rows if r.get('compose_project')==recipe.get('compose_project') and r.get('compose_service') in recipe.get('services',[]) and (not prefixes or any(r.get('name','').startswith(p) for p in prefixes))]
    if services is not None:
        if (not isinstance(services,list) or not services or len(services)>100 or any(not isinstance(v,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*',v) for v in services) or len(set(services))!=len(services) or not set(services)<=set(recipe.get('services',[]))):raise ValueError('Select unique services from the registered recipe.')
        services=sorted(services)
        members=[r for r in members if r.get('compose_service') in services]
    selected_services=services
    if not members:
        raise ValueError('No existing containers match this registered workload.')
    expected_files = _files(recipe.get('compose_files'))
    service_provenance = {}
    for row in members:
        if not re.fullmatch('[0-9a-f]{64}', row.get('id', '')) or not re.fullmatch('sha256:[0-9a-f]{64}', row.get('image_id', '')):
            raise ValueError('Full immutable container and image identities are required.')
        if row.get('compose_project') != recipe.get('compose_project'):
            raise ValueError('Container ownership conflicts with its registered Compose configuration.')
        owners = [name for name, p in projects.items() if any(row['name'].startswith(prefix) for prefix in p.get('container_prefixes', []))]
        if owners != [project] and not (recipe_revision is not None and not owners):
            raise ValueError('A container has ambiguous project ownership.')
    services = [r.get('compose_service') for r in members]
    if len(services) != len(set(services)) or not all(isinstance(s, str) and s for s in services):
        raise ValueError('Compose services must have unique container membership.')
    if set(selected_services if selected_services is not None else recipe.get('services', [])) - set(services):
        raise ValueError('Some deployment services are missing from the actual workload.')
    for row in members:
        observed_files = _files(row.get('compose_files'))
        if observed_files == expected_files:
            continue
        if not callable(resolver):
            raise ValueError('Container ownership conflicts with its registered Compose configuration.')
        # This callback is supplied by the worker, never from request/inventory
        # proof fields. A successful comparison remains private adoption evidence.
        resolved = resolver(recipe, row['compose_service'], list(observed_files))
        if (not isinstance(resolved, dict) or resolved.get('equivalent') is not True
                or resolved.get('mismatches') != [] or not isinstance(resolved.get('proof'), dict)):
            raise ValueError('Alternate Compose configuration is not verified equivalent.')
        from provenance import validate_proof
        proof = validate_proof(recipe, row['compose_service'], resolved['proof'])
        if (proof is None or _files(proof['observed_files']) != observed_files
                or resolved.get('digest') != proof['semantic_digest']):
            raise ValueError('Compose verification does not match the observed invocation.')
        service_provenance[row['compose_service']] = proof
    databases = []
    direct_databases = []
    member_ids = {r['id'] for r in members}
    for row in members:
        engine, host, database, port = _endpoint(row)
        if not host and not database:
            direct=row.get('direct_database')
            if direct is None:continue
            if (not isinstance(direct,dict) or set(direct)!={'engine','database','port'} or direct['engine']!='pgsql'
                    or not isinstance(direct['database'],str) or not 1<=len(direct['database'])<=128
                    or any(ord(c)<32 for c in direct['database']) or type(direct['port']) is not int or not 1<=direct['port']<=65535
                    or direct['port']!=row.get('database_port') or not row.get('database_data_directory') or not row.get('running')):
                raise ValueError('Direct database engine identity is unavailable.')
            storage=_storage(row,row['database_data_directory'])
            if not storage:raise ValueError('Database container has no verified persistent storage.')
            direct_databases.append({'client':row['id'],'container_id':row['id'],'host':row['name'],
                              'engine':direct['engine'],'database':direct['database'],'port':direct['port'],'storage':storage})
            continue
        if engine == 'sqlite':
            storage = _storage(row, database or '')
            if not storage:
                raise ValueError('SQLite data is not mapped to persistent storage.')
            databases.append({'client': row['id'], 'engine': engine, 'database': database, 'storage': storage})
            continue
        if not host or not database:
            raise ValueError('A database endpoint has incomplete host/database identity.')
        matches = _resolve(rows, row, host, port)
        if len(matches) != 1 or matches[0]['id'] not in member_ids:
            raise ValueError('Database endpoint is shared, external, ambiguous or outside this workload.')
        database_row = matches[0]
        directory = database_row.get('database_data_directory')
        if not directory:
            raise ValueError('The database engine data directory could not be read. Refresh its live identity before adoption.')
        storage = _storage(database_row, directory)
        if not storage:
            raise ValueError('Database container has no verified persistent storage.')
        databases.append({'client': row['id'], 'engine': engine, 'host': host, 'port': port, 'database': database,
                          'container_id': database_row['id'], 'storage': storage})
    referenced={d.get('container_id') for d in databases}
    databases.extend(d for d in direct_databases if d['container_id'] not in referenced)
    identities = {(d.get('container_id'), d.get('port'), d['database']) for d in databases if d.get('container_id')}
    for other in rows:
        if other['id'] in member_ids: continue
        engine, host, database, port = _endpoint(other)
        if not host or not database: continue
        if any((match['id'], port, database) in identities for match in _resolve(rows, other, host, port)):
            raise ValueError('Another workload uses the same database identity.')
    # A datastore shared with any other container may not be silently claimed.
    sources = {m['source'] for d in databases for m in d['storage']}
    if any(r['id'] not in member_ids and sources.intersection(m['source'] for m in _storage(r)) for r in rows):
        raise ValueError('Persistent database storage is shared with another workload.')
    import adopted_workloads, provenance
    claims=list(db.execute('SELECT e.project,e.name,r.document,r.digest FROM registry_environments e JOIN registry_recipes r ON r.id=e.recipe_revision AND r.project=e.project'))
    for retained in adopted_workloads.all_owners(db):
        claims.append({'project':retained['project'],'name':retained['environment'],'document':registry.canonical(retained['recipe']),'digest':retained['recipe_digest']})
    for claim in claims:
        claimed_recipe=json.loads(claim['document'])
        if _digest(claimed_recipe)!=claim['digest']:raise ValueError('Registered owner recipe integrity is invalid.')
        if claim['project']==project and claim['name']==environment:continue
        if any(provenance.matches(claimed_recipe,r['compose_project'],r['compose_service'],r['compose_files']) for r in members):raise ValueError('Another registered environment owns this Compose service.')
    selected = sorted([{'id': r['id'], 'name': r['name'], 'image_id': r['image_id'],
                        'service': r['compose_service'], 'running': bool(r['running'])} for r in members], key=lambda r: r['name'])
    result = {'project': project, 'environment': environment, 'project_revision': registered['revision'],
              'environment_revision': target['revision'], 'recipe_digest': _digest(recipe),
              'containers': selected, 'databases': sorted(databases, key=lambda d: d['client']),
              'state': 'active' if all(r['running'] for r in selected) else 'stopped' if not any(r['running'] for r in selected) else 'degraded'}
    if service_provenance:
        result['service_provenance'] = service_provenance
    if selected_services is not None:result['selected_services']=selected_services
    if recipe_revision is not None:result['selected_recipe_revision']=recipe_revision
    _conflicts(db,project,environment,result)
    result['fingerprint'] = _digest(result)
    return result


def recover(db, project, environment, fingerprint, actor, *, services=None, recipe_revision=None):
    """Read an exact accepted request without Docker access or new adoption writes."""
    registry.identifier(project)
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError('An authenticated actor is required.')
    if recipe_revision is not None:
        registry.revision_number(recipe_revision)
    if services is not None and (not isinstance(services, list) or not services or len(services) > 100 or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', v) for v in services) or len(set(services)) != len(services)):
        raise ValueError('Select unique valid services for recovery.')
    if environment not in registry.ENVIRONMENTS or not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
        raise ValueError('A valid environment and reviewed fingerprint are required.')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workload_adoptions'").fetchone():
        return None
    previous = db.execute('SELECT environment,fingerprint,actor,evidence FROM workload_adoptions WHERE project=? AND environment=?', (project, environment)).fetchone()
    if previous is None:
        return None
    if (previous['environment'], previous['fingerprint'], previous['actor']) != (environment, fingerprint, actor):
        raise ValueError('This workload was adopted with a different reviewed identity.')
    try:
        prior = json.loads(previous['evidence'])
        if prior['project'] != project or prior['environment'] != environment or prior['fingerprint'] != fingerprint or _digest({k:v for k,v in prior.items() if k != 'fingerprint'}) != fingerprint:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('The accepted adoption evidence could not be verified.') from None
    if prior.get('selected_services') != (sorted(services) if isinstance(services,list) else services) or prior.get('selected_recipe_revision') != recipe_revision:
        raise ValueError('Adoption replay selection differs from the accepted request.')
    return registry.get_project(db, project)


def adopt(db, project, environment, fingerprint, inventory, actor, *, now=None, resolver=None, services=None, recipe_revision=None):
    initialize(db)
    with registry.transaction(db):
        accepted = recover(db, project, environment, fingerprint, actor, services=services, recipe_revision=recipe_revision)
        if accepted is not None:
            return accepted
        evidence = preview(db, project, environment, inventory, now=now, resolver=resolver, services=services, recipe_revision=recipe_revision)
        if evidence['fingerprint'] != fingerprint:
            raise ValueError('Workload identity or settings changed. Review the refreshed adoption preview.')
        recipe = db.execute('SELECT id,document FROM registry_recipes WHERE project=? AND digest=?', (project, evidence['recipe_digest'])).fetchone()
        if recipe is None:
            raise ValueError('The verified recipe is unavailable.')
        document = json.loads(recipe['document'])
        if 'selected_services' in evidence:document['services']=evidence['selected_services']
        document['owned_services'] = sorted(r['service'] for r in evidence['containers'])
        if evidence.get('service_provenance'):
            document['service_provenance'] = evidence['service_provenance']
        digest = _digest(document)
        db.execute('INSERT OR IGNORE INTO registry_recipes(project,digest,document,created) VALUES(?,?,?,?)',
                   (project, digest, registry.canonical(document), time.time()))
        recipe_id = db.execute('SELECT id FROM registry_recipes WHERE project=? AND digest=?', (project, digest)).fetchone()['id']
        db.execute('UPDATE registry_projects SET legacy_binding=COALESCE(legacy_binding,?),revision=revision+1,updated=? WHERE id=?', (environment, time.time(), project))
        db.execute('UPDATE registry_environments SET state=?,recipe_revision=?,revision=revision+1 WHERE project=? AND name=?', (evidence['state'], recipe_id, project, environment))
        db.execute('INSERT INTO workload_adoptions VALUES(?,?,?,?,?,?)', (project, environment, fingerprint, registry.canonical(evidence), actor, time.time()))
        import adopted_workloads
        adopted_workloads.retain(db, project, environment)
        return registry.get_project(db, project)
