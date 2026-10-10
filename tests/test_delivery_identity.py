import tempfile
import unittest
from pathlib import Path
from store import connect,enqueue

class DeliveryIdentityTests(unittest.TestCase):
    def test_exact_replay_only_and_other_project_or_payload_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            db=connect(str(Path(directory)/'db'))
            args=dict(delivery='manual-request',project='first',sha='a'*40,enabled=True,branch='main',mode='validate',actor='admin',retry_of=None)
            initial=enqueue(db,**args)
            self.assertEqual(enqueue(db,**args),(initial[0],False))
            for changes in ({'project':'second'},{'sha':'b'*40},{'branch':'production'},{'mode':'deploy'},{'actor':'other'},{'retry_of':3}):
                with self.subTest(changes=changes),self.assertRaises(ValueError):enqueue(db,**dict(args,**changes))
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
            db.close()
