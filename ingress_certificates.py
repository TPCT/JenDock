"""Owned Certbot lineages and immutable certificate generations.

The trusted host adapter owns the mutation lock and supplies fresh authority.
Public results never contain private key bytes, paths, or subprocess output.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from cryptography.x509.verification import PolicyBuilder, Store

LIMIT=65536
ID=re.compile(r'[a-f0-9]{32}\Z')
HOST=re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
ROLES=('cert','chain','fullchain','privkey')

class CertificateError(ValueError):pass
class CommandError(CertificateError):
    def __init__(self,ended=True):super().__init__('Certificate command did not complete successfully.');self.ended=ended


def run_bounded(argv,*,timeout,env):
    """No shell/inherited environment; kill and reap the entire command group."""
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                             env=env,start_new_session=True,close_fds=True,umask=0o077)
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
    output=bytearray();deadline=time.monotonic()+timeout
    try:
        while selector.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=0:raise CommandError()
            for key,_ in selector.select(min(.1,remaining)):
                chunk=os.read(key.fd,8192)
                if not chunk:selector.unregister(key.fileobj)
                else:
                    output.extend(chunk)
                    if len(output)>LIMIT:raise CommandError()
        if process.wait(timeout=max(.001,deadline-time.monotonic()))!=0:raise CommandError()
        return bytes(output)
    except BaseException:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)
        raise
    finally:selector.close();process.stdout.close()


def _digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class CertificateManager:
    FIELDS={'executable','expected_version','server','account','email','agree_tos','config_dir','work_dir','logs_dir','challenge_root','state_dir','generations_dir','ca_bundle','timeout'}
    def __init__(self,config,*,owner_uid=0,executor=None):
        if not isinstance(config,dict) or set(config)!=self.FIELDS:raise CertificateError('Invalid certificate administrator policy.')
        self.config=dict(config);self.owner_uid=owner_uid;self.executor=executor or run_bounded
        if type(owner_uid) is not int or owner_uid<0:raise CertificateError('Invalid certificate administrator identity.')
        if type(config['timeout']) is not int or not 5<=config['timeout']<=600 or config['agree_tos'] is not True:raise CertificateError('Certificate timeout and terms policy are required.')
        if not isinstance(config['expected_version'],str) or not re.fullmatch(r'\d+\.\d+\.\d+',config['expected_version']):raise CertificateError('Pin the Certbot version.')
        if not isinstance(config['account'],str) or not ID.fullmatch(config['account']) or not isinstance(config['email'],str) or not re.fullmatch(r'[^\s@,]+@[^\s@,]+',config['email']):raise CertificateError('An exact installed ACME account is required.')
        url=urlsplit(config['server']) if isinstance(config['server'],str) else None
        if not url or url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or any(c.isspace() for c in config['server']):raise CertificateError('Configure one trusted HTTPS ACME directory.')
        self.paths={key:Path(config[key]) for key in ('executable','config_dir','work_dir','logs_dir','challenge_root','state_dir','generations_dir','ca_bundle') if isinstance(config[key],str)}
        if len(self.paths)!=8:raise CertificateError('Administrator paths are required.')
        for name,path in self.paths.items():self._safe(path,directory=name.endswith('_dir') or name=='challenge_root')
        dirs=[self.paths[k] for k in ('config_dir','work_dir','logs_dir','challenge_root','state_dir','generations_dir')]
        if any(a==b or a in b.parents or b in a.parents for i,a in enumerate(dirs) for b in dirs[i+1:]):raise CertificateError('Certificate directories must be separate dedicated namespaces.')
        self.policy_digest=_digest(config)
        self.environment={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8','HOME':str(self.paths['work_dir']),'REQUESTS_CA_BUNDLE':str(self.paths['ca_bundle'])}

    def _safe(self,path,*,directory=False,private=False):
        if not path.is_absolute() or '..' in path.parts or str(path).startswith('//'):raise CertificateError('Certificate paths must be absolute and normalized.')
        for parent in reversed(path.parents):
            info=parent.lstat()
            sticky=parent==Path('/tmp') and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o1777
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,self.owner_uid) or info.st_mode&0o022 and not sticky:raise CertificateError('Certificate ancestor is not trusted.')
        info=path.lstat()
        if info.st_uid not in (0,self.owner_uid) or info.st_mode&0o022 or directory and not stat.S_ISDIR(info.st_mode) or not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1):raise CertificateError('Certificate file ownership or type is invalid.')
        if private and (info.st_uid!=self.owner_uid or stat.S_IMODE(info.st_mode) not in (0o400,0o600)):raise CertificateError('Private key permissions are invalid.')
        return info

    def _read(self,path,*,private=False):
        before=self._safe(path,private=private)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            actual=os.fstat(fd)
            if (actual.st_dev,actual.st_ino)!=(before.st_dev,before.st_ino):raise CertificateError('Certificate file changed.')
            data=os.read(fd,LIMIT+1)
            if len(data)>LIMIT:raise CertificateError('Certificate file exceeds its bound.')
            return data
        finally:os.close(fd)

    def _atomic(self,path,data,mode=0o600):
        self._safe(path.parent,directory=True)
        fd,name=tempfile.mkstemp(prefix='.certificate-',dir=path.parent)
        try:
            os.fchmod(fd,mode)
            with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(name,path)
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.lexists(name):os.unlink(name)

    def _identity(self,route,operation,hostname):
        if not isinstance(route,str) or not ID.fullmatch(route) or not isinstance(operation,str) or not ID.fullmatch(operation) or not isinstance(hostname,str) or not HOST.fullmatch(hostname):raise CertificateError('Exact route, operation and normalized hostname are required.')
        return 'deploy-monitor-'+route

    def _journal(self,route):
        path=self.paths['state_dir']/(route+'.json')
        if not os.path.lexists(path):return None
        try:value=json.loads(self._read(path))
        except (ValueError,TypeError):raise CertificateError('Certificate journal is invalid.') from None
        if not isinstance(value,dict):raise CertificateError('Certificate journal is invalid.')
        return value

    def _save(self,route,journal):self._atomic(self.paths['state_dir']/(route+'.json'),json.dumps(journal,sort_keys=True).encode())

    def _lineage(self,name,hostname):
        live=self.paths['config_dir']/'live'/name;archive=self.paths['config_dir']/'archive'/name
        self._safe(live,directory=True);self._safe(archive,directory=True)
        result={};generation=None
        for role in ROLES:
            link=live/(role+'.pem');info=link.lstat()
            if not stat.S_ISLNK(info.st_mode) or info.st_uid!=self.owner_uid:raise CertificateError('Expected an owned Certbot live link.')
            target=os.readlink(link)
            match=re.fullmatch(r'../../archive/'+re.escape(name)+'/'+role+r'([1-9][0-9]{0,7})\.pem',target)
            if not match:raise CertificateError('Certificate live link escapes its lineage.')
            if generation is not None and generation!=match[1]:raise CertificateError('Certificate live generation is inconsistent.')
            generation=match[1];result[role]=self._read(archive/(role+generation+'.pem'),private=role=='privkey')
        self._renewal(name,hostname)
        return result

    def _renewal(self,name,hostname):
        live=self.paths['config_dir']/'live'/name;archive=self.paths['config_dir']/'archive'/name
        renewal=self._read(self.paths['config_dir']/'renewal'/(name+'.conf')).decode('utf-8')
        values={};webroots={};section='top';seen_map=False
        def scalar(value):
            if len(value)>=2 and value[0] in (chr(34),chr(39)) and value[-1]==value[0]:return value[1:-1]
            return value
        for line in renewal.splitlines():
            line=line.strip()
            if not line or line.startswith('#'):continue
            if line.startswith('['):
                if line=='[renewalparams]' and section=='top':section='parameters'
                elif line=='[[webroot_map]]' and section=='parameters' and not seen_map:section='webroots';seen_map=True
                else:raise CertificateError('Certificate renewal section is unsupported.')
                continue
            if '=' not in line:raise CertificateError('Certificate renewal metadata is invalid.')
            key,value=(part.strip() for part in line.split('=',1))
            if section=='webroots':
                if key in webroots:raise CertificateError('Duplicate renewal webroot mapping.')
                webroots[key]=scalar(value);continue
            key=key.lower().replace('-','_')
            if 'hook' in key or key in values:raise CertificateError('Certificate renewal hooks or duplicate settings are forbidden.')
            values[key]=scalar(value)
        challenge=str(self.paths['challenge_root'])
        if webroots!={hostname:challenge}:raise CertificateError('Renewal webroot mapping differs from the owned hostname and challenge directory.')
        if 'webroot_path' in values and scalar(values['webroot_path'].removesuffix(',').strip())!=challenge:raise CertificateError('Renewal challenge directory changed.')
        expected={'server':self.config['server'],'account':self.config['account'],'authenticator':'webroot','archive_dir':str(archive)}
        expected.update({role:str(live/(role+'.pem')) for role in ROLES})
        if any(values.get(k)!=v for k,v in expected.items()) or values.get('installer','None') not in ('None','none',''):raise CertificateError('Certificate lineage policy changed.')

    def _verify(self,files,hostname,*,allow_expired=False):
        try:
            leaf=x509.load_pem_x509_certificate(files['cert']);chain=x509.load_pem_x509_certificates(files['chain'])
            full=x509.load_pem_x509_certificates(files['fullchain']);key=serialization.load_pem_private_key(files['privkey'],password=None)
            if full!=[leaf,*chain]:raise ValueError()
            names=leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if len(names)!=1 or names.get_values_for_type(x509.DNSName)!=[hostname]:raise ValueError()
            if not isinstance(key,(rsa.RSAPrivateKey,ec.EllipticCurvePrivateKey)) or isinstance(key,rsa.RSAPrivateKey) and key.key_size<2048 or isinstance(key,ec.EllipticCurvePrivateKey) and key.key_size<256:raise ValueError()
            public=lambda k:k.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
            if public(key.public_key())!=public(leaf.public_key()):raise ValueError()
            now=datetime.datetime.now(datetime.timezone.utc)
            if now<leaf.not_valid_before_utc:raise ValueError()
            verification_time=now
            if now>=leaf.not_valid_after_utc:
                if not allow_expired:raise ValueError()
                verification_time=leaf.not_valid_after_utc-datetime.timedelta(seconds=1)
            roots=x509.load_pem_x509_certificates(self._read(self.paths['ca_bundle']))
            if not roots or any(not root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca for root in roots):raise ValueError()
            PolicyBuilder().store(Store(roots)).time(verification_time).build_server_verifier(x509.DNSName(hostname)).verify(leaf,chain)
            return {'fingerprint':leaf.fingerprint(hashes.SHA256()).hex(),'serial':format(leaf.serial_number,'x'),'sans':[hostname],
                    'not_before':leaf.not_valid_before_utc.timestamp(),'not_after':leaf.not_valid_after_utc.timestamp()}
        except Exception:raise CertificateError('Certificate identity, key, trust chain or validity is unverified.') from None

    def _commit(self,route,journal,files,metadata,authorize):
        generation=_digest({role:hashlib.sha256(data).hexdigest() for role,data in files.items()})
        parent=self.paths['generations_dir']/route
        if not parent.exists():parent.mkdir(mode=0o700)
        self._safe(parent,directory=True);target=parent/generation
        if not target.exists():
            temporary=Path(tempfile.mkdtemp(prefix='.generation-',dir=parent))
            try:
                for role,data in files.items():self._atomic(temporary/(role+'.pem'),data,0o400)
                os.rename(temporary,target)
                fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
            finally:
                if temporary.exists():
                    for item in temporary.iterdir():item.unlink()
                    temporary.rmdir()
        actual={role:self._read(target/(role+'.pem'),private=role=='privkey') for role in ROLES}
        if actual!=files:raise CertificateError('Immutable certificate generation changed.')
        authorize()
        evidence={'route_id':route,'operation_id':journal['operation_id'],'generation':generation,**metadata,'observed_at':time.time(),'status':'verified'}
        generations=journal.setdefault('generations',{})
        if generation in generations and generations[generation]!=metadata:raise CertificateError('Certificate generation ownership changed.')
        generations[generation]=metadata
        journal.update(state='verified',evidence=evidence);self._save(route,journal)
        return evidence

    def inspect(self,route_id,operation_id,hostname,*,authorize):
        name=self._identity(route_id,operation_id,hostname);authorize();journal=self._journal(route_id)
        if journal is None:return {'route_id':route_id,'operation_id':operation_id,'status':'absent'}
        if journal.get('operation_id')!=operation_id or journal.get('hostname')!=hostname or journal.get('policy_digest')!=self.policy_digest or journal.get('name')!=name:raise CertificateError('Certificate operation ownership changed.')
        try:
            files=self._lineage(name,hostname);metadata=self._verify(files,hostname)
            if journal.get('state')!='verified' and metadata['fingerprint']==journal.get('previous_fingerprint') and not journal.get('command_succeeded'):raise CertificateError('Renewal completion is unverified.')
        except (OSError,ValueError,UnicodeError):
            return {'route_id':route_id,'operation_id':operation_id,'status':'reconciliation-required'}
        return self._commit(route_id,journal,files,metadata,authorize)

    def request(self,route_id,operation_id,hostname,*,authorize,renew=False,retry=False):
        name=self._identity(route_id,operation_id,hostname);authorize();journal=self._journal(route_id)
        if journal:
            observed=self.inspect(route_id,operation_id,hostname,authorize=authorize)
            if observed['status']=='verified' and not renew:return observed
            # Valid output can reconcile a lost reply, but cannot prove its child
            # process exited. Never overlap another command with an unknown child.
            if journal.get('process_ended') is not True:
                return {'route_id':route_id,'operation_id':operation_id,'status':'reconciliation-required'}
            if renew and observed['status']!='verified':
                # Expiry removes serving readiness, not ownership. Permit renewal
                # only of exact previously journaled bytes with historical trust.
                try:files=self._lineage(name,hostname);metadata=self._verify(files,hostname,allow_expired=True)
                except (OSError,ValueError,UnicodeError):return observed
                generation=_digest({role:hashlib.sha256(data).hexdigest() for role,data in files.items()})
                if (journal.get('state')!='verified' or journal.get('generations',{}).get(generation)!=metadata
                        or metadata['not_after']>time.time()):return observed
            elif observed['status']!='verified' and not (retry and time.time()-journal['started']>=300):return observed
        else:
            for path in (self.paths['config_dir']/'live'/name,self.paths['config_dir']/'archive'/name,self.paths['config_dir']/'renewal'/(name+'.conf')):
                if os.path.lexists(path):raise CertificateError('Refusing an unowned preexisting certificate lineage.')
        renewal=self.paths['config_dir']/'renewal'/(name+'.conf')
        if os.path.lexists(renewal):self._renewal(name,hostname)
        # Fixed explicit empty configuration prevents implicit cli.ini hooks/plugins.
        cli=self.paths['state_dir']/'certbot-empty.ini'
        if os.path.lexists(cli):
            if self._read(cli)!=b'':raise CertificateError('Certificate CLI policy changed.')
        else:self._atomic(cli,b'')
        version=self.executor([str(self.paths['executable']),'--version'],timeout=5,env=self.environment)
        if version.strip()!=('certbot '+self.config['expected_version']).encode():raise CertificateError('Certbot version does not match the installed policy.')
        prior=journal.get('evidence',{}).get('fingerprint') if journal else None
        generations=journal.get('generations',{}) if journal else {}
        journal={'operation_id':operation_id,'hostname':hostname,'name':name,'policy_digest':self.policy_digest,'previous_fingerprint':prior,'attempt':journal.get('attempt',0)+1 if journal else 1,'started':time.time(),'state':'issuance-intent','process_ended':False,'command_succeeded':False,'generations':generations}
        authorize();self._save(route_id,journal)
        argv=[str(self.paths['executable']),'renew' if renew else 'certonly','--non-interactive','--no-directory-hooks','--config',str(cli),'--cert-name',name,'--server',self.config['server'],'--account',self.config['account'],'--agree-tos','--email',self.config['email']]
        for flag,key in (('--config-dir','config_dir'),('--work-dir','work_dir'),('--logs-dir','logs_dir')):argv.extend([flag,str(self.paths[key])])
        argv.extend(['--webroot','--webroot-path',str(self.paths['challenge_root'])])
        if not renew:argv.extend(['-d',hostname])
        authorize()
        try:self.executor(argv,timeout=self.config['timeout'],env=self.environment);journal.update(process_ended=True,command_succeeded=True)
        except CommandError as error:journal['process_ended']=error.ended
        except Exception:pass  # Lost replies are uncertain; inspect before any further command.
        self._save(route_id,journal)
        return self.inspect(route_id,operation_id,hostname,authorize=authorize)

    def generation_files(self,route_id,generation):
        if not isinstance(route_id,str) or not ID.fullmatch(route_id) or not isinstance(generation,str) or not re.fullmatch('[a-f0-9]{64}',generation):raise CertificateError('Invalid certificate generation identity.')
        journal=self._journal(route_id)
        if not journal or journal.get('policy_digest')!=self.policy_digest:raise CertificateError('Certificate generation ownership is unavailable.')
        target=self.paths['generations_dir']/route_id/generation;self._safe(target,directory=True)
        files={role:self._read(target/(role+'.pem'),private=role=='privkey') for role in ROLES}
        if _digest({role:hashlib.sha256(data).hexdigest() for role,data in files.items()})!=generation:raise CertificateError('Immutable certificate generation changed.')
        metadata=self._verify(files,journal['hostname'])
        if journal.get('generations',{}).get(generation)!=metadata:raise CertificateError('Certificate generation is not journal-owned.')
        return {'route_id':route_id,'hostname':journal['hostname'],'generation':generation,**metadata,**{role:str(target/(role+'.pem')) for role in ROLES}}
