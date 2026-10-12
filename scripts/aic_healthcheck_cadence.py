"""Reduce repeated AIC readiness bootstraps while preserving failure time budgets.

Default: preview only. --apply validates Compose and recreates only the five
application services, pinning each existing image through its normal env key.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import yaml

DOCKER = ['/opt/docker-engine/29.8.2/docker/docker', '-H', 'unix:///run/docker-isolation.sock']
ROOT = Path('/opt/docker-engine/projects/aic')
FILES = ['compose.yml', 'academy.yml', 'sys.yml', 'ops.yml']
TARGETS = ['aic-ops-app-1', 'aic-sys-app-1', 'aic-academy-app-1',
           'aic-website-api-1', 'aic-website-frontend-1']


def change_service(source, service):
    original = yaml.safe_load(source)
    expected = copy.deepcopy(original)
    health = expected['services'][service]['healthcheck']
    assert health['interval'] in ('10s', '15s', '30s')
    retries = math.ceil(int(health['interval'][:-1]) * health['retries'] / 30)
    health.update(interval='30s', retries=retries, start_period='20s', start_interval='5s')
    pattern = r'(?ms)^  ' + re.escape(service) + r':\n.*?(?=^  [a-zA-Z0-9_-]+:|^\S|\Z)'
    matches = list(re.finditer(pattern, source))
    assert len(matches) == 1, 'Unexpected service layout'
    match = matches[0]
    block = match.group()
    hp = r'(?ms)^    healthcheck:\n.*?(?=^    [a-zA-Z0-9_-]+:|\Z)'
    hm = list(re.finditer(hp, block))
    assert len(hm) == 1, 'Unexpected healthcheck layout'
    old = hm[0].group()
    new = old
    for key in ('interval', 'retries', 'start_period', 'start_interval'):
        line = f'      {key}: {health[key]}\n'
        entry = r'(?m)^      ' + key + r':[^\n]*\n'
        if re.search(entry, new):
            new = re.sub(entry, line, new, count=1)
        else:
            new += line
    changed = block[:hm[0].start()] + new + block[hm[0].end():]
    result = source[:match.start()] + changed + source[match.end():]
    assert yaml.safe_load(result) == expected, 'Unexpected configuration change'
    return result


def main():
    rows = json.loads(subprocess.check_output(DOCKER + ['inspect', *TARGETS]))
    originals, planned, targets, pins = {}, {}, [], {}
    for row in rows:
        assert row['State']['Running'] and row['State']['Health']['Status'] == 'healthy'
        labels = row['Config']['Labels']
        assert labels['com.docker.compose.project'] == 'aic'
        service = labels['com.docker.compose.service']
        matches = []
        for filename in FILES:
            path = ROOT / filename
            source = planned.get(path, path.read_text())
            definition = yaml.safe_load(source).get('services', {}).get(service)
            if definition and 'healthcheck' in definition:
                matches.append((path, source, definition))
        assert len(matches) == 1
        path, source, definition = matches[0]
        key = re.fullmatch(r'\$\{([A-Z_]+):\?[^}]+\}', definition['image'])
        assert key, 'Image must use its normal configurable reference'
        pins[key[1]] = row['Config']['Image']
        originals.setdefault(path, path.read_text())
        planned[path] = change_service(source, service)
        targets.append((service, row['Name'], row['Image']))
    print(json.dumps({'services': [x[0] for x in targets], 'interval_seconds': 30,
                      'readiness_checks_per_minute_before': 28, 'after': 10,
                      'existing_images_preserved': True}))
    if '--apply' not in sys.argv:
        return
    db = sqlite3.connect('file:/opt/docker-engine/data-overlay2/volumes/deploy-monitor_state/_data/state.sqlite?mode=ro', uri=True)
    assert not db.execute("SELECT id FROM jobs WHERE status IN ('queued','building','testing','deploying','recovering')").fetchall(), 'Wait for active deployments'
    command = ['/opt/docker-engine/tools/docker-compose', '--env-file', 'ports.env', '-p', 'aic']
    for filename in FILES:
        command += ['-f', filename]
    env = dict(os.environ, DOCKER_HOST='unix:///run/docker-isolation.sock', **pins)
    try:
        for path, value in planned.items():
            assert path.read_text() == originals[path], 'Concurrent configuration change'
            path.write_text(value)
        validation = subprocess.run(command + ['config', '--format', 'json'], cwd=ROOT, env=env, capture_output=True)
        assert validation.returncode == 0, 'Compose configuration validation failed'
    except BaseException:
        for path, value in originals.items():
            if path.read_text() == planned[path]:
                path.write_text(value)
        raise
    for service, name, image in targets:
        subprocess.run(command + ['up', '-d', '--no-deps', '--wait', '--wait-timeout', '120', service],
                       cwd=ROOT, env=env, check=True)
        row = json.loads(subprocess.check_output(DOCKER + ['inspect', name]))[0]
        assert row['Image'] == image, 'Image changed unexpectedly'
        assert row['Config']['Healthcheck']['Interval'] == 30_000_000_000
        assert row['State']['Health']['Status'] == 'healthy'
        print(service + ': healthy, original image preserved, interval verified')


if __name__ == '__main__':
    main()
