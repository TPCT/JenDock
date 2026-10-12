import sqlite3
import unittest
from unittest.mock import Mock, patch
import worker

class CollectorRecoveryTests(unittest.TestCase):
    def test_reconnects_after_initial_and_mid_collection_lock(self):
        first, second = Mock(), Mock()
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, False, True]
        collect = Mock(side_effect=[sqlite3.OperationalError('database is locked'), None, None])
        with patch.object(worker, 'connect', side_effect=[sqlite3.OperationalError('database is locked'), first, second]):
            worker.collect_resiliently(collect, 5, stop)
        self.assertEqual(collect.call_count, 3)
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertEqual(stop.wait.call_count, 4)

    def test_does_not_hide_database_corruption(self):
        stop = Mock(); stop.is_set.return_value = False
        db = Mock()
        with patch.object(worker, 'connect', return_value=db):
            with self.assertRaises(sqlite3.OperationalError):
                worker.collect_resiliently(Mock(side_effect=sqlite3.OperationalError('database disk image is malformed')), 5, stop)
        db.close.assert_called_once()
