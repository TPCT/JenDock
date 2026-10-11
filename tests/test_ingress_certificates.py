import os
from pathlib import Path
import tempfile
import unittest
import ingress_certificates as certificates

class CertificateBoundaryTests(unittest.TestCase):
    def test_closed_admin_policy(self):
        with self.assertRaises(ValueError):certificates.CertificateManager({'executable':'/bin/true','hooks':['unsafe']},owner_uid=os.getuid())
    def test_bounded_runner_timeout_has_no_raw_output(self):
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'runner';executable.write_text('#!/bin/sh\nprintf private-secret\nsleep 5\n');executable.chmod(0o700)
            with self.assertRaises(certificates.CommandError) as raised:certificates.run_bounded([str(executable)],timeout=.1,env={'PATH':'/usr/bin:/bin'})
            self.assertTrue(raised.exception.ended);self.assertNotIn('private-secret',str(raised.exception))

class CertificateIssuanceTests(unittest.TestCase):
    def setUp(self):
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
        self.tmp=tempfile.TemporaryDirectory(prefix='owned-cert-');self.root=Path(self.tmp.name);self.route='a'*32;self.operation='b'*32;self.hostname='app.fixture.test'
        self.config={'expected_version':'5.8.0','server':'https://acme.fixture.test/directory','account':'c'*32,'email':'admin@fixture.test','agree_tos':True,'timeout':5}
        for key in ('config_dir','work_dir','logs_dir','challenge_root','state_dir','generations_dir'):
            path=self.root/key;path.mkdir(mode=0o700);self.config[key]=str(path)
        executable=self.root/'certbot';executable.write_text('fixture');executable.chmod(0o700);self.config['executable']=str(executable)
        now=datetime.datetime.now(datetime.timezone.utc)
        self.ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Fixture CA')])
        self.ca=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.ca_key.public_key()).serial_number(1).not_valid_before(now-datetime.timedelta(hours=1)).not_valid_after(now+datetime.timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True,path_length=1),critical=True).add_extension(x509.KeyUsage(True,False,False,False,False,True,True,False,False),critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()),critical=False).sign(self.ca_key,hashes.SHA256()))
        self.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        self.leaf=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,self.hostname)])).issuer_name(name).public_key(self.key.public_key()).serial_number(2).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=12)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.SubjectAlternativeName([x509.DNSName(self.hostname)]),critical=False).add_extension(x509.KeyUsage(True,False,True,False,False,False,False,False,False),critical=True).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()),critical=False).sign(self.ca_key,hashes.SHA256()))
        pem=lambda cert:cert.public_bytes(serialization.Encoding.PEM)
        self.files={'cert':pem(self.leaf),'chain':pem(self.ca),'fullchain':pem(self.leaf)+pem(self.ca),'privkey':self.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())}
        trust=self.root/'trust.pem';trust.write_bytes(pem(self.ca));trust.chmod(0o600);self.config['ca_bundle']=str(trust)
        self.calls=[];self.lost=False;self.issued=True;self.authorizations=[]
        self.manager=certificates.CertificateManager(self.config,owner_uid=os.getuid(),executor=self.execute)
    def tearDown(self):self.tmp.cleanup()
    def authorize(self):self.authorizations.append(True)
    def lineage(self):
        name='deploy-monitor-'+self.route;base=Path(self.config['config_dir']);live=base/'live'/name;archive=base/'archive'/name
        live.mkdir(parents=True,mode=0o700,exist_ok=True);archive.mkdir(parents=True,mode=0o700,exist_ok=True);(base/'renewal').mkdir(mode=0o700,exist_ok=True)
        live.parent.chmod(0o700);archive.parent.chmod(0o700)
        for role,data in self.files.items():
            target=archive/(role+'1.pem');target.write_bytes(data);target.chmod(0o600)
            link=live/(role+'.pem')
            if link.is_symlink():link.unlink()
            link.symlink_to('../../archive/'+name+'/'+role+'1.pem')
        config='archive_dir = '+str(archive)+'\n'+''.join(role+' = '+str(live/(role+'.pem'))+'\n' for role in certificates.ROLES)
        config+='[renewalparams]\nserver = '+self.config['server']+'\naccount = '+self.config['account']+'\nauthenticator = webroot\nwebroot_path = '+self.config['challenge_root']+',\n[[webroot_map]]\n'+self.hostname+' = '+self.config['challenge_root']+'\n'
        renewal=base/'renewal'/(name+'.conf');renewal.write_text(config);renewal.chmod(0o600);return live,archive
    def execute(self,argv,**kwargs):
        if '--version' in argv:return b'certbot 5.8.0\n'
        self.calls.append(argv)
        if self.issued:self.lineage()
        if self.lost:raise TimeoutError('private-token')
        return b''
    def request(self,**kwargs):return self.manager.request(self.route,self.operation,self.hostname,authorize=self.authorize,**kwargs)
    def test_actual_chain_key_san_verification_and_immutable_generation(self):
        evidence=self.request();self.assertEqual(evidence['status'],'verified');self.assertEqual(evidence['sans'],[self.hostname]);self.assertNotIn('privkey',str(evidence))
        paths=self.manager.generation_files(self.route,evidence['generation']);self.assertEqual(Path(paths['privkey']).stat().st_mode&0o777,0o400)
        self.assertEqual(self.request()['generation'],evidence['generation']);self.assertEqual(len(self.calls),1)
        self.assertIn('--no-directory-hooks',self.calls[0]);self.assertNotIn('--apache',self.calls[0])
        Path(paths['cert']).chmod(0o600);Path(paths['cert']).write_bytes(b'changed')
        with self.assertRaises(ValueError):self.manager.generation_files(self.route,evidence['generation'])
    def test_success_lost_reply_and_restart_do_not_reissue(self):
        self.lost=True;first=self.request();self.assertEqual(first['status'],'verified')
        restarted=certificates.CertificateManager(self.config,owner_uid=os.getuid(),executor=self.execute)
        self.assertEqual(restarted.request(self.route,self.operation,self.hostname,authorize=self.authorize)['generation'],first['generation']);self.assertEqual(len(self.calls),1)
    def test_absent_uncertain_result_never_blind_retries(self):
        self.assertEqual(self.manager.inspect(self.route,self.operation,self.hostname,authorize=self.authorize)['status'],'absent')
        self.issued=False;self.lost=True
        self.assertEqual(self.request()['status'],'reconciliation-required');self.request(retry=True);self.assertEqual(len(self.calls),1)
    def test_foreign_lineage_not_adopted(self):
        self.lineage()
        with self.assertRaises(ValueError):self.request()
        self.assertEqual(self.calls,[])
    def test_permissions_links_hardlinks_and_wrong_hostname_rejected(self):
        evidence=self.request();live,archive=self.lineage()
        (archive/'privkey1.pem').chmod(0o644)
        self.assertEqual(self.manager.inspect(self.route,self.operation,self.hostname,authorize=self.authorize)['status'],'reconciliation-required')
        (archive/'privkey1.pem').chmod(0o600);os.link(archive/'cert1.pem',archive/'alias.pem')
        self.assertEqual(self.manager.inspect(self.route,self.operation,self.hostname,authorize=self.authorize)['status'],'reconciliation-required')
        (archive/'alias.pem').unlink();(live/'cert.pem').unlink();(live/'cert.pem').symlink_to('/etc/passwd')
        self.assertEqual(self.manager.inspect(self.route,self.operation,self.hostname,authorize=self.authorize)['status'],'reconciliation-required')
        with self.assertRaises(ValueError):self.manager.inspect(self.route,self.operation,'other.fixture.test',authorize=self.authorize)
    def test_renewal_hook_rejected_without_execution(self):
        self.request();path=Path(self.config['config_dir'])/'renewal'/('deploy-monitor-'+self.route+'.conf')
        with path.open('a') as stream:stream.write('deploy_hook = forbidden-command\n')
        self.assertEqual(self.request(renew=True)['status'],'reconciliation-required');self.assertEqual(len(self.calls),1)
    def test_wrong_key_and_untrusted_chain_rejected(self):
        from cryptography.hazmat.primitives import serialization
        self.files['privkey']=self.ca_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        self.assertEqual(self.request()['status'],'reconciliation-required')
    def test_writable_ancestor_and_policy_change_rejected(self):
        self.root.chmod(0o777)
        with self.assertRaises(ValueError):certificates.CertificateManager(self.config,owner_uid=os.getuid(),executor=self.execute)
        self.root.chmod(0o700);self.request()
        other=dict(self.config,email='other@fixture.test');manager=certificates.CertificateManager(other,owner_uid=os.getuid(),executor=self.execute)
        with self.assertRaises(ValueError):manager.request(self.route,self.operation,self.hostname,authorize=self.authorize)

    def test_exact_san_and_ca_trust_are_required(self):
        with self.assertRaises(ValueError):self.manager._verify(self.files,'other.fixture.test')
        Path(self.config['ca_bundle']).write_bytes(self.files['cert'])
        with self.assertRaises(ValueError):self.manager._verify(self.files,self.hostname)
    def test_revocation_after_issuance_leaves_intent_for_safe_reconciliation(self):
        def authorize():
            if self.calls:raise ValueError('revoked')
        with self.assertRaises(ValueError):self.manager.request(self.route,self.operation,self.hostname,authorize=authorize)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.request()['status'],'verified');self.assertEqual(len(self.calls),1)
    def test_retry_cannot_run_saved_renewal_hooks(self):
        self.issued=False
        def failed(argv,**kwargs):
            if '--version' in argv:return b'certbot 5.8.0'
            self.calls.append(argv);raise certificates.CommandError()
        self.manager.executor=failed;self.request();self.lineage()
        renewal=Path(self.config['config_dir'])/'renewal'/('deploy-monitor-'+self.route+'.conf')
        with renewal.open('a') as stream:stream.write('pre_hook = forbidden-command\n')
        journal=self.manager._journal(self.route);journal['started']-=600;self.manager._save(self.route,journal)
        with self.assertRaises(ValueError):self.request(retry=True)
        self.assertEqual(len(self.calls),1)
    def test_no_unowned_generation_or_operation_replay(self):
        evidence=self.request();journal=self.manager._journal(self.route);journal['generations']={};self.manager._save(self.route,journal)
        with self.assertRaises(ValueError):self.manager.generation_files(self.route,evidence['generation'])
        with self.assertRaises(ValueError):self.manager.request(self.route,'d'*32,self.hostname,authorize=self.authorize)
    def test_renew_uses_one_named_lineage_and_preserves_generation(self):
        first=self.request();second=self.request(renew=True)
        self.assertEqual(second['generation'],first['generation']);self.assertEqual(self.calls[-1][1],'renew')
        self.assertIn('--cert-name',self.calls[-1]);self.assertNotIn('--force-renewal',self.calls[-1])
        self.manager.generation_files(self.route,first['generation'])

    def test_verified_lost_reply_does_not_prove_child_ended_for_renewal(self):
        self.lost=True;self.assertEqual(self.request()['status'],'verified')
        self.assertFalse(self.manager._journal(self.route)['process_ended'])
        self.assertEqual(self.request(renew=True)['status'],'reconciliation-required')
        self.assertEqual(len(self.calls),1)

    def test_expired_journal_owned_certificate_can_renew_but_not_serve(self):
        import datetime
        from unittest.mock import patch
        self.request();initial=self.manager._journal(self.route)['evidence']
        real_datetime=datetime.datetime
        future=real_datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=13)
        class Later(real_datetime):
            @classmethod
            def now(cls,tz=None):return future
        with patch.object(certificates.datetime,'datetime',Later),patch.object(certificates.time,'time',return_value=future.timestamp()):
            with self.assertRaises(ValueError):self.manager.generation_files(self.route,initial['generation'])
            self.request(renew=True)
        self.assertEqual(len(self.calls),2);self.assertEqual(self.calls[-1][1],'renew')

    def test_unjournaled_expired_bytes_cannot_authorize_renewal(self):
        import datetime
        from unittest.mock import patch
        self.request();journal=self.manager._journal(self.route);journal['generations']={};self.manager._save(self.route,journal)
        future=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=13)
        class Later(datetime.datetime):
            @classmethod
            def now(cls,tz=None):return future
        with patch.object(certificates.datetime,'datetime',Later),patch.object(certificates.time,'time',return_value=future.timestamp()):
            self.assertEqual(self.request(renew=True)['status'],'reconciliation-required')
        self.assertEqual(len(self.calls),1)

    def test_renewal_webroot_path_and_map_are_pinned_before_command(self):
        self.request();path=Path(self.config['config_dir'])/'renewal'/('deploy-monitor-'+self.route+'.conf');original=path.read_text()
        for replacement in (original.replace('webroot_path = '+self.config['challenge_root'],'webroot_path = /foreign'),
                            original.replace(self.hostname+' = '+self.config['challenge_root'],self.hostname+' = /foreign'),
                            original+'other.fixture.test = '+self.config['challenge_root']+'\n'):
            path.write_text(replacement)
            self.assertEqual(self.request(renew=True)['status'],'reconciliation-required')
            self.assertEqual(len(self.calls),1)
        path.write_text(original);self.request(renew=True)
        command=self.calls[-1];self.assertEqual(command[command.index('--webroot-path')+1],self.config['challenge_root'])

    def test_known_ended_failed_renewal_retry_requires_backoff_and_owned_bytes(self):
        from unittest.mock import patch
        self.request()
        def fail(argv,**kwargs):
            if '--version' in argv:return b'certbot 5.8.0'
            self.calls.append(argv)
            raise certificates.CommandError(ended=True)
        self.manager.executor=fail
        self.assertEqual(self.request(renew=True)['status'],'reconciliation-required')
        status=lambda:self.manager.renewal_status(self.route,self.operation,self.hostname,authorize=self.authorize)
        self.assertFalse(status()['retryable'])
        started=self.manager._journal(self.route)['started']
        with patch.object(certificates.time,'time',return_value=started+301):
            self.assertTrue(status()['retryable'])
            self.request(renew=True,retry=True)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.manager._journal(self.route)['previous_fingerprint'],self.manager._verify(self.files,self.hostname)['fingerprint'])

    def test_unknown_renewal_process_never_becomes_retryable_by_age(self):
        from unittest.mock import patch
        self.request();self.lost=True
        self.assertEqual(self.request(renew=True)['status'],'reconciliation-required')
        started=self.manager._journal(self.route)['started']
        with patch.object(certificates.time,'time',return_value=started+301):
            status=self.manager.renewal_status(self.route,self.operation,self.hostname,authorize=self.authorize)
            self.assertFalse(status['retryable'])
            self.request(renew=True,retry=True)
        self.assertEqual(len(self.calls),2)

    def test_certbot_58_non_due_ari_cache_is_owned_non_executable_metadata(self):
        self.request()
        path=Path(self.config['config_dir'])/'renewal'/('deploy-monitor-'+self.route+'.conf')
        with path.open('a') as stream:stream.write('\n[acme_renewal_info]\nari_retry_after = 2026-10-12T15:04:05\n')
        result=self.request(renew=True)
        self.assertEqual(result['status'],'verified')

    def test_ari_cache_rejects_unknown_duplicate_hooks_and_bad_timestamp(self):
        self.request()
        path=Path(self.config['config_dir'])/'renewal'/('deploy-monitor-'+self.route+'.conf');original=path.read_text()
        for metadata in ('[acme_renewal_info]\npre_hook = command\n',
                         '[acme_renewal_info]\nunknown = 2026-10-12T15:04:05\n',
                         '[acme_renewal_info]\nari_retry_after = bad\n',
                         '[acme_renewal_info]\nari_retry_after = 2026-99-12T15:04:05\n',
                         '[acme_renewal_info]\nari_retry_after = 2026-10-12T15:04:05\nari_retry_after = 2026-10-13T15:04:05\n',
                         '[acme_renewal_info]\nari_retry_after = 2026-10-12T15:04:05\n[arbitrary]\nx = y\n'):
            with self.subTest(metadata=metadata):
                path.write_text(original+'\n'+metadata)
                with self.assertRaises(ValueError):self.manager._renewal('deploy-monitor-'+self.route,self.hostname)
