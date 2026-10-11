"""Gateway port reservations are durable intent, never proof of active routing."""
import tempfile
import unittest
from pathlib import Path
import socket
from concurrent.futures import ThreadPoolExecutor
from store import connect
import ingress_ports

class IngressPortsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'db'
        self.db=connect(self.path)
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def reserve(self,env='development',op='operation-12345678',**kwargs):
        return ingress_ports.reserve(self.db,'demo',env,op,first=23000,last=23003,
            occupied_ports=kwargs.pop('occupied_ports',set()),available=kwargs.pop('available',lambda p:True),**kwargs)
    def test_isolated_environment_durable_reservation_and_replay(self):
        dev=self.reserve(); prod=self.reserve('production','operation-87654321')
        self.assertNotEqual(dev['port'],prod['port'])
        other=connect(self.path)
        try:
            self.assertEqual(ingress_ports.get(other,'demo','development'),dev)
            self.assertEqual(self.reserve(occupied_ports={dev['port']}),dev)
        finally:other.close()
    def test_external_listener_and_docker_ports_excluded(self):
        row=self.reserve(occupied_ports={23000},available=lambda p:p!=23001)
        self.assertEqual(row['port'],23002)
    def test_collision_exhaustion_has_no_partial_reservation(self):
        with self.assertRaisesRegex(ValueError,'available'):self.reserve(occupied_ports=set(range(23000,23004)))
        self.assertIsNone(ingress_ports.get(self.db,'demo','development'))
    def test_operation_cannot_claim_other_environment_or_replace_owner(self):
        self.reserve()
        with self.assertRaises(ValueError):self.reserve('production')
        with self.assertRaises(ValueError):self.reserve(op='different-operation')
    def test_release_requires_original_owner_and_unbound_state(self):
        row=self.reserve()
        with self.assertRaises(ValueError):ingress_ports.release(self.db,'demo','development','other-operation')
        ingress_ports.bind(self.db,'demo','development',row['operation_id'],'a'*64)
        with self.assertRaises(ValueError):ingress_ports.release(self.db,'demo','development',row['operation_id'])
        self.assertEqual(ingress_ports.get(self.db,'demo','development')['container_id'],'a'*64)
    def test_bind_requires_matching_owner_and_no_identity_replacement(self):
        row=self.reserve()
        with self.assertRaises(ValueError):ingress_ports.bind(self.db,'demo','development','other-operation','a'*64)
        ingress_ports.bind(self.db,'demo','development',row['operation_id'],'a'*64)
        with self.assertRaises(ValueError):ingress_ports.bind(self.db,'demo','development',row['operation_id'],'b'*64)
    def test_unbound_release_is_idempotent_and_does_not_touch_peer(self):
        self.reserve(); peer=self.reserve('production','operation-87654321')
        ingress_ports.release(self.db,'demo','development','operation-12345678')
        ingress_ports.release(self.db,'demo','development','operation-12345678')
        self.assertEqual(ingress_ports.get(self.db,'demo','production'),peer)
    def test_missing_operation_identity_is_rejected_before_probe(self):
        with self.assertRaises(ValueError):self.reserve(op=None,available=lambda p:self.fail('unexpected probe'))

    def test_invalid_input_never_runs_host_probe(self):
        for kwargs in ({'occupied_ports':None},{'occupied_ports':{True}},{'occupied_ports':{-1}}):
            with self.assertRaises(ValueError):self.reserve(available=lambda p:self.fail('unexpected probe'),**kwargs)
        with self.assertRaises(ValueError):ingress_ports.reserve(self.db,'demo','staging','operation-12345678',first=23000,last=23003,occupied_ports=set())
    def test_ambiguous_create_cannot_release_unbound_port(self):
        row=self.reserve()
        ingress_ports.begin_creation(self.db,'demo','development',row['operation_id'])
        self.db.close();self.db=connect(self.path)
        with self.assertRaises(ValueError):ingress_ports.release(self.db,'demo','development',row['operation_id'])
        self.assertIsNone(ingress_ports.get(self.db,'demo','development')['container_id'])
        ingress_ports.bind(self.db,'demo','development',row['operation_id'],'a'*64)
        self.assertEqual(ingress_ports.get(self.db,'demo','development')['container_id'],'a'*64)

    def test_concurrent_connections_cannot_reserve_same_port(self):
        ingress_ports.initialize(self.db)
        def accept(env):
            db=connect(self.path)
            try:return ingress_ports.reserve(db,'demo',env,'operation-'+env,first=23000,last=23003,occupied_ports=set(),available=lambda p:True)
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(accept,['development','production']))
        self.assertEqual(len({row['port'] for row in rows}),2)

    def test_loopback_probe_detects_actual_listener(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            self.assertFalse(ingress_ports.loopback_available(listener.getsockname()[1]))

if __name__=='__main__':unittest.main()
