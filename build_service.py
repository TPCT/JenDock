"""Build this service from its committed tree on the verified VPS, sequentially."""
import io
import subprocess
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
tree = subprocess.check_output(['git', '-C', str(root), 'archive', '--format=tar', sha])
context = io.BytesIO()
with tarfile.open(fileobj=io.BytesIO(tree), mode='r:') as source, tarfile.open(fileobj=context, mode='w:gz', format=tarfile.GNU_FORMAT) as output:
    for item in source:
        if item.isfile() and (item.name in ['Dockerfile', '.dockerignore', 'server.py', 'store.py', 'worker.py', 'control.py', 'terminal_control.py', 'telemetry.py', 'resource_policy.py', 'registry.py', 'job_target.py', 'delivery_target.py', 'integrations.py', 'workspace.py', 'direct_limits.py', 'adoption.py', 'compose_identity.py', 'provenance.py', 'provenance_store.py', 'compose_reconciliation.py', 'compose_review.py', 'container_logs.py', 'ownership.py', 'application_operations.py'] or item.name.startswith('frontend/')):
            output.addfile(item, source.extractfile(item))
for target in ['receiver', 'worker']:
    command = ('D=/opt/docker-engine/29.8.2/docker/docker; export DOCKER_HOST=unix:///run/docker-isolation.sock; '
               '"$D" build --network deploy-build --target ' + target +
               ' --label org.opencontainers.image.revision=' + sha +
               ' -t deploy-monitor-' + target + ':' + sha[:7] + ' -')
    result = subprocess.run(['ssh', '-S', '/tmp/codex-docker-retry-%C', '-o', 'BatchMode=yes',
                             '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes', 'root@176.118.198.49', command],
                            input=context.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    Path('/tmp/deploy-monitor-' + target + '-build.log').write_bytes(result.stdout)
    print(target, sha[:7], 'exit', result.returncode, flush=True)
    if result.returncode:
        raise SystemExit(result.returncode)
