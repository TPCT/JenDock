import copy
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import build_profiles
import managed_services
import workload_artifact_verifier as verifier
import workload_package_verifier as package

ROOT = Path(__file__).resolve().parents[1] / 'workload-instrumentation'
IMAGE = 'sha256:' + 'a' * 64
PG = 'sha256:' + 'b' * 64


def tar(entries):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode='w', format=tarfile.USTAR_FORMAT) as stream:
        for name, body in entries:
            info = tarfile.TarInfo(name); info.size = len(body)
            stream.addfile(info, io.BytesIO(body))
    return target.getvalue()


def document(role='queue'):
    config = {'connection': 'database', 'queues': ['default'], 'timeout_seconds': 60,
              'retry_after_seconds': 90, 'shutdown_grace_seconds': 90, 'max_attempts': 3, 'sleep_seconds': 1}
    template = 'laravel-database-queue-v1'
    if role == 'scheduler':
        config = {'connection': 'pgsql', 'timezone': 'UTC', 'scope': 'default'}
        template = 'laravel-scheduler-v1'
    profile = build_profiles.role_profile(template, {'framework_version': package.FRAMEWORK_VERSION,
        'instrumentation': {'version': package.PACKAGE_VERSION, 'digest': package.PACKAGE_DIGEST},
        'configuration': config}, 'app', '65534:65534')
    profile['template_revision'] = profile.pop('revision')
    return dict(profile, template_id=template, source_job=1, project='fixture',
                source_environment='development', image=IMAGE, source_sha='c' * 40,
                target_fingerprint='d' * 64, recipe_revision=1)


class Docker:
    """Stateful fake daemon: full verifier uses normal commands and real archives."""
    def __init__(self):
        self.rows = {}; self.calls = []; self.count = 0; self.mutations = {}
        self.hidden = False; self.lost_create = False; self.scope = None; self.secret = None
        self.schemas = 0; self.postgres_images = {PG}

    def __call__(self, argv, payload=b'', *, timeout, env, limit):
        args = argv[1:]; self.calls.append((args, payload, dict(env), timeout, limit))
        assert timeout > 0 and timeout <= 180
        op = args[0]
        if op == 'image':
            image = args[2]
            return json.dumps([{'Id': image, 'Config': {'WorkingDir': '/app', 'Volumes': None,
                'Entrypoint': ['docker-entrypoint.sh'] if image in self.postgres_images else None,
                'Cmd': ['postgres'] if image in self.postgres_images else None}}]).encode()
        if op == 'ps':
            f = args[-1]
            if f.startswith('name='):
                name = f[len('name=^/'): -1]
                return b'' if self.hidden else '\n'.join(i for i, r in self.rows.items() if r['Name'] == '/' + name).encode()
            return '\n'.join(i for i in self.rows if i == f[3:]).encode()
        if op == 'create':
            self.count += 1
            identity = '%064x' % self.count
            image_index = next(i for i, s in enumerate(args) if s == IMAGE or s in self.postgres_images)
            image = args[image_index]
            def value(flag, default=None):
                return args[args.index(flag) + 1] if flag in args[:image_index] else default
            def values(flag):
                return [args[i + 1] for i in range(image_index) if args[i] == flag]
            labels = dict(v.split('=', 1) for v in values('--label'))
            tmpfs = dict(v.split(':', 1) for v in values('--tmpfs'))
            environ = [v if '=' in v else v + '=' + env[v] for v in values('-e')]
            entrypoint = [value('--entrypoint')] if value('--entrypoint') else ['docker-entrypoint.sh']
            command = args[image_index + 1:] or ['postgres']
            row = {'Id': identity, 'Name': '/' + value('--name'), 'Image': image,
                   'Config': {'Labels': labels, 'User': value('--user'), 'Entrypoint': entrypoint,
                              'Cmd': command, 'Env': environ, 'Healthcheck': {'Test': ['NONE']}},
                   'HostConfig': {'NetworkMode': value('--network'), 'ReadonlyRootfs': '--read-only' in args,
                                  'CapDrop': values('--cap-drop'), 'SecurityOpt': values('--security-opt'),
                                  'Memory': int(value('--memory')), 'NanoCpus': int(float(value('--cpus')) * 1e9),
                                  'PidsLimit': int(value('--pids-limit')), 'LogConfig': {'Type': value('--log-driver')},
                                  'Tmpfs': tmpfs}, 'Mounts': [], 'NetworkSettings': {'Ports': {}, 'Networks': {'none': {'IPAddress': '', 'GlobalIPv6Address': ''}} if value('--network') == 'none' else {}},
                   'State': {'Running': False, 'Status': 'created', 'ExitCode': 0, 'OOMKilled': False}}
            self.rows[identity] = row
            if labels[verifier.LABEL + '.role'] == 'database':
                self.nonce = labels[verifier.LABEL]; self.secret = env['POSTGRES_PASSWORD']
            if self.mutations.get('unsafe') and labels[verifier.LABEL + '.role'] == 'inspection':
                row['HostConfig']['NetworkMode'] = 'host'
            if self.mutations.get('extra_network') and labels[verifier.LABEL + '.role'] == 'inspection':
                row['NetworkSettings']['Networks']['foreign'] = {'IPAddress': '172.18.0.2'}
            if self.mutations.get('routed_ip') and labels[verifier.LABEL + '.role'] == 'inspection':
                row['NetworkSettings']['IPAddress'] = '172.18.0.2'
            if self.lost_create:
                self.lost_create = False
                self.hidden = self.mutations.get('hidden_create', False)
                raise RuntimeError('PRIVATE-CANDIDATE-SECRET lost reply')
            return (identity + '\n').encode()
        if op == 'inspect':
            return json.dumps([self.rows[args[1]]]).encode()
        if op == 'cp':
            path = args[1].split(':', 1)[1]
            if path.endswith('installed.json'):
                version = self.mutations.get('framework', package.FRAMEWORK_VERSION)
                return tar([('installed.json', json.dumps({'packages': [{'name': 'laravel/framework', 'version': 'v' + version}]}).encode())])
            component = path.split('/deploy-monitor-workloads/', 1)[1]
            entries = [(p, (ROOT / p).read_bytes()) for p in package.TRUSTED_MANIFEST if p == component or p.startswith(component + '/')]
            if self.mutations.get('package') and component == 'src':
                entries[0] = (entries[0][0], b'fake package but same echoed digest')
            return tar(entries)
        if op == 'start':
            row = self.rows[args[-1]]
            if row['Image'] in self.postgres_images:
                row['State'].update(Running=True, Status='running')
                return b''
            if self.mutations.get('timeout'):
                row['State'].update(Running=True, Status='running')
                raise TimeoutError('PRIVATE-CANDIDATE-SECRET')
            request = json.loads(payload); self.scope = request['scope']
            row['State'].update(Status='exited')
            workload = json.loads(dict(v.split('=', 1) for v in row['Config']['Env'])['DEPLOY_MONITOR_WORKLOAD'])
            result = {'protocol': 1, 'status': 'ok', 'observed_at': '2026-10-11T12:00:00Z', 'scope': self.scope}
            if request['operation'] == 'capabilities':
                role = workload['role']
                result['result'] = {'package_version': '1.0.0', 'package_digest': self.mutations.get('echo', package.PACKAGE_DIGEST),
                    'framework_version': package.FRAMEWORK_VERSION, 'adapter_revision': 1, 'role': role,
                    'operations': ['queue.observe', 'queue.failures', 'queue.retry', 'queue.receipt'] if role == 'queue' else ['scheduler.observe', 'scheduler.history'],
                    'retry_policy': 'registered_plain_jobs_without_retry_until'}
            elif workload['role'] == 'queue':
                result['result'] = {'driver': 'database', 'connection_id': workload['configuration']['connection'], 'workers': [],
                    'queues': [{'name': q, 'ready': 0, 'delayed': 0, 'reserved': 0} for q in workload['configuration']['queues']]}
            else:
                result.pop('scope'); result.update(status='unsupported', error={'code': 'registry_not_observed'})
            if self.mutations.get('overflow'):
                return b'x' * (limit + 1)
            return json.dumps(result).encode()
        if op == 'exec':
            if 'pg_dump' in args:
                self.schemas += 1
                return ('\\restrict token%d\nTRUSTEDSCHEMA%s\n\\unrestrict token%d\n' % (self.schemas,
                    'changed' if self.mutations.get('schema') and self.schemas > 1 else '', self.schemas)).encode()
            text = payload.decode()
            if text == 'SELECT 1;':
                return b'1\n'
            if text.startswith('SELECT value'):
                return (self.nonce + '\n' + '0\n' * len(verifier.TABLES)).encode()
            return b''
        if op == 'rm':
            del self.rows[args[-1]]
            return b''
        raise AssertionError(args)


class ArtifactVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workload-verifier-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.root / 'docker.sock'))
        os.chmod(self.root / 'docker.sock', 0o600)
        self.addCleanup(self.sock.close)
        self.docker = Docker()
        self.v = verifier.WorkloadArtifactVerifier(docker=str(Path('/usr/bin/true').resolve()), socket='unix://' + str(self.root / 'docker.sock'), postgres_image=PG, state_dir=self.root / 'journal', runner=self.docker)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)

    def assert_clean(self):
        self.assertEqual({}, self.docker.rows)
        self.assertFalse(self.v.has_pending_cleanup())
        self.assertEqual([], self.v.protected_images())

    def test_full_queue_and_scheduler_sequence_cleanup_before_receipt_and_replay(self):
        for role in ('queue', 'scheduler'):
            with self.subTest(role=role):
                doc = document(role)
                result = self.v.verify(self.db, doc)
                self.assertEqual(managed_services._role_evidence(doc), result)
                self.assert_clean()
                count = self.docker.count
                self.assertEqual(result, self.v.verify(self.db, doc))
                self.assertEqual(count, self.docker.count)
        self.assertEqual(8, self.docker.count)
        for args, payload, env, _, _ in self.docker.calls:
            if args[0] == 'create':
                self.assertIn('--read-only', args)
                self.assertNotIn('-v', args); self.assertNotIn('-p', args)
                self.assertNotIn(self.docker.secret, args)
        journal = '\n'.join(p.read_text() for p in (self.root / 'journal').glob('*.json'))
        self.assertNotIn(self.docker.secret, journal)
        self.assertNotIn('DB_PASSWORD', journal)

    def test_postgres_dependency_pin_changes_journal_identity_and_requires_new_verification(self):
        doc = document()
        first = self.v.verify(self.db, doc)
        old_count = self.docker.count
        next_pin = 'sha256:' + 'e' * 64
        self.docker.postgres_images.add(next_pin)
        other = verifier.WorkloadArtifactVerifier(docker=str(Path('/usr/bin/true').resolve()),
            socket='unix://' + str(self.root / 'docker.sock'), postgres_image=next_pin,
            state_dir=self.root / 'journal', runner=self.docker)
        self.assertEqual(first, other.verify(self.db, doc))
        self.assertEqual(old_count + 4, self.docker.count)
        journals = [json.loads(p.read_text()) for p in (self.root / 'journal').glob('*.json')]
        self.assertEqual({PG, next_pin}, {j['identity']['postgres_image'] for j in journals})
        self.assertEqual(2, len({j['key'] for j in journals}))
        self.assert_clean()

    def test_tampered_package_false_echo_framework_and_schema_are_terminal_clean_failures(self):
        for mutation in ('package', 'echo', 'framework', 'schema', 'unsafe', 'extra_network', 'routed_ip', 'timeout', 'overflow'):
            with self.subTest(mutation=mutation):
                self.docker.mutations = {mutation: {'echo': 'e' * 64, 'framework': '12.1.0'}.get(mutation, True)}
                self.docker.schemas = 0
                doc = document(); doc['source_job'] += len(self.docker.calls)
                with self.assertRaises(verifier.VerificationError) as caught:
                    self.v.verify(self.db, doc)
                self.assertNotIn('PRIVATE-CANDIDATE-SECRET', str(caught.exception))
                self.assert_clean()
                calls = len(self.docker.calls)
                with self.assertRaises(verifier.VerificationError):
                    self.v.verify(self.db, doc)
                self.assertEqual(calls, len(self.docker.calls))

    def test_lost_create_with_observable_exact_container_is_removed_and_not_retried(self):
        self.docker.lost_create = True
        with self.assertRaises(verifier.VerificationError):
            self.v.verify(self.db, document())
        self.assert_clean()
        self.assertEqual(1, self.docker.count)

    def test_unknown_create_absence_fences_until_owned_late_object_observed(self):
        self.docker.lost_create = True; self.docker.mutations['hidden_create'] = True
        with self.assertRaises(verifier.VerificationError) as caught:
            self.v.verify(self.db, document())
        self.assertEqual('create_outcome_unknown', caught.exception.code)
        self.assertTrue(self.v.has_pending_cleanup())
        self.assertEqual([IMAGE], self.v.protected_images())
        doc = document(); doc['source_job'] = 2
        with self.assertRaises(verifier.VerificationError):
            self.v.verify(self.db, doc)
        self.assertEqual(1, self.docker.count)
        self.docker.hidden = False
        self.v.recover_pending(self.db)
        self.assert_clean()
        self.assertEqual(1, self.docker.count)

    def test_recovery_never_deletes_replaced_or_foreign_container(self):
        self.docker.lost_create = True; self.docker.mutations['hidden_create'] = True
        with self.assertRaises(verifier.VerificationError):
            self.v.verify(self.db, document())
        self.docker.hidden = False
        row = next(iter(self.docker.rows.values()))
        row['Config']['Labels'][verifier.LABEL] = 'foreign'
        with self.assertRaises(verifier.VerificationError):
            self.v.recover_pending(self.db)
        self.assertEqual(1, len(self.docker.rows))
        self.assertFalse(any(c[0][0] == 'rm' for c in self.docker.calls))

    def test_open_sqlite_transaction_invalid_profile_and_untrusted_pin_have_no_effects(self):
        self.db.execute('CREATE TABLE x(id)'); self.db.execute('INSERT INTO x VALUES(1)')
        with self.assertRaises(verifier.VerificationError):
            self.v.verify(self.db, document())
        self.db.rollback()
        for key, value in [('command', ['sh']), ('user', '0:0'), ('source_environment', 'unknown')]:
            doc = document(); doc[key] = value
            with self.assertRaises(verifier.VerificationError):
                self.v.verify(self.db, doc)
        self.assertEqual([], self.docker.calls)

    def test_private_directory_creation_existing_permission_and_symlink_rejected(self):
        self.assertEqual(0o700, (self.root / 'journal').stat().st_mode & 0o777)
        # A private existing state directory directly under root-owned sticky /tmp
        # is valid; /tmp itself is never accepted as the private journal root.
        verifier.WorkloadArtifactVerifier(docker=str(Path('/usr/bin/true').resolve()),
            socket='unix://' + str(self.root / 'docker.sock'), postgres_image=PG, state_dir=self.root)
        os.chmod(self.root / 'journal', 0o755)
        with self.assertRaises(verifier.VerificationError):
            verifier.WorkloadArtifactVerifier(docker=str(Path('/usr/bin/true').resolve()), socket='unix://' + str(self.root / 'docker.sock'), postgres_image=PG, state_dir=self.root / 'journal')
        os.chmod(self.root / 'journal', 0o700)
        (self.root / 'alias').symlink_to(self.root / 'journal')
        with self.assertRaises(verifier.VerificationError):
            verifier.WorkloadArtifactVerifier(docker=str(Path('/usr/bin/true').resolve()), socket='unix://' + str(self.root / 'docker.sock'), postgres_image=PG, state_dir=self.root / 'alias')

    def test_single_file_metadata_archive_rejects_links_extra_data_duplicate_json(self):
        good = tar([('installed.json', b'{"packages":[]}')])
        self.assertEqual({'packages': []}, verifier.single_json_archive(good))
        for raw in (tar([('installed.json', b'{"packages":[],"packages":[]}')]),
                    tar([('installed.json', b'{}'), ('extra', b'')]),
                    tar([('../installed.json', b'{}')])):
            with self.assertRaises(verifier.VerificationError):
                verifier.single_json_archive(raw)


if __name__ == '__main__':
    unittest.main()
