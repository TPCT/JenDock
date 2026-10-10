"""Run on the owner's workstation with existing SSH and gh authentication.

Only approved repositories in projects.json are touched. No secret is printed
or written locally. Existing matching hooks are reused, never duplicated.
"""
import json
import subprocess
from pathlib import Path

URL = 'https://monitoring.shiftcodes.net/github'
LEGACY_URL = 'https://shiftcodes.net/_deploy/github'


def api(path, payload=None, method="POST"):
    command = ['gh', 'api', path]
    if payload is not None:
        command += ['--method', method, '--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('GitHub operation failed; inspect repository permissions and hook state before retrying')
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    projects = json.loads(Path(__file__).with_name('projects.json').read_text())
    command = ('DOCKER_HOST=unix:///run/docker-isolation.sock /opt/docker-engine/29.8.2/docker/docker '
               'exec deploy-monitor-receiver-1 python -c '
               "'import json; print(json.load(open(\"/config/credentials.json\"))[\"webhook_secret\"])'")
    result = subprocess.run(['ssh', '-S', '/tmp/codex-docker-retry-%C', '-p', '22',
                             '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             '-o', 'StrictHostKeyChecking=yes', 'root@176.118.198.49', command],
                            capture_output=True, text=True, check=True)
    secret = result.stdout.strip()
    assert len(secret) >= 48
    inventory = {}
    for name, project in projects.items():
        repository = project['repository']
        endpoint = 'repos/' + repository + '/hooks'
        matches = [h for h in api(endpoint) if h.get('config', {}).get('url') in (URL, LEGACY_URL)]
        assert len(matches) <= 1, 'Duplicate hooks require reconciliation'
        if matches:
            hook = matches[0]
            assert hook['active'] and 'push' in hook['events'], 'Existing hook is not active for pushes'
            if hook['config']['url'] != URL:
                hook = api(endpoint + '/' + str(hook['id']), {'config': {'url': URL, 'content_type': 'json', 'secret': secret, 'insecure_ssl': '0'}}, method='PATCH')
        else:
            hook = api(endpoint, {'name': 'web', 'active': True, 'events': ['push'],
                                  'config': {'url': URL, 'content_type': 'json', 'secret': secret, 'insecure_ssl': '0'}})
        inventory[name] = {'repository': repository, 'id': hook['id'], 'active': hook['active'], 'url': URL}
        Path(__file__).with_name('hooks.json').write_text(json.dumps(inventory, indent=2) + '\n')
        print(name, hook['id'], 'active', flush=True)


if __name__ == '__main__':
    main()
