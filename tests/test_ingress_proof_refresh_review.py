"""Independent crash/replay boundary checks for backend proof refresh."""
import json
import unittest
from unittest.mock import patch
import ingress_runtime as runtime
import ingress_route_generations as generations
import test_ingress_proof_refresh as fixtures


class ProofRefreshReviewTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ProofRefreshTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_unknown_reload_never_signals_again(self):
        def lost(args,**kwargs):
            if '-s' in args:
                self.f.commands.append(args)
                raise TimeoutError('outcome unknown')
            return self.f.command(args,**kwargs)
        with patch.object(runtime,'_command',side_effect=lost),self.assertRaises(TimeoutError):self.f.apply()
        self.assertEqual(generations.operation(self.f.db,self.f.operation)['phase'],'reload-intent')
        for _ in range(2):
            with self.assertRaises(ValueError):self.f.recover()
        self.assertEqual(self.f.signals(),1)
    def test_completed_refresh_cannot_replay_after_backend_advances(self):
        self.f.apply()
        self.f.db.execute("UPDATE ingress_backend_bindings SET generation=generation+1,backend=replace(backend,?,?) WHERE service_id='service-id'",('8'*64,'6'*64))
        with self.assertRaisesRegex(ValueError,'superseded'):self.f.apply()
        self.assertEqual(self.f.signals(),1)
    def test_pre_signal_abort_is_not_silently_retried(self):
        with patch.object(runtime,'_command',side_effect=ValueError('config test unavailable')),self.assertRaises(ValueError):self.f.apply()
        old=generations.operation(self.f.db,self.f.operation)
        with self.assertRaisesRegex(ValueError,'aborted'):self.f.apply()
        self.assertEqual(generations.operation(self.f.db,self.f.operation),old)
        self.assertEqual(self.f.signals(),0)
