import sqlite3
import unittest
from unittest.mock import patch
from delivery_target import public_job

class PublicCleanupTests(unittest.TestCase):
    def test_public_cleanup_is_scoped_and_does_not_expose_internal_fields(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        self.addCleanup(db.close)
        db.execute('CREATE TABLE image_cleanup(job_id INTEGER,kind TEXT,state TEXT,attempts INTEGER,detail TEXT,lease TEXT)')
        db.executemany('INSERT INTO image_cleanup VALUES(?,?,?,?,?,?)',[(1,'build-cache','pending',2,'private driver error','private lease'),(2,'job-images','running',7,'private','private')])
        with patch('job_target.public',side_effect=ValueError):result=public_job(db,{'id':1,'status':'succeeded'})
        self.assertEqual(result['cleanup'],[{'kind':'build-cache','state':'pending','attempts':2}])
        self.assertEqual(result['status'],'succeeded')
    def test_legacy_without_table_has_no_invented_cleanup_success(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        with patch('job_target.public',side_effect=ValueError):result=public_job(db,{'id':1})
        self.assertNotIn('cleanup',result)
