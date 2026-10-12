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
    test_defaults = {
        'JWT_SECRET': 'ci-isolated-test-jwt-secret-not-for-production',
        'APP_TIMEZONE': 'Africa/Cairo', 'APP_URL': 'http://localhost',
        'APP_DEBUG': 'false', 'BROADCAST_CONNECTION': 'null', 'BROADCAST_DRIVER': 'null',
        'FILESYSTEM_DISK': 'local', 'PUBLIC_FILESYSTEM_DRIVER': 'local',
        'LIVEWIRE_TEMPORARY_FILE_UPLOAD_DISK': 'local', 'VISIT_PDF_RENDER_SUBPROCESS': 'false',
    }
    environment = tests[0]['environment']
    defaults_ready = all(key in environment for key in test_defaults)
    print(json.dumps({'project': 'aic-ops', 'tmpfs': corrected, 'database': 'postgres'}))
    command = tests[0]['command']
    assert len(command) == 3 and command[:2] == ['sh', '-c']
    assert 'php vendor/bin/phpunit' in command[2]
    command_ready = '--do-not-cache-result' in command[2]
    if '--apply' not in sys.argv or (mounts == corrected and tests[0].get('database') == 'postgres' and command_ready and defaults_ready):
        return
    tests[0]['tmpfs'] = corrected
    tests[0]['database'] = 'postgres'
    for key, value in test_defaults.items():
        environment.setdefault(key, value)
    if not command_ready:
        command[2] = command[2].replace('php vendor/bin/phpunit', 'php vendor/bin/phpunit --do-not-cache-result')
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
