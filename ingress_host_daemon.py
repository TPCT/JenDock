"""Administrative entry point for the restricted ingress host helper.

Never import deployment configuration from a request. No service is installed or
started by importing this module. SQLite and all effect callbacks share one thread.
"""
import argparse
import dataclasses
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
import re
import stat
import ingress_ports

import application_operations as ops
import mcp_credentials
from ingress_apache_executor import ApacheExecutor, bounded_run
from ingress_host import HostAdapter
from ingress_host_grants import DockerGatewayVerifier, HostGrantIssuer
from ingress_host_service import HostService
from ingress_host_transport import HostServer

FIELDS = {'schema_version','database','database_uid','credentials_file','policy_file','socket_path',
          'allowed_uids','socket_group_gid','docker_executable','docker_socket','gateway_ports','apache','timeout_seconds'}
APACHE_FIELDS = {'executable','config','pid_file','listener','expected_argv','source_roots','server_root'}


def trusted_file(path, owner_uid, *, ancestor_uid=0):
    import stat
    if not isinstance(path,str) or not Path(path).is_absolute() or '..' in Path(path).parts:
        raise ValueError('Configured file must have an absolute normalized path')
    target=Path(path)
    for parent in target.parents:
        info=parent.lstat()
        sticky=parent==Path('/tmp') and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o1777
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,ancestor_uid,owner_uid) or info.st_mode&0o022 and not sticky:
            raise ValueError('Configured file has an unsafe ancestor')
    info=target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=owner_uid or info.st_mode&0o022 or info.st_nlink!=1:
        raise ValueError('Configured file ownership or type is unsafe')
    return target


def load_config(path, *, owner_uid=0):
    source=trusted_file(str(path),owner_uid,ancestor_uid=owner_uid)
    with source.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('Host configuration exceeds limit')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate host configuration field')
            result[key]=value
        return result
    value=json.loads(raw,object_pairs_hook=pairs)
    if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1,2) or set(value)!=(FIELDS if value['schema_version']==1 else FIELDS|{'tls'}):
        raise ValueError('Invalid host configuration schema')
    if value['schema_version'] == 2:
        tls = value['tls']
        if not isinstance(tls, dict) or set(tls) != {'listener', 'observation_listener', 'ca_file', 'certificates'}:
            raise ValueError('Invalid administrative TLS configuration')
        for key in ('listener', 'observation_listener'):
            item=tls[key]
            if not isinstance(item, dict) or set(item)!={'address','port'} or type(item['port']) is not int or not 1<=item['port']<=65535:
                raise ValueError('Invalid TLS listener')
        if tls['ca_file'] is not None: trusted_file(tls['ca_file'], owner_uid, ancestor_uid=owner_uid)
    if type(value['database_uid']) is not int or value['database_uid']<0:raise ValueError('Explicit database owner is required')
    for field in ('credentials_file','policy_file'):
        trusted_file(value[field],owner_uid,ancestor_uid=owner_uid)
    binary_owner=Path(value['docker_executable']).lstat().st_uid
    if binary_owner not in (0,owner_uid):raise ValueError('Untrusted Docker executable owner')
    trusted_file(value['docker_executable'],binary_owner,ancestor_uid=owner_uid)
    trusted_file(value['database'],value['database_uid'],ancestor_uid=owner_uid)
    if not isinstance(value['apache'],dict) or set(value['apache'])!=APACHE_FIELDS:raise ValueError('Invalid Apache configuration schema')
    apache=value['apache']
    if not isinstance(apache['listener'],dict) or set(apache['listener'])!={'address','port'}:raise ValueError('Invalid Apache listener')
    if not isinstance(apache['expected_argv'],list) or not isinstance(apache['source_roots'],list) or not apache['source_roots']:
        raise ValueError('Explicit Apache arguments and source roots are required')
    if not isinstance(value['allowed_uids'],list) or not value['allowed_uids'] or len(value['allowed_uids'])>64 or len(set(value['allowed_uids']))!=len(value['allowed_uids']):
        raise ValueError('Explicit bounded peer UID allowlist is required')
    if any(type(uid) is not int or uid<0 for uid in value['allowed_uids']):raise ValueError('Invalid peer UID')
    if value['socket_group_gid'] is not None and (type(value['socket_group_gid']) is not int or value['socket_group_gid']<0):raise ValueError('Invalid socket group')
    timeout=value['timeout_seconds']
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not 0<timeout<=60:raise ValueError('Invalid request deadline')
    ports=value['gateway_ports']
    if not isinstance(ports,dict) or set(ports)!={'first','last'} or any(type(ports[k]) is not int for k in ports) or not 1024<=ports['first']<=ports['last']<=65535 or ports['last']-ports['first']>4095:
        raise ValueError('Invalid bounded administrative gateway port range')
    docker_socket=Path(value['docker_socket'])
    if not docker_socket.is_absolute() or '..' in docker_socket.parts:
        raise ValueError('Docker endpoint must be an absolute Unix socket path')
    socket_path=Path(value['socket_path'])
    if not socket_path.is_absolute() or '..' in socket_path.parts:raise ValueError('Invalid socket path')
    return value


class Assembly:
    def __init__(self, config, *, owner_uid=0):
        self.config=config;self.owner_uid=owner_uid;self.db=None;self.server=None
        self.previous_credentials=os.environ.get('CREDENTIALS_FILE')
        self.credentials_installed=False
        self.previous_docker_host=os.environ.get("DOCKER_HOST")
        try:
            # recheck_credential uses the same trusted live owner configuration.
            os.environ['CREDENTIALS_FILE']=config['credentials_file'];self.credentials_installed=True
            self._owner(mcp_credentials.validate_owner_configuration())
            endpoint=Path(config['docker_socket'])
            self._socket_endpoint(endpoint)
            os.environ['DOCKER_HOST']='unix://'+str(endpoint)
            apache=config['apache']
            tls=config.get('tls')
            tls_options={'tls_listener':(tls['observation_listener']['address'],tls['observation_listener']['port']),'tls_ca_file':tls['ca_file']} if tls else {}
            self.executor=ApacheExecutor(**{k:apache[k] for k in APACHE_FIELDS-{'listener'}},
                listener=(apache['listener']['address'],apache['listener']['port']),owner_uid=owner_uid,timeout=config['timeout_seconds'],**tls_options)
            certificates = None
            if tls:
                from ingress_certificates import CertificateManager
                certificates=CertificateManager(tls['certificates'],owner_uid=owner_uid)
            self.adapter=HostAdapter(config['policy_file'],self.executor,owner_uid=owner_uid,
                challenge_root=certificates.paths['challenge_root'] if certificates else None)
            policy=self.adapter._policy()
            self.adapter._safe(Path(config['socket_path']).parent,directory=True)
            if policy['listener']!=apache['listener']:raise ValueError('Policy and Apache observation listeners must match')
            db_path=trusted_file(config['database'],config['database_uid'],ancestor_uid=owner_uid)
            self.db=sqlite3.connect(db_path.as_uri()+'?mode=rw',uri=True,timeout=15,isolation_level=None)
            self.db.row_factory=sqlite3.Row
            # Existing deployment schema only: never bootstrap a new database.
            for table in ('ingress_routes','ingress_definitions','ingress_operations','registry_projects','registry_environments'):
                if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                    raise ValueError('Host database is not an initialized product database')
            self.issuer=HostGrantIssuer(self.adapter,self.db,verify_gateway=DockerGatewayVerifier(config['docker_executable']),resolve_principal=self.resolve_principal)
            tls_adapter=None
            if tls:
                from ingress_host_tls import HostTLSAdapter
                tls_adapter=HostTLSAdapter(self.adapter,certificates,listener=tls['listener'])
            self.service=HostService(self.adapter,self.issuer,available_ports=self.available_ports,tls=tls_adapter)
            self.server=HostServer(config['socket_path'],self.service,allowed_uids=config['allowed_uids'],owner_uid=owner_uid,
                                   group_gid=config['socket_group_gid'],timeout=config['timeout_seconds'])
        except Exception:
            self.close();raise

    def _socket_endpoint(self, path):
        for parent in path.parents:
            info=parent.lstat()
            sticky=parent==Path('/tmp') and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o1777
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0,self.owner_uid) or info.st_mode&0o022 and not sticky:
                raise ValueError('Unsafe Docker socket ancestor')
        info=path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid not in (0,self.owner_uid) or info.st_mode&0o007:
            raise ValueError('Unsafe Docker endpoint')

    def available_ports(self):
        # Host-side observation only, never a claim of an OS lease.
        self._socket_endpoint(Path(self.config['docker_socket']))
        binary=self.config['docker_executable']
        binary_owner=Path(binary).lstat().st_uid
        if binary_owner not in (0,self.owner_uid):raise ValueError('Untrusted Docker executable owner')
        trusted_file(binary,binary_owner,ancestor_uid=self.owner_uid)
        deadline=time.monotonic()+self.config['timeout_seconds']
        environment={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','DOCKER_HOST':'unix://'+self.config['docker_socket']}
        def run(args):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('Docker port observation deadline exceeded')
            return bounded_run([binary,*args],remaining,env=environment)
        identifiers=run(['ps','--all','--quiet','--no-trunc']).split()
        if len(identifiers)>256 or any(not re.fullmatch('[a-f0-9]{64}',item) for item in identifiers):
            raise ValueError('Docker inventory exceeds bounded identity contract')
        occupied=set()
        for offset in range(0,len(identifiers),16):
            lines=run(['inspect','--format','{"configured":{{json .HostConfig.PortBindings}},"runtime":{{json .NetworkSettings.Ports}}}',*identifiers[offset:offset+16]]).splitlines()
            if len(lines)!=len(identifiers[offset:offset+16]):raise ValueError('Docker port inventory is incomplete')
            for line in lines:
                document=json.loads(line)
                if not isinstance(document,dict) or set(document)!={'configured','runtime'}:raise ValueError('Invalid Docker port inventory')
                for kind,bindings in document.items():
                    if bindings is None:continue
                    if not isinstance(bindings,dict):raise ValueError('Invalid Docker port binding evidence')
                    for entries in bindings.values():
                        if entries is None:continue
                        if not isinstance(entries,list):raise ValueError('Invalid Docker published ports')
                        for entry in entries:
                            if not isinstance(entry,dict) or set(entry)!={'HostIp','HostPort'} or not isinstance(entry['HostPort'],str):
                                raise ValueError('Invalid Docker published port identity')
                            if kind=='configured' and entry['HostPort'] in ('','0'):continue
                            if not re.fullmatch('[0-9]{1,5}',entry['HostPort']):raise ValueError('Invalid Docker host port')
                            port=int(entry['HostPort'])
                            if not 1<=port<=65535:raise ValueError('Invalid Docker published port')
                            occupied.add(port)
        ports=self.config['gateway_ports']
        available=[]
        for port in range(ports['first'],ports['last']+1):
            if time.monotonic()>=deadline:raise ValueError('Host port observation deadline exceeded')
            if port not in occupied and ingress_ports.loopback_available(port):available.append(port)
        return {'ports':available,'observed':time.time()}

    def _owner(self, subject):
        trusted_file(self.config['credentials_file'],self.owner_uid,ancestor_uid=self.owner_uid)
        owner=mcp_credentials.configured_owner(subject)
        if owner is None:raise ValueError('Original console owner is no longer authorized')
        # Console already supports domain creation; do not widen issued MCP scopes.
        return dataclasses.replace(owner,scopes=owner.scopes|{'domains:create'})

    def resolve_principal(self, db, operation):
        owner=self._owner(operation['subject'])
        credential=operation['credential_id']
        if credential is None:return owner
        principal=mcp_credentials.principal_for_credential(db,credential)
        if principal.subject_id!=owner.subject_id:raise ValueError('Credential owner changed')
        return principal

    def close(self):
        try:
            if self.server is not None:self.server.close()
        finally:
            self.server=None
            try:
                if self.db is not None:self.db.close()
            finally:
                self.db=None
                if self.credentials_installed:
                    if self.previous_credentials is None:os.environ.pop('CREDENTIALS_FILE',None)
                    else:os.environ['CREDENTIALS_FILE']=self.previous_credentials
                    if self.previous_docker_host is None:os.environ.pop('DOCKER_HOST',None)
                    else:os.environ['DOCKER_HOST']=self.previous_docker_host
                    self.credentials_installed=False


def serve(config_path, *, stop_event=None, owner_uid=0, install_signals=True, check=False):
    config=load_config(config_path,owner_uid=owner_uid)
    stop_event=stop_event or threading.Event();previous={}
    assembly=Assembly(config,owner_uid=owner_uid)
    try:
        if check:return
        if install_signals:
            for signum in (signal.SIGTERM,signal.SIGINT):
                previous[signum]=signal.signal(signum,lambda *_:stop_event.set())
        assembly.server.open();assembly.server.socket.settimeout(.25)
        while not stop_event.is_set():assembly.server.serve_once()
    finally:
        assembly.close()
        for signum,handler in previous.items():signal.signal(signum,handler)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Restricted ingress host helper (administrative setup required)')
    parser.add_argument('--config',required=True)
    parser.add_argument('--check',action='store_true',help='Validate assembly without opening a socket or changing Apache')
    args=parser.parse_args(argv)
    try:serve(args.config,check=args.check)
    except Exception:
        print('Ingress host helper failed; verify trusted configuration and local administrative diagnostics.',file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
