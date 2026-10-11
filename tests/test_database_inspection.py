"""Real SQLite/session control-plane contracts; scripted runtime evidence only."""
import copy
import json
import time
import unittest
from cryptography.fernet import Fernet
import database_auth
import database_bindings as bindings
import database_candidates
import database_inspection as inspection
import registry
import test_database_bindings as fixtures


class ScriptedHelper:
    image='sha256:'+'f'*64
    def __init__(self,case):self.case=case;self.calls=0;self.hook=None;self.response=None
    def inspect(self,target,payload,*,revalidate):
        self.calls+=1
        if self.case.db.in_transaction:raise AssertionError('helper called inside transaction')
        revalidate(target)
        if self.hook:self.hook()
        revalidate(target)
        if self.response is not None:return copy.deepcopy(self.response)
        return {'result':{'engine':'postgresql','identity':{'database':'app','user':'reader','database_oid':'42','server_version':'170004','cluster_identity_verified':False},
            'data':['public'],'next_cursor':None,'page_size':25,'observed_at':time.time()}}


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.DatabaseBindingsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
        self.owner=self.f.principal.subject_id
        self.config=self.f.key.parent/'owner.json';self.config.write_text(json.dumps({'username':self.owner}))
        now=time.time();self.session='d'*64
        self.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(self.session,'csrf',self.owner,now-10,now-1))
        self.authority=database_auth.ConsoleAuthority(self.db,{'kind':'console','session_digest':self.session},self.config)
        secret=bindings.save_credential(self.db,self.authority.principal,'app','development',{'username':'reader','password':'private-password'},expected_revision=0,key_path=self.f.key)
        values=self.f.definition(secret)
        recipe=self.db.execute("SELECT id FROM registry_recipes WHERE project='app' LIMIT 1").fetchone()[0]
        self.db.execute("UPDATE registry_environments SET recipe_revision=? WHERE project='app' AND name='development'",(recipe,))
        values['ownership']['revision']=recipe
        self.secret=secret
        self.binding=bindings.save_binding(self.db,self.authority.principal,'app','development',values,expected_revision=0)
        env=registry.get_environment(self.db,'app','development')
        self.candidate={'project':'app','environment':'development','reference':'web','engine':'postgresql','database':'app','port':5432,
            'container_id':'a'*64,'image_id':'sha256:'+'b'*64,'recipe_revision':recipe,'environment_revision':env['revision'],
            'project_revision':registry.get_project(self.db,'app')['revision']}
        self.candidate['fingerprint']=database_candidates.digest(self.candidate)
        self.helper=ScriptedHelper(self)
        self.service=inspection.InspectorService(self.db,self.authority,resolve_candidate=self.resolve,helper=self.helper,key_path=self.f.key,cursor_key=Fernet.generate_key().decode())
    def resolve(self,*args,**kwargs):return copy.deepcopy(self.candidate)
    def preview(self):return self.service.preview('app','development',self.binding['id'],1)
    def accept(self,p,key='accept-request-0001'):return self.service.accept('app','development',p['preview_id'],key)
    def count(self,table):return self.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
    def test_success_fresh_accept_and_replay_after_expiry_revocation(self):
        p=self.preview();receipt=self.accept(p);self.assertEqual(self.helper.calls,2)
        self.db.execute('UPDATE database_previews SET expires=0')
        bindings.revoke_credential(self.db,self.authority.principal,'app','development',self.secret['id'],expected_revision=1)
        self.assertEqual(self.accept(p),receipt);self.assertEqual(self.helper.calls,2)
        self.assertEqual(self.count('database_accepted_bindings'),1)
    def test_helper_error_and_wrong_identity_never_create_preview(self):
        for response in ({'error':'permission_denied'},{'result':{'engine':'postgresql','identity':{'database':'other'}}}):
            self.helper.response=response
            with self.assertRaises(inspection.InspectionError):self.preview()
            self.assertEqual(self.count('database_previews'),0)
    def test_expired_first_acceptance_and_wrong_environment(self):
        p=self.preview();self.db.execute('UPDATE database_previews SET expires=0')
        with self.assertRaises(inspection.InspectionError):self.accept(p)
        with self.assertRaises(inspection.InspectionError):self.service.accept('app','production',p['preview_id'],'accept-request-0001')
        self.assertEqual(self.helper.calls,1)
    def test_replaced_candidate_and_rotated_secret_reject_accept(self):
        p=self.preview();self.candidate['container_id']='0'*64
        with self.assertRaises(inspection.InspectionError):self.accept(p)
        self.candidate['container_id']='a'*64
        bindings.revoke_credential(self.db,self.authority.principal,'app','development',self.secret['id'],expected_revision=1)
        with self.assertRaises(inspection.InspectionError):self.accept(p)
        self.assertEqual(self.count('database_accepted_bindings'),0)
    def test_revocation_inside_helper_prevents_preview(self):
        self.helper.hook=lambda:self.db.execute('DELETE FROM sessions')
        with self.assertRaises(Exception):self.preview()
        self.assertEqual(self.count('database_previews'),0)
    def test_engine_recreation_between_preview_accept(self):
        p=self.preview()
        self.helper.response={'result':{'engine':'postgresql','identity':{'database':'app','user':'reader','database_oid':'99','server_version':'170004','cluster_identity_verified':False},'data':['public'],'next_cursor':None,'page_size':25,'observed_at':time.time()}}
        with self.assertRaises(inspection.InspectionError):self.accept(p)
        self.assertEqual(self.count('database_accepted_bindings'),0)
    def test_acceptance_receipt_failure_rolls_back_observation(self):
        p=self.preview()
        self.db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON database_acceptances BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(Exception):self.accept(p)
        self.assertEqual(self.count('database_accepted_bindings'),0)
        self.assertEqual(self.count('database_acceptances'),0)
    def test_read_requires_acceptance_and_replay_conflict(self):
        with self.assertRaises(inspection.InspectionError):self.service.read('app','development',self.binding['id'],1,{'action':'schemas'})
        p=self.preview();self.accept(p)
        result=self.service.read('app','development',self.binding['id'],1,{'action':'schemas'})
        self.assertEqual(result['identity']['database'],'app')
        self.assertEqual(result['scope'],{'project':'app','environment':'development',
            'binding_id':self.binding['id'],'binding_revision':1,
            'definition_digest':self.binding['definition_digest'],
            'container_id':self.candidate['container_id'],
            'candidate_fingerprint':self.service._candidate(self.binding)['fingerprint']})
        p2=self.preview()
        with self.assertRaises(inspection.InspectionError):self.accept(p2)
    def test_changed_helper_image_and_stale_observation_rejected(self):
        p=self.preview();self.helper.image='sha256:'+'e'*64
        with self.assertRaises(inspection.InspectionError):self.accept(p)
        self.helper.response={'result':{'engine':'postgresql','identity':{'database':'app','user':'reader','database_oid':'42','server_version':'170004','cluster_identity_verified':False},'data':['public'],'next_cursor':None,'page_size':25,'observed_at':0}}
        with self.assertRaises(inspection.InspectionError):self.preview()
    def test_changed_owner_cannot_replay_or_read(self):
        p=self.preview();self.accept(p)
        self.config.write_text(json.dumps({'username':'replacement'}))
        with self.assertRaises(Exception):self.accept(p)
        with self.assertRaises(Exception):self.service.read('app','development',self.binding['id'],1,{'action':'schemas'})
    def test_acceptance_projection_is_historical_and_invalidated_by_configuration(self):
        def current():
            return bindings.get_binding(self.db,self.authority.principal,'app','development',self.binding['id'])
        self.assertIsNone(current()['acceptance'])
        self.accept(self.preview())
        accepted=current()
        self.assertFalse(accepted['verified'])
        self.assertTrue(accepted['acceptance']['runtime_revalidation_required'])
        self.assertEqual(accepted['acceptance']['binding_revision'],1)
        bindings.disable_binding(self.db,self.authority.principal,'app','development',self.binding['id'],expected_revision=1)
        self.assertIsNone(current()['acceptance'])
    def test_credential_rotation_invalidates_acceptance_projection(self):
        self.accept(self.preview())
        bindings.save_credential(self.db,self.authority.principal,'app','development',
            {'username':'reader','password':'rotated-password'},expected_revision=1,
            credential_id=self.secret['id'],key_path=self.f.key)
        row=bindings.get_binding(self.db,self.authority.principal,'app','development',self.binding['id'])
        self.assertIsNone(row['acceptance'])
        self.assertEqual(row['status'],'credential-stale')
    def test_binding_revision_invalidates_acceptance_projection(self):
        self.accept(self.preview())
        values={k:v for k,v in self.binding['definition'].items() if k not in ('schema_version','project','environment')}
        row=bindings.save_binding(self.db,self.authority.principal,'app','development',values,
            binding_id=self.binding['id'],expected_revision=1)
        self.assertIsNone(row['acceptance'])

    def test_no_private_payload_in_records(self):
        p=self.preview();self.accept(p)
        for table in ('database_previews','database_acceptances','database_accepted_bindings'):
            self.assertNotIn('private-password',str([tuple(r) for r in self.db.execute('SELECT * FROM '+table)]))

if __name__=='__main__':unittest.main()
