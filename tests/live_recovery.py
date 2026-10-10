"""Disposable service in this product's Compose group; no business data/mounts.

Run inside the deployed worker. Verifies real Docker cutover failure and restart
recovery. Does not call providers, migrate databases or touch other services.
"""
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

from store import connect, enqueue
from worker import DOCKER, atomic_json, compose, env_values, recover, replace_env, run, verify_runtime

root = Path('/opt/docker-engine/projects/deploy-monitor')
config = root / 'validation.json'
env = root / 'validation.env'
database = Path('/worker-state/validation.sqlite')
journal = Path('/worker-state/validation-transaction.json')
recipe = {'compose_project': 'deploy-monitor', 'env_file': str(env),
          'compose_files': [str(config)], 'services': ['recovery-check'], 'http_checks': []}
try:
    for tag, command in [('good', ['python', '-m', 'http.server', '8080']),
                         ('bad', ['python', '-c', 'raise SystemExit(9)'])]:
        dockerfile = ('FROM python:3.13-alpine\nUSER 65534:65534\nWORKDIR /tmp\nCMD ' + json.dumps(command) + '\n').encode()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.GNU_FORMAT) as archive:
            info = tarfile.TarInfo('Dockerfile'); info.size = len(dockerfile)
            archive.addfile(info, io.BytesIO(dockerfile))
        subprocess.run([DOCKER, 'build', '--network', 'none', '-t', 'deploy-monitor-fixture:' + tag, '-'],
                       input=stream.getvalue(), stdout=subprocess.DEVNULL, check=True, timeout=120)
    config.write_text(json.dumps({'services': {'recovery-check': {
        'image': '${CHECK_IMAGE}', 'network_mode': 'none', 'read_only': True,
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'mem_limit': '64m', 'pids_limit': 32,
        'healthcheck': {'test': ['CMD', 'python', '-c', "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080',timeout=2)"],
                        'interval': '2s', 'timeout': '3s', 'retries': 3}}}}))
    env.write_text('CHECK_IMAGE=deploy-monitor-fixture:good\n'); env.chmod(0o600)
    run(compose(recipe, 'up', '-d', '--no-deps', '--wait', '--wait-timeout', '30', 'recovery-check'), timeout=60)
    verify_runtime(recipe, ['deploy-monitor-fixture:good'])
    db = connect(database)
    job, _ = enqueue(db, 'offline-recovery-test', 'recovery-check', 'a' * 40, True)
    atomic_json(journal, {'job_id': job, 'recipe': recipe, 'previous_env': {'CHECK_IMAGE': 'deploy-monitor-fixture:good'}})
    replace_env(env, {'CHECK_IMAGE': 'deploy-monitor-fixture:bad'})
    result = subprocess.run(compose(recipe, 'up', '-d', '--no-deps', '--wait', '--wait-timeout', '10', 'recovery-check'),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
    assert result.returncode != 0, 'Bad image unexpectedly passed'
    db.close()
    # Open a new connection, as a restarted worker does with its persisted journal.
    db = connect(database)
    recover(db, journal)
    verify_runtime(recipe, ['deploy-monitor-fixture:good'])
    assert env_values(env, ['CHECK_IMAGE'])['CHECK_IMAGE'] == 'deploy-monitor-fixture:good'
    assert db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()['status'] == 'rolled-back'
    assert not journal.exists()
    db.close()
    print(json.dumps({'real_docker_initial_health': True, 'broken_image_rejected': True,
                      'persisted_journal_reopened': True, 'previous_image_restored_healthy': True,
                      'production_applications_touched': False}))
finally:
    if config.exists():
        subprocess.run(compose(recipe, 'rm', '-sf', 'recovery-check'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    for tag in ['good', 'bad']:
        subprocess.run([DOCKER, 'image', 'rm', 'deploy-monitor-fixture:' + tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    for path in [config, env, journal, database, Path(str(database)+'-wal'), Path(str(database)+'-shm')]:
        path.unlink(missing_ok=True)
