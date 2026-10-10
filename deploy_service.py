"""Install this product's committed config/images only when its worker is idle."""
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
def source(name):
    return subprocess.check_output(['git', '-C', str(root), 'show', sha + ':' + name], text=True)
payload = {'compose': source('compose.yml'), 'policy': json.loads(source('projects.json')), 'version': sha[:7]}
script = '''import json,sqlite3
from pathlib import Path
from worker import replace_env
payload=json.loads(''' + repr(json.dumps(payload)) + ''')
db=sqlite3.connect('/state/state.sqlite')
assert db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','validation-queued','building','testing','deploying','recovering')").fetchone()[0]==0,'Worker busy; do not interrupt'
root=Path('/opt/docker-engine/projects/deploy-monitor');policy=root/'policy';policy.mkdir(exist_ok=True);policy.chmod(0o755)
p=policy/'projects.json';t=policy/'projects.next.json';t.write_text(json.dumps(payload['policy'],ensure_ascii=False,indent=2));t.chmod(0o644);t.replace(p)
p=root/'compose.yml';t=root/'compose.next.yml';t.write_text(payload['compose']);t.chmod(0o644);t.replace(p)
replace_env(root/'.env',{'DEPLOY_MONITOR_VERSION':payload['version']})
print('Monitor configuration installed')
'''
ssh = ['ssh', '-S', '/tmp/codex-docker-retry-%C', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
       '-o', 'StrictHostKeyChecking=yes', 'root@176.118.198.49']
subprocess.run(ssh + ['DOCKER_HOST=unix:///run/docker-isolation.sock /opt/docker-engine/29.8.2/docker/docker exec -i deploy-monitor-worker-1 python -'],
               input=script.encode(), check=True)
subprocess.run(ssh + ['export DOCKER_HOST=unix:///run/docker-isolation.sock; /opt/docker-engine/tools/docker-compose '
                     '-p deploy-monitor --env-file /opt/docker-engine/projects/deploy-monitor/.env '
                     '-f /opt/docker-engine/projects/deploy-monitor/compose.yml up -d --wait --wait-timeout 90'], check=True)
