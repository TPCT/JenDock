"""Revisioned resource budgets; saved allocations are not runtime enforcement."""
import copy
import json
import math
import re
import time

ENVIRONMENTS = ('development', 'production')


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS resource_policies(project TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL, updated REAL NOT NULL)')


def read(db, project):
    initialize(db)
    row = db.execute('SELECT * FROM resource_policies WHERE project=?', (project,)).fetchone()
    if row:
        return dict(project=project, revision=row['revision'], document=json.loads(row['document']), updated=row['updated'])
    return dict(project=project, revision=0, updated=None, document={'budget': {}, 'environments': {e: {'budget': {}, 'services': {}} for e in ENVIRONMENTS}})


def number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(label + ' is outside the supported range.')
    return value


def budget(value):
    if not isinstance(value, dict) or set(value) - {'memory_mib', 'cpu'}:
        raise ValueError('Invalid resource budget.')
    for k, v in value.items():
        number(v, k, 16 if k == 'memory_mib' else 0.1, 1048576 if k == 'memory_mib' else 1024 if k == 'cpu' else 1048576)
    return value


def validate(document):
    if not isinstance(document, dict) or set(document) != {'budget', 'environments'}:
        raise ValueError('Invalid resource policy.')
    total = budget(document['budget'])
    envs = document['environments']
    if not isinstance(envs, dict) or set(envs) != set(ENVIRONMENTS):
        raise ValueError('Development and Production budgets are required.')
    seen = set()
    for environment, data in envs.items():
        if not isinstance(data, dict) or set(data) != {'budget', 'services'}:
            raise ValueError('Invalid environment allocation.')
        ceiling = budget(data['budget'])
        services = data['services']
        if not isinstance(services, dict) or len(services) > 100:
            raise ValueError('Invalid service allocations.')
        for name, limits in services.items():
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name) or name in seen:
                raise ValueError('A service must belong to exactly one environment.')
            seen.add(name)
            if not isinstance(limits, dict) or set(limits) != {'memory_mib', 'cpu'}:
                raise ValueError('Each service needs RAM and CPU limits.')
            budget(limits)
        for field in ('memory_mib', 'cpu'):
            allocated = sum(x[field] for x in services.values())
            if allocated and (field not in ceiling or allocated > ceiling[field] + 1e-8):
                raise ValueError(environment + ' service allocations exceed its ' + field + ' budget.')
    for field in ('memory_mib', 'cpu'):
        allocated = sum(e['budget'].get(field, 0) for e in envs.values())
        if allocated and (field not in total or allocated > total[field] + 1e-8):
            raise ValueError('Environment allocations exceed the project ' + field + ' budget.')


def save(db, project, revision, document):
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid policy revision.')
    document = copy.deepcopy(document)
    validate(document)
    initialize(db)
    db.execute('BEGIN IMMEDIATE')
    try:
        current = read(db, project)
        if db.execute("SELECT 1 FROM control_runs WHERE project=? AND status='running' AND operation IN ('resource-apply','container-limits-save')", (project,)).fetchone():
            raise ValueError('Resource application is running. Wait before changing the budget.')
        if current['revision'] != revision:
            raise ValueError('Resource settings changed. Reload before saving.')
        result = dict(project=project, revision=revision + 1, document=document, updated=time.time())
        db.execute('INSERT OR REPLACE INTO resource_policies VALUES(?,?,?,?)', (project, result['revision'], json.dumps(document), result['updated']))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return result


def allocation(db, project, environment, container, revision):
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid policy revision.')
    if not isinstance(environment, str) or environment not in ENVIRONMENTS:
        raise ValueError('Choose Development or Production.')
    if not isinstance(container, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container):
        raise ValueError('Invalid service name.')
    current = read(db, project)
    if current['revision'] != revision:
        raise ValueError('Resource settings changed. Reload before applying.')
    validate(current['document'])
    limits = current['document']['environments'].get(environment, {}).get('services', {}).get(container)
    if not limits:
        raise ValueError('Save this service allocation in the selected environment first.')
    return copy.deepcopy(limits)
