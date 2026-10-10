"""One deployment at a time. Recipes are administrator-owned, never webhook commands.

The receiver cannot reach Docker. The worker rechecks the configured branch and
uses isolated test containers without production environment files or volumes.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from store import connect, projects, effective_projects, valid_branch, set_status, snapshot

DOCKER = os.environ.get('DOCKER_BIN', '/usr/local/bin/docker')
COMPOSE = os.environ.get('COMPOSE_BIN', '/usr/local/bin/docker-compose')
STATE = Path(os.environ.get('STATE_DIR', '/worker-state'))
DATABASE = os.environ.get('DATABASE', '/state/state.sqlite')
CONFIG = os.environ.get('PROJECTS_FILE', '/config/projects.json')
diagnostics = threading.local()


class StepFailure(Exception):
    pass


def run(args, timeout=180, cwd=None, capture=False):
    # Command output can contain application secrets; it never reaches the UI.
    env = {k: v for k, v in os.environ.items() if k in (
        'PATH', 'HOME', 'DOCKER_HOST', 'GIT_SSH_COMMAND', 'SSL_CERT_FILE')}
    env.update(GIT_TERMINAL_PROMPT='0', DOCKER_BUILDKIT='0')
    try:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise StepFailure('انتهت مهلة الخطوة؛ لم يتم اعتبارها ناجحة.') from exc
    if result.returncode:
        job_id = getattr(diagnostics, 'job_id', None)
        if job_id is not None:
            folder = STATE / 'diagnostics'
            folder.mkdir(mode=0o700, exist_ok=True)
            path = folder / (str(int(job_id)) + '.log')
            path.write_bytes(result.stdout[-65536:] + b'\n' + result.stderr[-65536:])
            path.chmod(0o600)
            for old in folder.glob('*.log'):
                if old.stat().st_mtime < time.time() - 7 * 86400:
                    old.unlink()
        reason = 'فشلت خطوة التنفيذ'
        output = (result.stdout[-65536:] + result.stderr[-65536:]).lower()
        for needle, label in [(b'no space left', 'المساحة لا تكفي للبناء'),
                              (b'could not resolve', 'تعذر حل اسم مضيف أثناء البناء'),
                              (b'pull access denied', 'الصورة الأساسية غير متاحة'),
                              (b'failures!', 'فشلت اختبارات المشروع'),
                              (b'assertionerror', 'فشل تحقق داخل اختبارات المشروع'),
                              (b'permission denied', 'صلاحيات خطوة التنفيذ غير كافية')]:
            if needle in output:
                reason = label
                break
        raise StepFailure(reason + ' (exit %d). التشخيص التفصيلي محفوظ محليًا للعامل.' % result.returncode)
    return result.stdout.decode() if capture else None


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as out:
        os.chmod(temp, 0o600)
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compose(recipe, *args):
    command = [COMPOSE, '-p', recipe['compose_project']]
    if recipe.get('env_file'):
        command += ['--env-file', recipe['env_file']]
    for filename in recipe['compose_files']:
        command += ['-f', filename]
    return command + list(args)


def replace_env(path, changes):
    # Preserve existing configuration symlinks such as .env -> ports.env.
    path = Path(path).resolve(strict=True)
    lines = path.read_text().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split('=', 1)[0]
        if key in changes:
            lines[i] = key + '=' + changes[key]
            seen.add(key)
    lines.extend(k + '=' + v for k, v in changes.items() if k not in seen)
    temp = path.with_name(path.name + '.deploy-tmp')
    with temp.open('w') as out:
        os.chmod(temp, path.stat().st_mode & 0o777)
        out.write('\n'.join(lines) + '\n')
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


def env_values(path, keys):
    values = {}
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key in keys:
            values[key] = value.strip().strip('"').strip("'")
    if set(keys) != set(values):
        raise StepFailure('إعداد نسخة التشغيل غير مكتمل؛ لم يتغير الإنتاج.')
    return values


def containers(recipe):
    raw = run(compose(recipe, 'ps', '--all', '--format', 'json', *recipe['services']), capture=True)
    if raw.strip().startswith('['):
        return json.loads(raw)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def verify_runtime(recipe, expected_images=None):
    entries = containers(recipe)
    if {e['Service'] for e in entries} != set(recipe['services']):
        raise StepFailure('خدمة مطلوبة غير موجودة بعد النشر.')
    for entry in entries:
        if entry.get('State') != 'running' or entry.get('Health') == 'unhealthy':
            raise StepFailure('خدمة غير سليمة بعد النشر.')
    if expected_images is not None and {e['Image'] for e in entries} != set(expected_images):
        raise StepFailure('الصور التي تعمل لا تطابق الصور التي تم بناؤها.')
    # An authenticated application flow belongs in project tests; HTTP is a
    # deployment availability gate, not an assertion of business acceptance.
    import urllib.request
    import urllib.error
    for check in recipe['http_checks']:
        try:
            with urllib.request.urlopen(check['url'], timeout=15) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        except OSError as error:
            raise StepFailure('تعذر الوصول إلى الموقع بعد النشر.') from error
        if code not in check['statuses']:
            raise StepFailure('فشل فحص HTTP بعد النشر: %d' % code)


def recover(db, journal):
    record = json.loads(journal.read_text())
    recipe = record['recipe']
    set_status(db, record['job_id'], 'recovering', 'جارٍ استعادة الصور السابقة والتحقق منها.')
    replace_env(recipe['env_file'], record['previous_env'])
    run(compose(recipe, 'up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *recipe['services']), timeout=240)
    verify_runtime(recipe)
    set_status(db, record['job_id'], 'rolled-back', 'فشل النشر؛ عادت الصور السابقة واجتازت فحص التشغيل. لم تتغير قاعدة البيانات.')
    journal.unlink()


def deploy(db, job, project, validate_only=False):
    diagnostics.job_id = job['id']
    recipe = project['recipe']
    name, sha = job['project'], job['sha']
    branch = job['branch'] or project['branch']
    if not valid_branch(branch):
        raise StepFailure('اسم الفرع غير صالح.')
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise StepFailure('معرّف Git غير صالح.')
    if not recipe.get('tests') or not recipe.get('http_checks'):
        raise StepFailure('اختبارات النشر لم تُعتمد لهذا المشروع بعد.')
    journal = STATE / 'transaction.json'
    with tempfile.TemporaryDirectory(prefix=name + '-', dir='/work') as temp:
        source = Path(temp) / 'source'
        set_status(db, job['id'], 'building', 'جارٍ التحقق من Git وتحضير بناء معزول.')
        remote = 'git@github.com:' + project['repository'] + '.git'
        run(['git', 'clone', '--branch', branch, '--', remote, str(source)], timeout=300)
        head = run(['git', 'rev-parse', 'HEAD'], cwd=source, capture=True).strip()
        if head != sha:
            set_status(db, job['id'], 'superseded', 'الفرع يحتوي إصدارًا أحدث؛ لن ننشر الإصدار القديم.')
            return
        # Disallow automatic schema changes until a project-specific migration
        # and backwards compatibility policy is explicitly configured.
        previous_images = sorted({e['Image'] for e in containers(recipe)})
        revisions = set()
        for previous in previous_images:
            value = run([DOCKER, 'image', 'inspect', '--format', '{{index .Config.Labels "org.opencontainers.image.revision"}}', previous], capture=True).strip()
            if not re.fullmatch('[0-9a-f]{7,40}', value):
                value = previous.rsplit(':', 1)[-1]
            if not re.fullmatch('[0-9a-f]{7,40}', value):
                raise StepFailure('لا يمكن تحديد معرّف Git للصورة الحالية؛ تحتاج مراجعة قبل النشر.')
            revisions.add(value)
        changed = '\n'.join(run(['git', 'diff', '--name-only', old, sha], cwd=source, capture=True) for old in revisions)
        schema_changes = any(any(part in '/' + line for part in ('/migrations/', '/alembic/versions/')) for line in changed.splitlines())
        if schema_changes and not validate_only:
            set_status(db, job['id'], 'needs-review', 'تغييرات قاعدة البيانات تحتاج خطة ترحيل قبل النشر التلقائي.')
            return
        tracked = run(['git', 'ls-files'], cwd=source, capture=True).splitlines()
        if any(Path(f).name in ('.env', 'id_rsa', 'id_ed25519') for f in tracked):
            raise StepFailure('يوجد ملف إعدادات حساس ضمن Git؛ أوقف البناء قبل نسخ السياق.')
        images = {}
        updates = {}
        version = sha[:12] + '-' + str(job['id'])
        for index, spec in enumerate(recipe['images']):
            # Never overwrite a running tag during same-commit validation/retry.
            tag = spec['name'] + ':' + version
            set_status(db, job['id'], 'building', 'جارٍ بناء صورة ' + spec['name'] + ' من Git.')
            args = [DOCKER, 'build', '--force-rm', '--memory', '1536m', '--cpu-period', '100000',
                    '--cpu-quota', '200000', '--network', 'deploy-build',
                    '--label', 'net.shiftcodes.deployer=deploy-monitor',
                    '--label', 'net.shiftcodes.job=' + str(job['id']),
                    '--label', 'org.opencontainers.image.revision=' + sha,
                    '-t', tag, '-f', str(source / spec['dockerfile'])]
            if index == 0:
                args += ['--no-cache']
            for key, value in spec.get('build_args', {}).items():
                args += ['--build-arg', key + '=' + value]
            if spec.get('target'):
                args += ['--target', spec['target']]
            args += [str(source / spec.get('context', '.'))]
            run(args, timeout=1800)
            images[spec['name']] = tag
            if spec.get('deploy', True):
                updates[spec['env_key']] = tag if spec.get('env_mode', 'image') == 'image' else version
        set_status(db, job['id'], 'testing', 'جارٍ اختبار الصور دون أسرار أو قواعد بيانات الإنتاج.')
        for i, test in enumerate(recipe['tests']):
            test_name = 'deploy-test-%s-%d' % (job['id'], i)
            args = [DOCKER, 'run', '--rm', '--name', test_name, '--network', 'none', '--read-only',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=512m', '--cpus=1',
                    '--pids-limit=128', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m']
            for mount in test.get('tmpfs', []):
                args += ['--tmpfs', mount]
            for key, value in test.get('environment', {}).items():
                args += ['-e', key + '=' + value]
            args += ['--entrypoint', test['command'][0], images[test['image']], *test['command'][1:]]
            try:
                run(args, timeout=180)
            finally:
                subprocess.run([DOCKER, 'rm', '-f', test_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if validate_only:
            set_status(db, job['id'], 'validated', 'نجح البناء والاختبار الداخلي؛ لم تتغير خدمات الإنتاج.' + (' توجد تغييرات ترحيل تحتاج مراجعة.' if schema_changes else ''))
            return
        previous_env = env_values(recipe['env_file'], updates)
        current_head = run(['git', 'ls-remote', '--refs', 'origin', 'refs/heads/' + branch], cwd=source, capture=True).split()
        if not current_head or current_head[0] != sha:
            set_status(db, job['id'], 'superseded', 'وصل تعديل أحدث أثناء البناء؛ لم تتغير خدمات الإنتاج.')
            return
        # Persist recovery instructions before the first production mutation.
        atomic_json(journal, {'job_id': job['id'], 'recipe': recipe, 'previous_env': previous_env})
        set_status(db, job['id'], 'deploying', 'اكتمل البناء والاختبار؛ جارٍ تحديث الخدمات المحددة.')
        try:
            replace_env(recipe['env_file'], updates)
            run(compose(recipe, 'up', '-d', '--no-deps', '--wait', '--wait-timeout', '180', *recipe['services']), timeout=240)
            verify_runtime(recipe, [images[s['name']] for s in recipe['images'] if s.get('deploy', True)])
        except Exception:
            recover(db, journal)
            return
        set_status(db, job['id'], 'succeeded', 'اكتمل النشر وفحص التشغيل. لم تُنفّذ ترحيلات قاعدة بيانات تلقائية.')
        journal.unlink()
        # No force, no volume pruning. Docker refuses images still referenced.
        for old in previous_images:
            if old not in images.values():
                subprocess.run([DOCKER, 'image', 'rm', old], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


def observe(db):
    raw = run([DOCKER, 'ps', '-a', '--filter', 'label=com.docker.compose.project', '--format', '{{json .}}'], capture=True)
    data = []
    for line in raw.splitlines():
        obj = json.loads(line)
        if obj.get('Names', '').startswith(('tanialab-', 'deploy-monitor-recovery-check-', 'deploy-test-')):
            continue
        data.append({k: obj.get(k) for k in ('Names', 'Image', 'State', 'Status')})
    stats_raw = run([DOCKER, 'stats', '--no-stream', '--format', '{{json .}}'], timeout=30, capture=True)
    stats = {row['Name']: row for row in (json.loads(line) for line in stats_raw.splitlines() if line.strip())}
    for entry in data:
        reading = stats.get(entry['Names'], {})
        entry.update({key: reading.get(key) for key in ('CPUPerc','MemUsage','MemPerc','PIDs')})
    snapshot(db, 'containers', data)
    disk = shutil.disk_usage('/work')
    snapshot(db, 'disk', {'total': disk.total, 'free': disk.free})
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
            memory[key] = int(value.strip().split()[0]) * 1024
    snapshot(db, 'resources', {'memory': memory, 'load_1m': os.getloadavg()[0], 'cpu_count': os.cpu_count()})
    snapshot(db, 'worker', {'state': 'running', 'external_monitor': False, 'outbound_alerts': False})


def observe_branches(db, name, project):
    try:
        raw=run(['git','ls-remote','--heads','git@github.com:'+project['repository']+'.git'],timeout=45,capture=True)
        branches={}
        for line in raw.splitlines():
            sha,ref=line.split('\t',1)
            branch=ref.removeprefix('refs/heads/')
            if re.fullmatch('[0-9a-f]{40}',sha) and valid_branch(branch):
                branches[branch]=sha
        snapshot(db,'branches:'+name,{'branches':branches,'error':False})
    except (StepFailure,ValueError):
        snapshot(db,'branches:'+name,{'branches':{},'error':True,'detail':'تعذر جلب فروع GitHub. أعد المحاولة.'})
    db.execute('DELETE FROM requests WHERE project=?',(name,))


def main():
    STATE.mkdir(exist_ok=True)
    Path('/work').mkdir(exist_ok=True)
    with (STATE / 'worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = connect(DATABASE)
        journal = STATE / 'transaction.json'
        if journal.exists():
            try:
                recover(db, journal)
            except Exception:
                snapshot(db, 'worker', {'state': 'recovery-failed', 'detail': 'تحتاج استعادة التشغيل مراجعة؛ توقفت عمليات النشر.'})
                raise SystemExit(1)
        db.execute("UPDATE jobs SET status='interrupted',detail='توقف العامل قبل بدء التحديث؛ لم تعتبر المهمة ناجحة.' WHERE status IN ('building','testing','deploying')")
        def branch_collector():
            branch_db=connect(DATABASE)
            while True:
                try:
                    policy=projects(CONFIG)
                    requested=branch_db.execute('SELECT project FROM requests ORDER BY created LIMIT 1').fetchone()
                    name=requested['project'] if requested else next((name for name in policy if not (row:=branch_db.execute('SELECT updated FROM snapshots WHERE name=?',('branches:'+name,)).fetchone()) or time.time()-row['updated']>300),None)
                    if name in policy:
                        observe_branches(branch_db,name,policy[name])
                except (OSError,ValueError,StepFailure):
                    pass
                time.sleep(3)
        threading.Thread(target=branch_collector,daemon=True).start()
        def collector():
            observations = connect(DATABASE)
            while True:
                try:
                    observe(observations)
                except (StepFailure, OSError):
                    snapshot(observations, 'worker', {'state': 'unavailable', 'detail': 'تعذر قراءة Docker؛ البيانات السابقة قديمة.'})
                time.sleep(15)
        threading.Thread(target=collector, daemon=True).start()
        while True:
            try:
                job = db.execute("SELECT * FROM jobs WHERE status IN ('queued','validation-queued') ORDER BY id LIMIT 1").fetchone()
                if job:
                    project = effective_projects(db, CONFIG).get(job['project'])
                    validation = job['status'] == 'validation-queued'
                    claimed=db.execute("UPDATE jobs SET status='building',updated=? WHERE id=? AND status=?",(time.time(),job['id'],job['status'])).rowcount
                    if not claimed:
                        continue
                    if not project or (not project.get('enabled') and not validation):
                        set_status(db, job['id'], 'awaiting-validation', 'النشر التلقائي غير مفعّل لهذا المشروع.')
                    else:
                        try:
                            deploy(db, job, project, validate_only=validation)
                        except Exception as error:
                            if journal.exists():
                                snapshot(db, 'worker', {'state': 'recovery-failed'})
                                raise SystemExit(1)
                            phase = db.execute('SELECT detail FROM jobs WHERE id=?', (job['id'],)).fetchone()['detail']
                            detail = str(error) if isinstance(error, StepFailure) else 'فشل داخلي؛ لم يُعتبر النشر ناجحًا.'
                            set_status(db, job['id'], 'failed', phase + ' — ' + detail)
                        finally:
                            # Only this job's unreferenced images; never force
                            # delete a running image and never touch volumes.
                            subprocess.run([DOCKER, 'image', 'prune', '-af', '--filter', 'label=net.shiftcodes.job=' + str(job['id'])],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                            subprocess.run([DOCKER, 'builder', 'prune', '-af'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            except (StepFailure, OSError):
                snapshot(db, 'worker', {'state': 'unavailable', 'detail': 'تعذر قراءة Docker؛ البيانات السابقة قديمة.'})
            time.sleep(15)


if __name__ == '__main__':
    main()
