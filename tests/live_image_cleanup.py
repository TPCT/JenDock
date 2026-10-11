"""Explicit disposable image cleanup acceptance; never prunes builder cache/volumes."""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import image_cleanup
import job_target
import managed_services
import registry
import worker
from store import connect,enqueue


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true')
    if not parser.parse_args().run:raise ValueError('Explicit --run required')
    docker=shutil.which('docker');real_run=subprocess.run
    def command(args,**kwargs):
        return subprocess.check_output([docker,*args],stderr=subprocess.DEVNULL,timeout=30,**kwargs).decode().strip()
    def images():return set(command(['image','ls','--all','--no-trunc','-q']).splitlines())
    def peers():
        ids=command(['ps','-aq','--no-trunc']).splitlines()
        if not ids:return {}
        return {r['Id']:{'image':r['Image'],'running':r['State']['Running'],'restarting':r['State']['Restarting'],'restarts':r['RestartCount']} for r in json.loads(command(['inspect',*ids]))}
    before_images=images();before_peers=peers();owned=[];attempts=[]
    identifier=int(uuid.uuid4().hex[:15],16);suffix=uuid.uuid4().hex
    assert not command(['image','ls','-q','--filter','label=net.shiftcodes.job='+str(identifier)])
    try:
        with tempfile.TemporaryDirectory(prefix='deploy-monitor-cleanup-') as temporary:
            root=Path(temporary);path=root/'state.sqlite';db=connect(path)
            try:
                archive=io.BytesIO()
                with tarfile.open(fileobj=archive,mode='w') as tar:
                    content=b'disposable deploy-monitor cleanup fixture\n';info=tarfile.TarInfo('fixture.txt');info.size=len(content);tar.addfile(info,io.BytesIO(content))
                for name in ('protected','unused'):
                    image=command(['import','--change','LABEL com.docker.compose.project=deploy-monitor',
                        '--change','LABEL net.shiftcodes.job='+str(identifier),'--change','LABEL net.shiftcodes.cleanup-fixture='+suffix+'-'+name,
                        '-'],input=archive.getvalue())
                    assert image.startswith('sha256:') and image not in before_images
                    owned.append(image)
                    metadata=json.loads(command(['image','inspect',image]))[0]
                    assert metadata['Config']['Labels']['net.shiftcodes.job']==str(identifier), 'Imported image label mismatch'
                    assert not metadata.get('RepoTags'), 'Fixture image must remain untagged'
                protected,unused=owned
                print(json.dumps({'fixture_candidate_count':len(command(['image','ls','--all','--no-trunc','--quiet','--filter','label=net.shiftcodes.job='+str(identifier)]).splitlines())}),flush=True)
                recipe={'compose_project':'fixture','compose_files':['/fixture/compose.yml'],'services':['web'],
                    'images':[{'name':'app'}],'service_profiles':{'application-v1':{'revision':1,'image_name':'app','command':['app','serve'],'healthcheck':['app','health'],'user':'65534:65534'}}}
                registry.import_legacy(db,{'fixture':{'repository':'TPCT/fixture','repository_id':12,'branch':'main','recipe':recipe}},[])
                target=job_target.capture('fixture','legacy-unclassified','TPCT/fixture',12,'main','b'*40,'validate',1,recipe,project_revision=1)
                source,_=enqueue(db,'source-'+suffix,'fixture','b'*40,True,'main','validate',target_factory=lambda connection:target)
                db.execute("UPDATE jobs SET status='validated' WHERE id=?",(source,))
                managed_services.publish_artifact(db,source,'application-v1',protected)
                assert protected in managed_services.protected_images(db)
                db.execute("INSERT INTO jobs(id,delivery,project,sha,status,updated,created) VALUES(?,?,?,?,'succeeded',?,?)",(identifier,suffix,'fixture','c'*40,time.time(),time.time()))
                def effect(args,**kwargs):
                    if args[:3]==[docker,'image','rm']:
                        assert args==[docker,'image','rm',unused],'Unexpected deletion target'
                        attempts.append(unused)
                        if len(attempts)==1:return subprocess.CompletedProcess(args,1)
                    elif args and args[0]==docker:
                        assert 'prune' not in args and 'rm' not in args and 'volume' not in args,'Unexpected destructive command'
                    return real_run(args,**kwargs)
                with patch.object(worker,'DOCKER',docker),patch.object(worker,'STATE',root),patch.object(worker.control,'JOURNAL',root/'environment.json'),patch.object(worker.subprocess,'run',side_effect=effect):
                    # Simulate restart after deployment committed but before finally enqueued.
                    worker.process_cleanup(db,job_id=identifier,kind='job-images')
                    pending=dict(db.execute("SELECT * FROM image_cleanup WHERE job_id=? AND kind='job-images'",(identifier,)).fetchone())
                    assert pending['state']=='pending' and pending['attempts']==1, {'state':pending['state'],'attempts':pending['attempts'],'removals':len(attempts)}
                    assert db.execute('SELECT status FROM jobs WHERE id=?',(identifier,)).fetchone()[0]=='succeeded'
                    assert {protected,unused}<=images()
                    db.close();db=connect(path)
                    # Persisted next-attempt is honored, then wall clock advances to it.
                    assert not worker.process_cleanup(db,job_id=identifier,kind='job-images')
                    with patch.object(image_cleanup.time,'time',return_value=pending['next_attempt']+1):
                        assert worker.process_cleanup(db,job_id=identifier,kind='job-images')
                    done=db.execute("SELECT state,attempts FROM image_cleanup WHERE job_id=? AND kind='job-images'",(identifier,)).fetchone()
                    assert tuple(done)==('succeeded',2)
                    assert unused not in images() and protected in images()
                    assert db.execute('SELECT status FROM jobs WHERE id=?',(identifier,)).fetchone()[0]=='succeeded'
                    assert not worker.process_cleanup(db,job_id=identifier,kind='job-images')
                    assert len(attempts)==2
                print(json.dumps({'untagged_images':True,'durable_retry':True,'restart_reconciliation':True,'catalog_protected_image_retained':True,'unused_image_removed':True,'deployment_still_succeeded':True,'replay_no_extra_removal':True,'builder_cache_prune_executed':False}),flush=True)
            finally:db.close()
    finally:
        current=images()
        for image in owned:
            if image in current:
                result=real_run([docker,'image','rm',image],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
                assert result.returncode==0,'Exact fixture image cleanup failed'
        after_images=images();after_peers=peers()
        assert not set(owned)&after_images
        assert before_images<=after_images,'Preexisting image disappeared'
        assert set(before_peers)<=set(after_peers),'Preexisting container disappeared'
        assert all(after_peers[k]['image']==v['image'] and (not v['running'] or after_peers[k]['running']) for k,v in before_peers.items())
        transitions={k:{'before':v,'after':after_peers[k]} for k,v in before_peers.items() if v!=after_peers[k]}
        if transitions:print(json.dumps({'peer_transitions':transitions}),flush=True)
        print(json.dumps({'cleanup':True,'fixture_images_remaining':[],'preexisting_images_and_containers_preserved':True}),flush=True)

if __name__=='__main__':main()
