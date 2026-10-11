"""Bounded worker-private Docker metadata; no exec or credential discovery."""
import json
import math
import os
from pathlib import PurePosixPath
import re
import stat
import time
from database_helper import exchange

LABELS = ('com.docker.compose.project','com.docker.compose.service','com.docker.compose.project.config_files','com.docker.compose.project.working_dir')
CONTAINER_FORMAT = ('{"Id":{{json .Id}},"Image":{{json .Image}},'
    '"State":{"Running":{{json .State.Running}},"Paused":{{json .State.Paused}},"Restarting":{{json .State.Restarting}},"StartedAt":{{json .State.StartedAt}}},'
    '"HostConfig":{"NetworkMode":{{json .HostConfig.NetworkMode}}},'
    '"Config":{"Labels":{'+','.join(json.dumps(k)+':{{json (index .Config.Labels '+json.dumps(k)+')}}' for k in LABELS)+'}},'
    '"Mounts":{{json .Mounts}},"NetworkSettings":{"Networks":{{json .NetworkSettings.Networks}}}}')
VOLUME_FORMAT = '{"Name":{{json .Name}},"Driver":{{json .Driver}},"Mountpoint":{{json .Mountpoint}},"CreatedAt":{{json .CreatedAt}},"Options":{{json .Options}}}'


class InventoryError(ValueError):
    def __init__(self):super().__init__('Database inventory is unavailable or exceeds its configured bounds.')


def _path(value):
    return isinstance(value,str) and value.startswith('/') and not value.startswith('//') and str(PurePosixPath(value))==value and '..' not in PurePosixPath(value).parts and not any(ord(c)<32 for c in value)


def _bind_proof(source, roots):
    if not _path(source) or not any(source==root or source.startswith(root+'/') for root in roots):return None
    fd=None
    try:
        fd=os.open('/',os.O_PATH|os.O_DIRECTORY)
        for part in PurePosixPath(source).parts[1:]:
            child=os.open(part,os.O_PATH|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
            if stat.S_ISLNK(os.fstat(fd).st_mode):return None
        info=os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):return None
        return {'realpath':source,'device':info.st_dev,'inode':info.st_ino}
    except OSError:return None
    finally:
        if fd is not None:os.close(fd)


class DockerInventory:
    def __init__(self, *, docker, socket, bind_roots=('/opt/docker-engine/projects',), timeout=20, max_bytes=8*1024*1024, runner=exchange):
        if (not _path(docker) or not isinstance(socket,str) or not socket.startswith('unix://') or not _path(socket[7:])
                or not isinstance(bind_roots,(tuple,list)) or len(bind_roots)>32 or any(not _path(root) or root=='/' for root in bind_roots)
                or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=30
                or type(max_bytes) is not int or not 1024<=max_bytes<=16*1024*1024):raise InventoryError()
        self.docker=docker;self.env={'PATH':'/usr/bin:/bin','DOCKER_HOST':socket}
        self.roots=tuple(bind_roots);self.timeout=timeout;self.max_bytes=max_bytes;self.runner=runner

    def __call__(self):
        observed=time.time();deadline=time.monotonic()+self.timeout;remaining_bytes=self.max_bytes
        def run(args):
            nonlocal remaining_bytes
            remaining=deadline-time.monotonic()
            if remaining<=0 or remaining_bytes<=0:raise InventoryError()
            raw=self.runner([self.docker,*args],timeout=remaining,env=self.env,limit=remaining_bytes)
            if not isinstance(raw,bytes) or len(raw)>remaining_bytes or time.monotonic()>deadline:raise InventoryError()
            remaining_bytes-=len(raw)
            return raw.decode('utf-8')
        try:
            ids=run(['ps','-aq','--no-trunc']).splitlines()
            if len(ids)>1000 or len(ids)!=len(set(ids)) or any(not re.fullmatch('[a-f0-9]{64}',i) for i in ids):raise InventoryError()
            containers=[]
            for start in range(0,len(ids),50):
                batch=ids[start:start+50]
                rows=[json.loads(line) for line in run(['inspect','--type','container','--format',CONTAINER_FORMAT,*batch]).splitlines()]
                if len(rows)!=len(batch) or {row['Id'] for row in rows}!=set(batch):raise InventoryError()
                for row in rows:
                    labels=row['Config']['Labels']
                    # Rebuild even when an injected runner returns broader metadata.
                    containers.append({'Id':row['Id'],'Image':row['Image'],
                        'State':{k:row['State'][k] for k in ('Running','Paused','Restarting','StartedAt')},
                        'HostConfig':{'NetworkMode':row['HostConfig']['NetworkMode']},
                        'Config':{'Labels':{k:labels.get(k) for k in LABELS}},
                        'Mounts':[{k:m[k] for k in ('Type','Source','Destination','RW','Name') if k in m} for m in row['Mounts']],
                        'NetworkSettings':{'Networks':{name:{k:net.get(k) for k in ('NetworkID','EndpointID','IPAddress','Aliases')} for name,net in row['NetworkSettings']['Networks'].items()}}})
            names=sorted({m['Name'] for row in containers for m in row['Mounts'] if m['Type']=='volume'})
            if len(names)>1000 or any(not isinstance(n,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,254}',n) for n in names):raise InventoryError()
            volumes={}
            for start in range(0,len(names),50):
                batch=names[start:start+50]
                rows=[json.loads(line) for line in run(['volume','inspect','--format',VOLUME_FORMAT,*batch]).splitlines()]
                if len(rows)!=len(batch) or {row['Name'] for row in rows}!=set(batch):raise InventoryError()
                for row in rows:volumes[row['Name']]={k:row.get(k) for k in ('Name','Driver','Mountpoint','CreatedAt','Options')}
            sources={m['Source'] for row in containers for m in row['Mounts'] if m['Type']=='bind'}
            if len(sources)>1000:raise InventoryError()
            proofs={}
            for source in sources:
                if time.monotonic()>deadline:raise InventoryError()
                proof=_bind_proof(source,self.roots)
                if proof is not None:proofs[source]=proof
            return {'observed':observed,'containers':containers,'volumes':volumes,'bind_sources':proofs}
        except Exception:
            raise InventoryError() from None
