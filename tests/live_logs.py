"""Real disposable Docker log fixture inside the deploy-monitor Compose group."""
import json
import pathlib
import subprocess
import tempfile
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import container_logs


def run(args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)


def peers():
    ids=run(['docker','ps','-aq','--filter','label=com.docker.compose.project=deploy-monitor']).split()
    return {r['Id']:(r['State']['Status'],r['State']['StartedAt']) for r in json.loads(run(['docker','inspect',*ids]))} if ids else {}


before=peers()
with tempfile.TemporaryDirectory(prefix='monitor-logs-') as directory:
    path=pathlib.Path(directory)/'compose.json'
    path.write_text(json.dumps({'services':{'logs-check':{'image':'deploy-monitor-worker:local-current','network_mode':'none','read_only':True,'cap_drop':['ALL'],'entrypoint':['python','-u','-c','import sys; print("log-sentinel"); print("password=secret-sentinel",file=sys.stderr)']}}}))
    base=['docker','compose','-p','deploy-monitor','-f',str(path)]
    try:
        run(base+['up','-d','--no-build','--pull','never','logs-check'])
        cid=run(base+['ps','-aq','logs-check']).strip()
        run(['docker','wait',cid])
        text,truncated=container_logs.capture(['docker','logs','--timestamps','--tail','200',cid])
        safe=container_logs.redact(text,{})
        assert 'log-sentinel' in safe and 'secret-sentinel' not in safe and '[redacted]' in safe
        assert not truncated
        print(json.dumps({'passed':True,'real_docker_stdout_stderr':True,'redacted':True,'stopped_container_logs':True}))
    finally:
        run(base+['rm','-sf','logs-check'])
assert peers()==before, 'Existing project peers changed'
