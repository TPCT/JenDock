"""Local UI acceptance using real HTTP/Unix configuration, no Docker or live DB."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_database_control_flow import DatabaseControlFlowTests

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',action='store_true');parser.add_argument('--release',required=True)
    args=parser.parse_args()
    if not args.run:raise ValueError('Explicit --run required')
    release=Path(args.release)
    if release.exists():raise ValueError('Release marker already exists')
    os.environ['WEB_ROOT']=str(Path(__file__).resolve().parents[1]/'frontend/dist')
    fixture=DatabaseControlFlowTests();fixture.setUp()
    try:
        secret=fixture.save()
        fixture.action('database-binding-save',values={'engine':'postgresql','adapter_version':1,
            'ownership':{'kind':'adopted-compose','reference':'db','revision':1},'database_name':'app',
            'database_port':5432,'secret_id':secret['id'],'secret_revision':1,'allowed_schemas':['public'],'policy_revision':1},expected_revision=0)
        fixture.action('database-migration-credential-save',values={'username':'fixture_migrator','password':'synthetic-only'},expected_revision=0)
        print(json.dumps({'origin':fixture.origin,'project':'app','scope':'synthetic configuration; no database connection or deployment'}),flush=True)
        deadline=time.monotonic()+900
        while not release.exists() and time.monotonic()<deadline:time.sleep(.5)
        binding=fixture.action('database-migration-binding-current')['binding']
        print(json.dumps({'saved':bool(binding),'status':binding['status'] if binding else None,
            'framework':binding['definition']['framework'] if binding else None}),flush=True)
    finally:fixture.doCleanups()

if __name__=='__main__':main()
