"""Queue maintenance must preserve active work and drain stale registry requests."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import worker
from store import connect, enqueue, snapshot


class WorkerQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temporary.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def test_unknown_request_does_not_starve_later_valid_request(self):
        self.db.execute("INSERT INTO requests VALUES('removed',1)")
        self.db.execute("INSERT INTO requests VALUES('app',2)")
        with patch.object(worker, 'effective_projects', return_value={'app': {'repository': 'TPCT/app'}}), \
             patch.object(worker, 'run', return_value='a' * 40 + '\trefs/heads/main\n') as git:
            self.assertEqual(worker.collect_branches_once(self.db), 'app')
        self.assertEqual(self.db.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM snapshots WHERE name='branches:app'").fetchone())
        self.assertIn('git@github.com:TPCT/app.git', git.call_args.args[0])

    def test_unknown_request_does_not_block_automatic_refresh(self):
        self.db.execute("INSERT INTO requests VALUES('removed',1)")
        with patch.object(worker, 'effective_projects', return_value={'app': {'repository': 'TPCT/app'}}), \
             patch.object(worker, 'run', return_value=''):
            self.assertEqual(worker.collect_branches_once(self.db), 'app')
        self.assertEqual(self.db.execute('SELECT count(*) FROM requests').fetchone()[0], 0)

    def test_current_observations_do_not_fetch_without_request(self):
        snapshot(self.db, 'branches:app', {'branches': {}})
        with patch.object(worker, 'effective_projects', return_value={'app': {'repository': 'TPCT/app'}}), \
             patch.object(worker, 'run') as git:
            self.assertIsNone(worker.collect_branches_once(self.db))
        git.assert_not_called()

    def test_retention_preserves_every_active_state(self):
        active = ('queued', 'validation-queued', 'building', 'testing', 'deploying', 'recovering')
        for index, status in enumerate((*active, 'succeeded')):
            self.db.execute('INSERT INTO jobs(delivery,project,sha,created,updated,status) VALUES(?,?,?,?,?,?)',
                            (str(index), 'old-' + str(index), 'a' * 40, 1, time.time() - 31 * 86400, status))
        enqueue(self.db, 'new', 'fresh', 'b' * 40, True, 'main')
        retained = {row['status'] for row in self.db.execute("SELECT status FROM jobs WHERE project LIKE 'old-%'")}
        self.assertEqual(retained, set(active))
