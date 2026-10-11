import unittest
from concurrent.futures import ThreadPoolExecutor
from store import connect
import application_operations as ops
import test_managed_services as fixture
import ingress_capacity as capacity
import managed_services as services
import resource_policy

class GatewayCapacityTests(unittest.TestCase):
    setUp=fixture.ManagedServiceTests.setUp
    tearDown=fixture.ManagedServiceTests.tearDown
    create=fixture.ManagedServiceTests.create
    def reserve(self,resources=None):
        return capacity.reserve(self.db,'app','development','gateway-operation-123',resources or {'memory_mib':128,'cpu':0.5},1)
    def test_gateway_claim_blocks_application_oversubscription(self):
        self.reserve()
        with self.assertRaises(ValueError):self.create()
        self.assertEqual(capacity.get(self.db,'app','development')['memory_mib'],128)
    def test_existing_application_claim_blocks_gateway(self):
        self.create()
        with self.assertRaises(ValueError):self.reserve()
        self.assertIsNone(capacity.get(self.db,'app','development'))
    def test_missing_operation_identity_is_rejected(self):
        with self.assertRaises(ValueError):capacity.reserve(self.db,'app','development',None,{'memory_mib':64,'cpu':0.2},1)

    def test_replay_and_payload_conflict(self):
        row=self.reserve();self.assertEqual(self.reserve(),row)
        with self.assertRaises(ValueError):self.reserve({'memory_mib':64,'cpu':0.5})
    def test_policy_cannot_remove_retained_gateway_budget(self):
        self.reserve();p=resource_policy.read(self.db,'app')['document']
        p['environments']['development']['budget']['memory_mib']=64
        with self.assertRaises(ValueError):resource_policy.save(self.db,'app',1,p)
    def test_gateway_and_application_can_share_sufficient_budget(self):
        self.reserve({'memory_mib':64,'cpu':0.2});self.create()
        self.assertEqual(services.reservation_totals(self.db,'app','development')['memory_mib'],224)
    def test_uncertain_creation_retains_claim_and_identity(self):
        self.reserve();capacity.begin_creation(self.db,'app','development','gateway-operation-123')
        with self.assertRaises(ValueError):capacity.release(self.db,'app','development','gateway-operation-123')
        capacity.mark_bound(self.db,'app','development','gateway-operation-123','a'*64)
        with self.assertRaises(ValueError):capacity.mark_bound(self.db,'app','development','gateway-operation-123','b'*64)
        self.assertEqual(capacity.host_totals(self.db),{'memory_mib':0,'cpu':0.5})
        self.assertEqual(capacity.totals(self.db,'app','development'),{'memory_mib':128,'cpu':0.5})
    def test_concurrent_gateway_and_application_share_one_budget(self):
        capacity.initialize(self.db)
        def accept(kind):
            db=connect(self.path)
            try:
                try:
                    if kind=='gateway':return capacity.reserve(db,'app','development','concurrent-gateway-id',{'memory_mib':128,'cpu':0.5},1)
                    return ops.create_service(db,self.principal,ops.service_input('app','development',self.values),'concurrent-app-operation')
                except ValueError:return None
            finally:db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(accept,['gateway','app']))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertLessEqual(services.reservation_totals(self.db,'app','development')['memory_mib'],256)

    def test_unbound_release_and_namespace_collision(self):
        self.reserve();capacity.release(self.db,'app','development','gateway-operation-123')
        self.assertIsNone(capacity.get(self.db,'app','development'))
        with self.assertRaises(ValueError):self.create(dict(self.values,name='ingress'))

if __name__=='__main__':unittest.main()
