import os
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import worker


class WorkerWorkloadVerificationTests(unittest.TestCase):
    def test_cleanup_fails_closed_and_idle_recovery_does_not_restart_work(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(worker, 'STATE', Path(directory)), \
             patch.object(worker, 'workload_verifier') as factory, \
             patch.object(worker, 'snapshot') as snapshot:
            self.assertFalse(worker.workload_cleanup_pending())
            worker.recover_workload_verifications(object())
            factory.assert_not_called()
            (Path(directory) / 'workload-verification').mkdir()
            factory.side_effect = ValueError('private detail')
            self.assertTrue(worker.workload_cleanup_pending())
            worker.recover_workload_verifications(object())
            self.assertNotIn('private detail', str(snapshot.call_args))
            factory.side_effect = None
            factory.return_value.has_pending_cleanup.return_value = False
            self.assertFalse(worker.workload_cleanup_pending())
            db = object()
            worker.recover_workload_verifications(db)
            factory.return_value.recover_pending.assert_called_once_with(db)
            factory.return_value.verify.assert_not_called()

    def test_worker_configuration_is_lazy_and_shared_by_publication_paths(self):
        db = object()
        implementation = Mock()
        instance = implementation.return_value
        instance.verify.return_value = {'verified': True}
        module = types.SimpleNamespace(WorkloadArtifactVerifier=implementation)
        with patch.dict(sys.modules, workload_artifact_verifier=module), \
             patch.dict(os.environ, {'WORKLOAD_POSTGRES_IMAGE': 'sha256:'+'a'*64,
                                     'DOCKER_HOST': 'unix:///fixture/docker.sock'}), \
             patch.object(worker, 'DOCKER', '/fixture/docker'), \
             patch.object(worker, 'STATE', Path('/fixture/state')), \
             patch('artifact_publication.record') as record, \
             patch('artifact_publication.process', return_value=True) as process:
            worker.complete_build(db, 7, {}, {}, 'validated', 'Passed')
            worker.publish_service_artifacts(db, 8, {}, {})
            worker.process_artifact_publications(db)
            self.assertEqual(record.call_count, 2)
            self.assertEqual([c.args[2] for c in process.call_args_list], [7, 8, None])
            implementation.assert_not_called()
            document = {'image': 'sha256:'+'b'*64}
            for call in process.call_args_list:
                self.assertEqual(call.kwargs['verify_role'](document), {'verified': True})
            self.assertEqual(implementation.call_count, 3)
            implementation.assert_called_with(docker='/fixture/docker', socket='unix:///fixture/docker.sock',
                postgres_image='sha256:'+'a'*64, state_dir=Path('/fixture/state/workload-verification'))
            instance.verify.assert_called_with(db, document)


if __name__ == '__main__':
    unittest.main()
