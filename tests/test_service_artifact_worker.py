import json
import unittest
from unittest.mock import patch

import worker


class ArtifactWorkerTests(unittest.TestCase):
    def test_publisher_resolves_built_tag_to_immutable_identity(self):
        digest = 'sha256:' + 'a' * 64
        with patch.object(worker, 'run', return_value=json.dumps([{'Id': digest}])) as run:
            self.assertEqual(worker.inspect_artifact_image('web:commit-4'), digest)
            run.assert_called_once_with([worker.DOCKER, 'image', 'inspect', 'web:commit-4'], capture=True)

    def test_cleanup_protects_service_sources_and_never_force_deletes(self):
        protected, unused, rollback = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64
        with patch('artifact_publication.pending', return_value=False), patch('managed_services.protected_images', return_value=[protected]), \
             patch('managed_delivery.protected_images', return_value={rollback}), \
             patch.object(worker, 'run', return_value=protected+'\n'+unused+'\n'+rollback+'\nnot-an-id\n'), \
             patch.object(worker.subprocess, 'run', return_value=worker.subprocess.CompletedProcess([],0)) as remove:
            worker.prune_job_images('db', 4)
            remove.assert_called_once_with([worker.DOCKER, 'image', 'rm', unused],
                stdout=worker.subprocess.DEVNULL, stderr=worker.subprocess.DEVNULL, timeout=10)

class ArtifactRetentionTests(unittest.TestCase):
    def test_enqueue_preserves_old_artifact_source_evidence_only(self):
        import tempfile
        from pathlib import Path
        from store import connect, enqueue
        import managed_services
        with tempfile.TemporaryDirectory() as temporary:
            db = connect(Path(temporary) / 'db')
            try:
                managed_services.initialize(db)
                for identifier in (1, 2):
                    db.execute("INSERT INTO jobs(id,delivery,project,sha,status,updated,created) VALUES(?,?,?,?,?,?,?)",
                               (identifier, str(identifier), 'app', 'a'*40, 'succeeded', 1, 1))
                db.execute('INSERT INTO service_artifacts VALUES(?,?,?,?,?,?)', ('artifact', 'app', 1, '{}', 'digest', 1))
                enqueue(db, 'new-request', 'app', 'b'*40, True, 'main')
                self.assertIsNotNone(db.execute('SELECT id FROM jobs WHERE id=1').fetchone())
                self.assertIsNone(db.execute('SELECT id FROM jobs WHERE id=2').fetchone())
            finally:
                db.close()
