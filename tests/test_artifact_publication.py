import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import artifact_publication as publication
import job_target
import registry
import worker
from store import connect, enqueue


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'db')
        profile = {'revision': 1, 'image_name': 'app', 'command': ['app'], 'healthcheck': ['health'], 'user': '65534:65534'}
        self.recipe = {'compose_project': 'app', 'compose_files': ['/managed/app.yml'], 'services': ['web'], 'images': [{'name': 'app'}], 'service_profiles': {'first': profile, 'second': copy.deepcopy(profile)}}
        registry.import_legacy(self.db, {'app': {'repository': 'TPCT/app', 'repository_id': 12, 'branch': 'main', 'recipe': self.recipe}}, [])
        target = job_target.capture('app', 'legacy-unclassified', 'TPCT/app', 12, 'main', 'b'*40, 'validate', 1, self.recipe, project_revision=1)
        self.job, _ = enqueue(self.db, 'publication-fixture', 'app', 'b'*40, True, 'main', 'validate', target_factory=lambda db: target)
        self.images = {'app': 'app:fixture'}

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_second_insert_failure_is_atomic_and_retry_preserves_source(self):
        publication.initialize(self.db)
        self.db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON service_artifacts
            WHEN json_extract(NEW.document,'$.template_id')='second'
            BEGIN SELECT RAISE(ABORT,'fixture'); END""")
        with patch.object(worker, 'run', return_value=json.dumps([{'Id': 'sha256:'+'a'*64}])):
            worker.complete_build(self.db, self.job, self.recipe, self.images, 'succeeded', 'Deployment passed.')
        self.assertEqual(self.db.execute('SELECT status FROM jobs WHERE id=?', (self.job,)).fetchone()[0], 'succeeded')
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0], 0)
        self.assertTrue(publication.pending(self.db, self.job))
        with patch.object(worker, 'run') as run:
            worker.prune_job_images(self.db, self.job)
            run.assert_not_called()
        publication.process(self.db, lambda ref: 'sha256:'+'a'*64, self.job)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE status='artifacts-pending'").fetchone()[0], 1)
        self.db.execute('DROP TRIGGER reject_second')
        self.db.close(); self.db = connect(Path(self.tmp.name) / 'db')
        self.assertTrue(publication.process(self.db, lambda ref: 'sha256:'+'a'*64, self.job))
        publication.process(self.db, lambda ref: self.fail('Already published'), self.job)
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT attempts FROM artifact_publications').fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE status='artifacts-published'").fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'succeeded')

    def test_all_images_resolved_before_any_catalog_write(self):
        publication.record(self.db, self.job, self.recipe, self.images, status='validated', detail='Tests passed.')
        calls = []
        def inspect(ref):
            self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0], 0)
            calls.append(ref)
            if len(calls) == 2: raise RuntimeError('SECRET upstream output')
            return 'sha256:'+'b'*64
        self.assertFalse(publication.process(self.db, inspect, self.job))
        self.assertEqual(self.db.execute('SELECT status FROM jobs').fetchone()[0], 'validated')
        self.assertNotIn('SECRET', self.db.execute('SELECT detail FROM artifact_publications').fetchone()[0])
        self.assertEqual(self.db.execute('SELECT count(*) FROM service_artifacts').fetchone()[0], 0)

    def test_late_older_publication_does_not_replace_newer_catalog_source(self):
        import managed_services
        publication.record(self.db, self.job, self.recipe, self.images, status='validated', detail='Tests passed.')
        target = job_target.capture('app', 'legacy-unclassified', 'TPCT/app', 12, 'main', 'c'*40, 'validate', 1, self.recipe, project_revision=1)
        newer, _ = enqueue(self.db, 'newer-publication-fixture', 'app', 'c'*40, True, 'main', 'validate', target_factory=lambda db: target)
        # Equal creation timestamps exercise the monotonic source-ID tie break;
        # updated timestamps deliberately point in the wrong order.
        self.db.execute('UPDATE jobs SET created=100,updated=999 WHERE id=?', (self.job,))
        self.db.execute('UPDATE jobs SET created=100,updated=101 WHERE id=?', (newer,))
        publication.record(self.db, newer, self.recipe, {'app': 'app:newer'}, status='validated', detail='Tests passed.')
        publication.process(self.db, lambda ref: 'sha256:'+'c'*64, newer)
        publication.process(self.db, lambda ref: 'sha256:'+'a'*64, self.job)
        self.assertEqual({item['source_job'] for item in managed_services.catalog(self.db,'app')}, {newer})
        self.assertEqual(managed_services.protected_images(self.db), {'sha256:'+'c'*64})
