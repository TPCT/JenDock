"""Read-only, bounded run status; never serialize migration sources or credentials."""
import json
import math
import re
from database_bindings import _digest

STATES={'not-configured','captured','source-ready','planned','applying','recovery-required','verified','failed'}

def public_status(db,job_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_migrations'").fetchone():return None
    row=db.execute('SELECT state,proof,proof_digest,updated FROM job_migrations WHERE job_id=?',(job_id,)).fetchone()
    if row is None:return None
    unavailable={'state':'unavailable','files':[],'updated':None}
    try:
        if row['state'] not in STATES or type(row['updated']) not in (int,float) or not math.isfinite(row['updated']):return unavailable
        files=[]
        if row['proof'] is not None:
            if len(row['proof'])>16*1024*1024:return unavailable
            proof=json.loads(row['proof'])
            if _digest(proof)!=row['proof_digest']:return unavailable
            migrations=proof['helper_proof']['plan']['migrations']
            if not isinstance(migrations,list) or len(migrations)>1000:return unavailable
            for item in migrations:
                path=item[4]
                if not isinstance(path,str) or len(path)>512 or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*',path) or any(p in ('.','..','.git') for p in path.split('/')):return unavailable
                if path not in files:files.append(path)
        elif row['state'] in ('planned','applying','verified','failed','recovery-required'):return unavailable
        return {'state':row['state'],'files':files,'updated':row['updated']}
    except (ValueError,TypeError,KeyError,IndexError):return unavailable
