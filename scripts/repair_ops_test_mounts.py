"""Correct Ops test tmpfs ownership to match its non-root Docker image user.

Run on the verified server with --apply; defaults to a read-only preview.
"""
import json
import os
from pathlib import Path
import sys


def main():
    path = Path('/opt/docker-engine/projects/deploy-monitor/policy/projects.json')
    original = path.read_bytes()
    data = json.loads(original)
    tests = data['aic-ops']['recipe']['tests']
    assert len(tests) == 1 and tests[0]['image'] == 'deploy-test-aic-ops'
    mounts = tests[0]['tmpfs']
    expected = {'/var/www/html/storage', '/var/www/html/bootstrap/cache'}
    assert {mount.split(':', 1)[0] for mount in mounts} == expected
    corrected = []
    for mount in mounts:
        target, options = mount.split(':', 1)
        options = options.split(',')
        for name, desired in [('uid', '999'), ('gid', '988')]:
            matches = [i for i, option in enumerate(options) if option.startswith(name + '=')]
            assert len(matches) == 1
            i = matches[0]
            assert options[i].split('=', 1)[1] in ('65534', desired)
            options[i] = name + '=' + desired
        corrected.append(target + ':' + ','.join(options))
    assert tests[0].get('database') in (None, 'postgres')
    print(json.dumps({'project': 'aic-ops', 'tmpfs': corrected, 'database': 'postgres'}))
    if '--apply' not in sys.argv or (mounts == corrected and tests[0].get('database') == 'postgres'):
        return
    tests[0]['tmpfs'] = corrected
    tests[0]['database'] = 'postgres'
    assert path.read_bytes() == original, 'Configuration changed during review'
    stat = path.stat()
    temporary = path.with_name('.projects-ops-mounts.tmp')
    try:
        with temporary.open('x') as handle:
            os.fchmod(handle.fileno(), stat.st_mode)
            os.fchown(handle.fileno(), stat.st_uid, stat.st_gid)
            handle.write(json.dumps(data, indent=2) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    assert json.loads(path.read_text())['aic-ops']['recipe']['tests'][0]['tmpfs'] == corrected
    print('Ops isolated test mount ownership and PostgreSQL recipe verified.')


if __name__ == '__main__':
    main()
