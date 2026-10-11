"""Real local Git and Docker build-only acceptance; no deployed service mutation."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import artifact_publication
import build_profiles
import job_target
import managed_services
import registry
import worker
from store import connect, enqueue
from live_adoption import command, peers as raw_peers


def peers(docker):
    rows = raw_peers(docker)
    for row in rows.values(): row['Mounts'] = sorted(row['Mounts'], key=lambda item:(item['Type'],item['Destination'],item.get('Source','')))
    return rows


def main():
    docker = shutil.which('docker')
    command([docker,'image','inspect','python:3.13-slim'])
    baseline = peers(docker)
    runtime = ROOT/'.runtime'; runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='artifact-build-',dir=runtime) as directory:
        root = Path(directory); source = root/'repository'; source.mkdir()
        work = root/'work'; work.mkdir(); state = root/'worker-state'; state.mkdir()
        command(['git','init','--initial-branch=main',str(source)])
        (source/'Dockerfile').write_text('FROM python:3.13-slim\nCOPY app.py /app.py\n')
        (source/'app.py').write_text("def answer(): return 'verified-local-build'\n")
        command(['git','-C',str(source),'add','Dockerfile','app.py'])
        command(['git','-C',str(source),'-c','user.name=Local fixture','-c','user.email=fixture@invalid','commit','-m','Synthetic build fixture'])
        sha = command(['git','-C',str(source),'rev-parse','HEAD']).strip()
        db = connect(root/'state.sqlite'); image_ids = []
        try:
            registry.import_legacy(db,{},[])
            registry.create_project(db,'deploy-monitor',{'repository':'TPCT/deploy-monitor','repository_id':12})
            values = {'dockerfile':'Dockerfile','context':'.','target':None,
                      'test_command':['python','-c',"import sys;sys.path.insert(0,'/');import app;assert app.answer()=='verified-local-build'"],
                      'command':['python','/app.py'],'healthcheck':['python','-c','import sys;sys.exit(0)'],'user':'65534:65534'}
            saved = build_profiles.save(db,'deploy-monitor','development',1,1,0,values)
            pinned = build_profiles.get_private(db,'deploy-monitor','development')
            target = job_target.capture('deploy-monitor','development','TPCT/deploy-monitor',12,'main',sha,'validate',
                                        pinned['source_revision'],pinned['recipe'],project_revision=saved['project_revision'],environment_revision=saved['environment_revision'])
            job_id,_ = enqueue(db,'local-artifact-build','deploy-monitor',sha,True,'main','validate','fixture',target_factory=lambda db:target)
            job = dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
            project = worker.project_for_job(db,job)
            real_run = worker.run
            def run(argv,**kwargs):
                if argv[:2]==['git','clone']:
                    argv=list(argv);argv[-2]=str(source)
                return real_run(argv,**kwargs)
            with patch.object(worker,'DOCKER',docker),patch.object(worker,'ARTIFACT_WORK',work),patch.object(worker,'ARTIFACT_BUILD_NETWORK','none'),patch.object(worker,'STATE',state),patch.object(worker,'run',side_effect=run),patch.object(worker,'containers',side_effect=AssertionError('Build-only must not inspect runtime containers')),patch.object(worker,'migration_policy',side_effect=AssertionError('Build-only must not inspect migration policy')):
                worker.deploy(db,job,project,validate_only=True)
            updated = dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
            assert updated['status']=='validated',updated['detail']
            artifacts = managed_services.catalog(db,'deploy-monitor')
            assert len(artifacts)==1,artifacts
            private = managed_services.artifact_for_service(db,'deploy-monitor',artifacts[0]['artifact_id'])
            image_ids.append(private['image'])
            actual = json.loads(command([docker,'image','inspect',private['image']]))[0]
            assert actual['Config']['Labels']['org.opencontainers.image.revision']==sha
            assert actual['Config']['Labels']['net.shiftcodes.job']==str(job_id)
            assert private['source_sha']==sha and private['source_job']==job_id
            assert not artifact_publication.pending(db,job_id)
            assert registry.get_environment(db,'deploy-monitor','development')['recipe_revision'] is None
            assert db.execute('SELECT COUNT(*) FROM managed_services').fetchone()[0]==0
            assert not {'compose_project','compose_files','env_file','http_checks'} & set(pinned['recipe'])
            db.close();db=connect(root/'state.sqlite')
            assert managed_services.artifact_for_service(db,'deploy-monitor',private['id'])['image']==private['image']
            report={'source_sha':sha,'status':updated['status'],'artifact_image':private['image'],
                    'profile_only_no_runtime_recipe':True,'no_containers_provisioned':True,'publication_reconnect':'passed'}
        finally:
            # Remove only images labeled by this fixture's source SHA; the worker
            # test containers remove themselves and use immutable-ID cleanup.
            identifiers=command([docker,'image','ls','-q','--no-trunc','--filter','label=org.opencontainers.image.revision='+sha]).split()
            for identifier in set(identifiers): command([docker,'image','rm',identifier])
            db.close()
    assert peers(docker)==baseline,'Existing product peers changed'
    report['existing_peers_unchanged']=True;report['fixture_cleanup']=True
    (runtime/'artifact-build-last.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
