"""Actual candidate-image verifier acceptance; not production or runtime readiness.

--build-only retains the one requested review image for a subsequent frozen verifier
run. Default builds, invokes the real verifier for queue/scheduler and cleans the
exact candidate image. PostgreSQL ownership/cleanup belongs entirely to the verifier.
"""
import argparse,hashlib,json,os,shutil,sqlite3,subprocess,sys,tempfile,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
BASE='sha256:bd9c8acfc81b73714965f4981560a9aab8b43309f903b7da586ef5661ae83c13'
POSTGRES='sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
TAG='deploy-monitor-workload-candidate:review'

def run(args,*,cwd=None,timeout=120):
    p=subprocess.run(args,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if p.returncode:raise RuntimeError('Candidate fixture command failed: '+args[0]+' (exit '+str(p.returncode)+').')
    return p.stdout.decode().strip()

def peers(docker):
    ids=run([docker,'ps','-aq','--no-trunc']).split()
    return {x['Id']:(x['Image'],x['State']['Running']) for x in json.loads(run([docker,'inspect',*ids]))} if ids else {}

def build(docker,tag=TAG):
    existing=subprocess.run([docker,'image','inspect',tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if existing:raise RuntimeError('Review image tag already exists; pass its immutable --image for verification instead of overwriting it.')
    run([docker,'image','inspect',BASE])
    nonce=uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='deploy-monitor-workload-context-') as directory:
        root=Path(directory);shutil.copytree(ROOT/'tests/workload_candidate',root,dirs_exist_ok=True)
        shutil.copytree(ROOT/'workload-instrumentation/vendor',root/'vendor')
        package=root/'vendor/shiftcodes/deploy-monitor-workloads';package.mkdir(parents=True)
        for name in ('src','config','database'):shutil.copytree(ROOT/'workload-instrumentation'/name,package/name)
        shutil.copy2(ROOT/'workload-instrumentation/composer.json',package/'composer.json')
        (root/'bootstrap/cache').mkdir();(root/'storage/framework/views').mkdir(parents=True);(root/'storage/logs').mkdir()
        run(['composer','dump-autoload','--no-scripts','--no-plugins','--no-interaction'],cwd=root)
        run([docker,'build','--network','none','--label','com.docker.compose.project=deploy-monitor','--label','net.shiftcodes.workload-candidate='+nonce,'-t',tag,str(root)],timeout=300)
    row=json.loads(run([docker,'image','inspect',tag]))[0]
    assert row['Config']['Labels']['net.shiftcodes.workload-candidate']==nonce
    return row['Id'],nonce

def document(image,role):
    import build_profiles,workload_package_verifier as package
    configuration={'connection':'database','queues':['default'],'timeout_seconds':60,'retry_after_seconds':90,'max_attempts':1,'sleep_seconds':1,'shutdown_grace_seconds':90} if role=='queue' else {'connection':'pgsql','timezone':'UTC','scope':'default'}
    template='laravel-database-queue-v1' if role=='queue' else 'laravel-scheduler-v1'
    profile=build_profiles.role_profile(template,{'framework_version':package.FRAMEWORK_VERSION,'instrumentation':{'version':package.PACKAGE_VERSION,'digest':package.PACKAGE_DIGEST},'configuration':configuration},'candidate','65534:65534')
    return {'project':'fixture','source_environment':'development','source_sha':'a'*40,'recipe_revision':1,'target_fingerprint':'b'*64,'source_job':1 if role=='queue' else 2,'image':image,'user':profile['user'],'workload':profile['workload'],'template_id':template,'template_revision':profile['revision'],'image_name':'candidate','command':profile['command'],'healthcheck':profile['healthcheck']}

def publication(db,verifier,image,docker):
    import artifact_publication,build_profiles,job_target,managed_services,registry
    from store import enqueue
    registry.import_legacy(db,{'fixture':{'repository':'TPCT/fixture','repository_id':12,'branch':'development','recipe':{'compose_project':'fixture','compose_files':['/fixture/compose.json'],'services':['app']}}},[])
    before=build_profiles.get(db,'fixture','development')
    roles={document(image,role)['template_id']:{key:document(image,role)['workload'][key] for key in ('framework_version','instrumentation','configuration')} for role in ('queue','scheduler')}
    values={'dockerfile':'Dockerfile','context':'.','target':None,'test_command':['php','artisan','--version'],'command':['php','artisan','--version'],'healthcheck':['php','artisan','--version'],'user':'65534:65534','role_profiles':roles}
    build_profiles.save(db,'fixture','development',before['project_revision'],before['environment_revision'],0,values)
    source=build_profiles.get_private(db,'fixture','development');recipe=source['recipe']
    current=build_profiles.get(db,'fixture','development')
    target=job_target.capture('fixture','development','TPCT/fixture',12,'development','a'*40,'validate',source['source_revision'],recipe,project_revision=current['project_revision'],environment_revision=current['environment_revision'])
    job,_=enqueue(db,'actual-role-publication','fixture','a'*40,True,'development','validate',target_factory=lambda db:target)
    artifact_publication.record(db,job,recipe,{recipe['images'][0]['name']:image},status='validated')
    def inspect(ref):
        assert ref==image
        return json.loads(run([docker,'image','inspect',ref]))[0]['Id']
    assert artifact_publication.process(db,inspect,job,verify_role=lambda doc:verifier.verify(db,doc))
    rows=db.execute('SELECT id FROM service_artifacts WHERE job_id=?',(job,)).fetchall()
    assert len(rows)==3
    observed=[]
    for row in rows:
        artifact=managed_services.artifact_for_service(db,'fixture',row['id'])
        if 'workload' in artifact:
            capability=managed_services.role_capability(db,artifact)
            assert capability['artifact_verified'] and not capability['operational']
            observed.append(artifact['workload']['role'])
    assert sorted(observed)==['queue','scheduler']
    assert artifact_publication.process(db,inspect,job,verify_role=lambda doc:(_ for _ in ()).throw(AssertionError('Completed publication reexecuted verifier')))
    print(json.dumps({'actual_publication':True,'roles':sorted(observed),'catalog_artifact_verified':True,'runtime_operational':False,'publication_replay_no_executor':True}),flush=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--build-only',action='store_true');parser.add_argument('--image');parser.add_argument('--publication',action='store_true');parser.add_argument('--evidence',type=Path);parser.add_argument('--docker',default=shutil.which('docker'));parser.add_argument('--socket',default='unix:///var/run/docker.sock');args=parser.parse_args()
    source_files=('workload_artifact_verifier.py','workload_package_verifier.py','artifact_publication.py','managed_services.py','build_profiles.py')
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_files}
    baseline=peers(args.docker);image=args.image;nonce=None;verified=False;state=None
    try:
        if not image:image,nonce=build(args.docker)
        if args.build_only:
            print(json.dumps({'candidate_image':image,'tag':TAG,'actual_laravel':'12.69.3','verifier_executed':False}),flush=True);return
        from workload_artifact_verifier import WorkloadArtifactVerifier
        state_parent=Path(tempfile.mkdtemp(prefix='deploy-monitor-workload-'));state_parent.chmod(0o700)
        state=state_parent/'state'
        from store import connect
        db=connect(state_parent/'db')
        verifier=WorkloadArtifactVerifier(docker=args.docker,socket=args.socket,postgres_image=POSTGRES,state_dir=state)
        try:
            for role in ('queue','scheduler'):
                doc=document(image,role);evidence=verifier.verify(db,doc)
                assert evidence['role']==role and evidence['image']==image
                assert verifier.verify(db,doc)==evidence
                print(json.dumps({'actual_candidate_role':role,'artifact_verified':True,'journal_replay':True,'runtime_operational':False}),flush=True)
            if args.publication:publication(db,verifier,image,args.docker)
            verified=True
        finally:db.close()
    finally:
        if nonce and not args.build_only and verified:
            row=json.loads(run([args.docker,'image','inspect',image]))[0]
            assert row['Config']['Labels']['net.shiftcodes.workload-candidate']==nonce
            run([args.docker,'image','rm',image])
        after=peers(args.docker)
        unchanged=after==baseline
        if state:
            if verified and unchanged:shutil.rmtree(state.parent)
            else:
                (state.parent/'peer-observations.json').write_text(json.dumps({'before':baseline,'after':after}))
                print(json.dumps({'verifier_journal_retained':str(state),'candidate_image_retained':image}),flush=True)
        if not unchanged:
            print(json.dumps({'peer_snapshot_changed':True,'added_container_ids':sorted(set(after)-set(baseline)),'removed_container_ids':sorted(set(baseline)-set(after)),'changed_container_ids':sorted(k for k in baseline.keys() & after.keys() if baseline[k]!=after[k])}),flush=True)
        print(json.dumps({'peer_containers_preserved':unchanged,'exact_container_cleanup':unchanged,'candidate_image_retained':bool(args.build_only or args.image or not verified)}),flush=True)
        assert unchanged, 'Peer snapshot changed; exact observations and journal retained for review.'
        assert hashes=={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_files}, 'Executed sources changed during acceptance.'
        if verified and args.evidence:
            args.evidence.write_text(json.dumps({'candidate_image':image,'postgres_image':POSTGRES,'framework':'Laravel 12.69.3','source_hashes':hashes,'roles':['queue','scheduler'],'actual_executor_verified':True,'journal_replay_verified':True,'actual_publication_verified':args.publication,'runtime_operational':False,'exact_container_cleanup':True,'peers_unchanged':True,'peer_count':len(baseline)},indent=2)+'\n')
if __name__=='__main__':main()
