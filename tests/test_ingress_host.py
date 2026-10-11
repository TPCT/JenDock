import json
import os
from pathlib import Path
import unittest
import tempfile
import threading
import time
from ingress_host import HostAdapter

class Executor:
    def __init__(self): self.calls=[]; self.names=[]; self.fail=None; self.observed=True
    def observe(self, hostname, token): return self.observed
    def observe_absence(self, hostname): return None if self.observed is None else not self.observed
    def inventory(self): return self.names
    def running(self): return True
    def configtest(self):
        self.calls.append('test')
        if self.fail=='test': raise RuntimeError('invalid')
    def reload(self):
        self.calls.append('reload')
        if self.fail=='reload': raise RuntimeError('uncertain')

def make_host(tmp_path):
    for name in ('managed','state'): (tmp_path/name).mkdir(mode=0o700)
    policy={'managed_dir':str(tmp_path/'managed'),'state_dir':str(tmp_path/'state'), 'listener':{'address':'127.0.0.1','port':80}, 'routes':{'a'*32:{'hostname':'dev.example.test','zone':'example.test','port':31001,'revision':1}}}
    p=tmp_path/'policy.json';p.write_text(json.dumps(policy));p.chmod(0o600)
    e=Executor();return HostAdapter(p,e,owner_uid=os.getuid()),e,tmp_path

def request(): return {'route_id':'a'*32,'operation_id':'b'*32,'expected_digest':None}

class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = make_host(Path(self.temp.name))

    def test_lifecycle_replay(self):
        a,e,p=self.host;s=a.stage_http(request());r=dict(request(),digest=s['digest']);a.activate_http(r)
        assert a.verify({'route_id':'a'*32})['status']=='active'
        a.activate_http(r);assert e.calls==['test','reload']

    def test_foreign_wildcard_collision(self):
        a,e,p=self.host;e.names=[{'names':['*.example.test'],'path':'/foreign.conf'}]
        with self.assertRaises(ValueError):a.stage_http(request())
        assert not list((p/'managed').iterdir())

    def test_closed_request(self):
        with self.assertRaises(ValueError):self.host[0].stage_http(dict(request(),config='evil'))

    def test_failed_config_restores(self):
        a,e,p=self.host;s=a.stage_http(request());e.fail='test'
        with self.assertRaises(RuntimeError):a.activate_http(dict(request(),digest=s['digest']))
        assert not list((p/'managed').iterdir());assert 'reload' not in e.calls

    def test_reload_uncertain_requires_recovery(self):
        a,e,p=self.host;s=a.stage_http(request());e.fail='reload'
        with self.assertRaises(RuntimeError):a.activate_http(dict(request(),digest=s['digest']))
        assert a.inspect({'route_id':'a'*32})['status']=='recovery_required'
        e.fail=None;e.observed=False;a.recover({'route_id':'a'*32});assert not list((p/'managed').iterdir())

    def test_symlink_policy_rejected(self):
        a,e,p=self.host;q=p/'link';q.symlink_to(p/'policy.json')
        with self.assertRaises(ValueError):HostAdapter(q,e,owner_uid=os.getuid()).stage_http(request())

    def test_stopped_apache_not_started(self):
        a,e,p=self.host;s=a.stage_http(request());e.running=lambda:False
        with self.assertRaises(ValueError):a.activate_http(dict(request(),digest=s['digest']))
        assert not e.calls


    def test_unknown_reload_does_not_repeat_effects(self):
        a,e,p=self.host;s=a.stage_http(request());e.fail='reload'
        with self.assertRaises(RuntimeError):a.activate_http(dict(request(),digest=s['digest']))
        calls=list(e.calls);e.observed=None
        with self.assertRaises(ValueError):a.recover({'route_id':'a'*32})
        assert e.calls==calls
        assert list((p/'managed').iterdir())
        e.observed=True
        assert a.recover({'route_id':'a'*32})['status']=='active'
        assert e.calls==calls

    def test_stale_digest_and_changed_policy_fail_before_effects(self):
        a,e,p=self.host
        with self.assertRaises(ValueError):a.stage_http(dict(request(),expected_digest='0'*64))
        staged=a.stage_http(request())
        policy=json.loads((p/'policy.json').read_text())
        policy['routes']['a'*32]['port']=31002
        (p/'policy.json').write_text(json.dumps(policy))
        with self.assertRaises(ValueError):a.activate_http(dict(request(),digest=staged['digest']))
        self.assertEqual(e.calls,[])

    def test_foreign_config_at_owned_name_is_not_adopted(self):
        a,e,p=self.host
        target=p/'managed'/('a'*32+'.conf');target.write_text('foreign')
        with self.assertRaises(ValueError):a.stage_http(request())
        self.assertEqual(target.read_text(),'foreign')

    def test_syntax_failure_preserves_previous_active_route(self):
        a,e,p=self.host
        first=a.stage_http(request());a.activate_http(dict(request(),digest=first['digest']))
        target=p/'managed'/('a'*32+'.conf');before=target.read_bytes()
        policy=json.loads((p/'policy.json').read_text());policy['routes']['a'*32]['revision']=2
        (p/'policy.json').write_text(json.dumps(policy))
        new=dict(request(),operation_id='c'*32,expected_digest=first['digest'])
        staged=a.stage_http(new);e.fail='test'
        with self.assertRaises(RuntimeError):a.activate_http(dict(new,digest=staged['digest']))
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(e.calls,['test','reload','test'])

    def test_crash_after_install_journal_recovers_observed_config(self):
        a,e,p=self.host;s=a.stage_http(request())
        state=p/'state'/('a'*32+'.json');data=json.loads(state.read_text())
        data['status']='installing';state.write_text(json.dumps(data))
        target=p/'managed'/('a'*32+'.conf');target.write_text(data['candidate']);target.chmod(0o600)
        recovered=HostAdapter(p/'policy.json',e,owner_uid=os.getuid()).recover({'route_id':'a'*32})
        self.assertEqual(recovered['status'],'active');self.assertEqual(e.calls,[])

    def test_unsafe_parent_link_rejected(self):
        a,e,p=self.host
        (p/'alias').symlink_to(p/'managed',target_is_directory=True)
        policy=json.loads((p/'policy.json').read_text());policy['managed_dir']=str(p/'alias')
        (p/'policy.json').write_text(json.dumps(policy))
        with self.assertRaises(ValueError):a.stage_http(request())

    def test_unverified_activation_not_active(self):
        a,e,p=self.host;s=a.stage_http(request());e.observed=None
        with self.assertRaises(ValueError):a.activate_http(dict(request(),digest=s['digest']))
        self.assertEqual(a.inspect({'route_id':'a'*32})['status'],'recovery_required')

    def test_lock_serializes_complete_activation(self):
        a,e,p=self.host;s=a.stage_http(request())
        entered=threading.Event();release=threading.Event();inspected=threading.Event();failures=[]
        def configtest():
            entered.set()
            if not release.wait(2):raise RuntimeError('test timeout')
        e.configtest=configtest
        def activate():
            try:a.activate_http(dict(request(),digest=s['digest']))
            except Exception as error:failures.append(error)
        def inspect():
            try:a.inspect({'route_id':'a'*32});inspected.set()
            except Exception as error:failures.append(error)
        first=threading.Thread(target=activate);second=threading.Thread(target=inspect)
        first.start()
        try:
            self.assertTrue(entered.wait(2));second.start()
            self.assertFalse(inspected.wait(.05))
        finally:
            release.set();first.join(2)
            if second.ident:second.join(2)
        self.assertTrue(inspected.is_set());self.assertEqual(failures,[])

    def test_policy_and_root_ancestors_cannot_be_symlinks(self):
        a,e,p=self.host
        real=p/'real';real.mkdir(mode=0o700)
        (real/'managed').mkdir(mode=0o700)
        (real/'state').mkdir(mode=0o700)
        alias=p/'ancestor';alias.symlink_to(real,target_is_directory=True)
        original=json.loads((p/'policy.json').read_text())
        # Final entries are ordinary files/directories; only an ancestor is linked.
        linked_policy=real/'policy.json'
        linked_policy.write_text(json.dumps(original));linked_policy.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'traverse symlinks'):
            HostAdapter(alias/'policy.json',e,owner_uid=os.getuid()).stage_http(request())
        for field in ('managed_dir','state_dir'):
            with self.subTest(field=field):
                policy=dict(original)
                policy[field]=str(alias/('managed' if field=='managed_dir' else 'state'))
                (p/'policy.json').write_text(json.dumps(policy))
                with self.assertRaisesRegex(ValueError,'traverse symlinks'):
                    a.stage_http(request())
        self.assertEqual(e.calls,[])
        self.assertEqual(list((p/'managed').iterdir()),[])
        self.assertEqual(list((real/'managed').iterdir()),[])

    def test_group_or_world_writable_ancestor_is_rejected_with_secure_leaves(self):
        a,e,p=self.host
        for mode in (0o777,0o770):
            with self.subTest(mode=mode):
                p.chmod(mode)
                try:
                    with self.assertRaisesRegex(ValueError,'Unsafe host filesystem'):a.stage_http(request())
                finally:p.chmod(0o700)
        self.assertEqual(e.calls,[])

    def prepare_restore(self):
        a,e,p=self.host
        first=a.stage_http(request());a.activate_http(dict(request(),digest=first['digest']))
        previous_token=json.loads((p/'state'/('a'*32+'.json')).read_text())['grant_digest']
        policy=json.loads((p/'policy.json').read_text());policy['routes']['a'*32]['revision']=2
        (p/'policy.json').write_text(json.dumps(policy))
        new=dict(request(),operation_id='c'*32,expected_digest=first['digest'])
        staged=a.stage_http(new);e.fail='reload'
        with self.assertRaises(RuntimeError):a.activate_http(dict(new,digest=staged['digest']))
        e.fail=None
        return a,e,p,previous_token

    def test_restore_unknown_observation_keeps_restoring_without_repeat_reload(self):
        a,e,p,prior=self.prepare_restore()
        e.observe=lambda hostname,token:None if token==prior else False
        with self.assertRaises(ValueError):a.recover({'route_id':'a'*32})
        self.assertEqual(a.inspect({'route_id':'a'*32})['status'],'restoring')
        calls=list(e.calls)
        with self.assertRaises(ValueError):a.recover({'route_id':'a'*32})
        self.assertEqual(e.calls,calls)
        e.observe=lambda hostname,token:token==prior
        self.assertEqual(a.recover({'route_id':'a'*32})['status'],'staged')
        self.assertEqual(e.calls,calls)

    def test_lost_restore_reload_reply_is_observed_without_repeat_reload(self):
        a,e,p,prior=self.prepare_restore();e.observe=lambda *args:False;e.fail='reload'
        with self.assertRaises(RuntimeError):a.recover({'route_id':'a'*32})
        calls=list(e.calls);e.fail=None;e.observe=lambda hostname,token:token==prior
        self.assertEqual(a.recover({'route_id':'a'*32})['status'],'staged')
        self.assertEqual(e.calls,calls)

    def test_initial_restore_requires_explicit_absence_observation(self):
        a,e,p=self.host;staged=a.stage_http(request());e.fail='reload'
        with self.assertRaises(RuntimeError):a.activate_http(dict(request(),digest=staged['digest']))
        e.fail=None;e.observed=False;e.observe_absence=None
        with self.assertRaisesRegex(ValueError,'restoration outcome'):a.recover({'route_id':'a'*32})
        self.assertEqual(a.inspect({'route_id':'a'*32})['status'],'restoring')
        self.assertFalse((p/'managed'/('a'*32+'.conf')).exists())
        calls=list(e.calls)
        e.observe_absence=lambda hostname:True
        self.assertEqual(a.recover({'route_id':'a'*32})['status'],'staged')
        self.assertEqual(e.calls,calls)
