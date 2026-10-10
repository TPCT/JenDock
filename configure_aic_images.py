"""Parameterize current AIC image references, proving resolved config is identical.

Run inside the trusted worker. This does not recreate services or change app env
files, AWS settings, volumes, ports, commands or database configuration.
"""
import json
import re
import subprocess
from pathlib import Path

from worker import COMPOSE, replace_env

root = Path('/opt/docker-engine/projects/aic')
files = [root / name for name in ('compose.yml', 'academy.yml', 'sys.yml', 'ops.yml')]
env = (root / 'ports.env').resolve(strict=True)
command = [COMPOSE, '-p', 'aic', '--env-file', str(env)]
for path in files:
    command += ['-f', str(path)]
command += ['config', '--format', 'json']


def resolved():
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def write(path, text):
    temporary = path.with_name(path.name + '.parameterize-tmp')
    temporary.write_text(text)
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)


before = resolved()
originals = {path: path.read_text() for path in [*files, env]}
updates = {}
try:
    for path in files:
        def substitute(match):
            name, revision = match.group(1), match.group(2)
            key = name.upper().replace('-', '_') + '_IMAGE'
            updates[key] = name + ':' + revision
            return 'image: ${' + key + ':?Set the committed image}'
        new = re.sub(r'image: (aic-(?:website-(?:backend|frontend|reverb)|(?:academy|sys|ops)-(?:app|web))):([a-f0-9]{7,40})\b',
                     substitute, originals[path])
        write(path, new)
    assert len(updates) == 9, 'Unexpected existing image layout; no change retained'
    replace_env(env, updates)
    assert resolved() == before, 'Resolved Compose configuration changed'
except BaseException:
    for path, text in originals.items():
        write(path, text)
    raise
print(json.dumps({'image_variables_added': sorted(updates), 'resolved_compose_unchanged': True,
                  'services_recreated': False, 'application_env_files_changed': False}))
