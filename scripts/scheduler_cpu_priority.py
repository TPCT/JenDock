"""Lower background CPU priority, preserving quotas, schedules and Compose layout.
Run on the verified VPS with --apply. Default mode reports changes only.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import yaml

DOCKER = ['/opt/docker-engine/29.8.2/docker/docker', '-H', 'unix:///run/docker-isolation.sock']
COMPOSE = '/opt/docker-engine/tools/docker-compose'
TARGETS = ['aic-sys-scheduler-1', 'aic-ops-scheduler-1', 'aic-academy-scheduler-1',
           'etsawaa-scheduler-1', 'lms-v2-scheduler-1', 'octa-scheduler-1',
           'elfak-elfak01-scheduler-1']

def main():
    rows = json.loads(subprocess.check_output(DOCKER + ['inspect', *TARGETS]))
    originals, planned, projects, updates = {}, {}, {}, []
    for row in rows:
        assert row['State']['Running'], 'Target is not running'
        labels = row['Config']['Labels']
        service, project = labels['com.docker.compose.service'], labels['com.docker.compose.project']
        files = labels['com.docker.compose.project.config_files'].split(',')
        candidates = []
        for name in files:
            path = Path(name).resolve()
            assert path.is_relative_to('/opt/docker-engine/projects'), 'Unexpected Compose path'
            raw = planned.get(path, path.read_text())
            services = (yaml.safe_load(raw) or {}).get('services', {})
            if service in services:
                candidates.append((path, raw, services[service]))
        assert candidates, 'Compose service is missing: '+project+'/'+service
        path, raw, definition = candidates[-1]
        old = row['HostConfig']['CpuShares']
        assert old in (0, 256), 'Preserve an existing custom priority'
        assert definition.get('cpu_shares', 0) in (0, 256), 'Preserve configured priority'
        if definition.get('cpu_shares') != 256:
            pattern = r'(?m)^(  '+re.escape(service)+r':[^\n]*\n)'
            assert len(re.findall(pattern, raw)) == 1, 'Unsupported service layout'
            originals.setdefault(path, path.read_text())
            planned[path] = re.sub(pattern, r'\g<1>    cpu_shares: 256\n', raw, count=1)
        projects[project] = files
        updates.append((row['Id'], row['Name'], old, row['HostConfig']['NanoCpus']))
    print(json.dumps({'targets': [name for _, name, _, _ in updates], 'cpu_shares': 256, 'quota_changes': False}))
    if '--apply' not in sys.argv:
        return
    changed = []
    try:
        for path, value in planned.items():
            path.write_text(value)
        env = dict(os.environ, DOCKER_HOST='unix:///run/docker-isolation.sock')
        for project, files in projects.items():
            args = [COMPOSE, '-p', project]
            folder = Path(files[0]).parent
            if (folder / 'ports.env').exists():
                args += ['--env-file', str(folder / 'ports.env')]
            for name in files:
                args += ['-f', name]
            result = subprocess.run(args + ['config', '--format', 'json'], cwd=folder, env=env, capture_output=True)
            assert result.returncode == 0, 'Compose validation failed; configuration restored'
        for identifier, name, old, quota in updates:
            subprocess.run(DOCKER + ['update', '--cpu-shares', '256', identifier], check=True, stdout=subprocess.DEVNULL)
            changed.append((identifier, old))
            observed = json.loads(subprocess.check_output(DOCKER + ['inspect', identifier]))[0]
            assert observed['HostConfig']['CpuShares'] == 256 and observed['HostConfig']['NanoCpus'] == quota
        print('Verified priority on all scheduler containers; quotas and running processes preserved')
    except BaseException:
        for path, value in originals.items():
            path.write_text(value)
        for identifier, old in reversed(changed):
            subprocess.run(DOCKER + ['update', '--cpu-shares', str(old), identifier], stdout=subprocess.DEVNULL)
        raise

if __name__ == '__main__':
    main()
