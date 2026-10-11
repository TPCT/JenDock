"""Owned Cloudflare DNS effects with write-ahead intent and uncertain-write recovery."""
import hashlib
import ipaddress
import json
import re
import time
import urllib.parse
import urllib.request
import uuid

import ingress
import integrations
import registry

class DNSError(ValueError):pass

def _digest(value):return hashlib.sha256(registry.canonical(value).encode()).hexdigest()

def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS ingress_dns_ownership(route_id TEXT PRIMARY KEY,zone_id TEXT NOT NULL,record_id TEXT NOT NULL,marker TEXT NOT NULL,operation_id TEXT NOT NULL,record_digest TEXT NOT NULL,observed REAL NOT NULL,UNIQUE(zone_id,record_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS ingress_dns_intents(operation_id TEXT PRIMARY KEY,route_id TEXT NOT NULL,fingerprint TEXT NOT NULL,marker TEXT NOT NULL,method TEXT NOT NULL,record_id TEXT,state TEXT NOT NULL,created REAL NOT NULL)''')

def request(method,path,token,body=None):
    """Fixed API origin; redirects disabled; at most 1 MiB received per request."""
    if method not in ('GET','POST','PATCH') or not re.fullmatch(r'/zones/[a-f0-9]{32}(?:/dns_records(?:/[a-f0-9]{32})?)?(?:\?[A-Za-z0-9%_.=&-]+)?',path):raise DNSError('Unsupported DNS request.')
    try:
        integrations._token(token)
        data=None if body is None else registry.canonical(body).encode()
        if data is not None and len(data)>8192:raise ValueError()
        req=urllib.request.Request('https://api.cloudflare.com/client/v4'+path,data=data,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
        with urllib.request.build_opener(integrations._NoRedirect()).open(req,timeout=10) as response:
            if response.status!=200:raise ValueError()
            raw=response.read(1048577)
        if len(raw)>1048576:raise ValueError()
        return json.loads(raw)
    except Exception:raise DNSError('Cloudflare request failed or its outcome is uncertain.') from None

def _call(transport,method,path,token,body=None):
    try:
        response=transport(method,path,token,body)
        if len(registry.canonical(response).encode())>1048576 or not isinstance(response,dict) or response.get('success') is not True:raise ValueError()
        return response
    except Exception:raise DNSError('Cloudflare request failed or its outcome is uncertain.') from None

def _context(db,route_id,key_path,authorize):
    authorize()
    row=db.execute('SELECT d.document,d.digest FROM ingress_routes r JOIN ingress_definitions d ON d.id=r.definition_id WHERE r.id=?',(route_id,)).fetchone()
    if row is None:raise DNSError('Accepted domain intent is unavailable.')
    definition=json.loads(row['document'])
    if _digest(definition)!=row['digest']:raise DNSError('Accepted domain intent changed.')
    connection=db.execute("SELECT revision,enabled,metadata FROM integration_connections WHERE provider='cloudflare'").fetchone()
    if not connection or not connection['enabled'] or connection['revision']!=definition['integration_revision'] or _digest(json.loads(connection['metadata']))!=definition['integration_observation_digest']:raise DNSError('Cloudflare integration changed; revalidate the domain intent.')
    if ingress._origin()[1]!=definition['origin_revision']:raise DNSError('Configured origin changed; revalidate the domain intent.')
    token=integrations.credential_for_use(db,'cloudflare',key_path)
    return definition,token

def _record(value,hostname):
    if not isinstance(value,dict) or not re.fullmatch('[a-f0-9]{32}',str(value.get('id',''))) or value.get('name')!=hostname:raise DNSError('DNS record identity differs from the exact hostname.')
    if not isinstance(value.get('type'),str) or not isinstance(value.get('content'),str) or len(value['content'])>4096 or type(value.get('ttl')) is not int or type(value.get('proxied',False)) is not bool or value.get('comment') is not None and (not isinstance(value['comment'],str) or len(value['comment'])>500):raise DNSError('DNS record metadata is invalid.')
    return {key:value.get(key) for key in ('id','name','type','content','ttl')}|{'proxied':value.get('proxied',False),'comment':value.get('comment') or ''}

def _records(db,route_id,key_path,transport,authorize):
    definition,token=_context(db,route_id,key_path,authorize)
    zone=_call(transport,'GET','/zones/'+definition['zone_id'],token).get('result')
    if not isinstance(zone,dict) or zone.get('id')!=definition['zone_id'] or zone.get('name')!=definition['zone_name'] or zone.get('status')!='active' or not isinstance(zone.get('account'),dict) or zone['account'].get('id')!=definition['account_id']:raise DNSError('Cloudflare zone or account identity changed.')
    records=[];ids=set();pages=None
    for page in range(1,11):
        current,token=_context(db,route_id,key_path,authorize)
        if current!=definition:raise DNSError('Domain intent changed during DNS observation.')
        path='/zones/'+definition['zone_id']+'/dns_records?'+urllib.parse.urlencode({'name.exact':definition['hostname'],'page':page,'per_page':100,'match':'all'})
        response=_call(transport,'GET',path,token);info=response.get('result_info');rows=response.get('result')
        if not isinstance(info,dict) or type(info.get('total_pages')) is not int or not 1<=info['total_pages']<=10 or info.get('page')!=page or not isinstance(rows,list) or len(rows)>100:raise DNSError('DNS pagination is incomplete or exceeds its bound.')
        if pages is not None and pages!=info['total_pages']:raise DNSError('DNS pagination changed during observation.')
        pages=info['total_pages']
        for value in rows:
            record=_record(value,definition['hostname'])
            if record['id'] in ids:raise DNSError('DNS pagination contains duplicate record identities.')
            ids.add(record['id']);records.append(record)
        if page==pages:return definition,sorted(records,key=lambda row:row['id'])
    raise DNSError('DNS observation exceeded its bound.')

def _preview_digest(definition,records):return _digest({'definition':_digest(definition),'records':records})

def preview(db,route_id,key_path,*,transport=None,authorize):
    initialize(db);definition,records=_records(db,route_id,key_path,transport or request,lambda:authorize('read'))
    return {'route_id':route_id,'preview_digest':_preview_digest(definition,records),'records':[{k:v for k,v in row.items() if k in ('id','name','type','ttl','proxied') or (k=='content' and row['type'] in ('A','AAAA','CNAME'))} for row in records],
            'dns':'observed','ready':False,'origin':'unverified','tls':'unverified','adoption_required':bool(records) and not db.execute('SELECT 1 FROM ingress_dns_ownership WHERE route_id=?',(route_id,)).fetchone()}

def _payload(definition,marker):return {'name':definition['hostname'],'type':definition['dns_type'],'content':definition['target'],'proxied':definition['proxied'],'ttl':definition['ttl'],'comment':marker}

def _matches(record,payload):return all(record.get(k)==v for k,v in payload.items())

def _verified(db,route_id,intent,definition,records,key_path,authorize):
    matches=[row for row in records if row['comment']==intent['marker'] and _matches(row,_payload(definition,intent['marker'])) and (intent['record_id'] is None or row['id']==intent['record_id'])]
    if len(records)!=1 or len(matches)!=1:raise DNSError('DNS outcome requires reconciliation; no further write was attempted.')
    record=matches[0]
    with registry.transaction(db):
        current,_=_context(db,route_id,key_path,authorize)
        if current!=definition:raise DNSError('Domain intent changed before DNS ownership commit.')
        db.execute('INSERT INTO ingress_dns_ownership VALUES(?,?,?,?,?,?,?) ON CONFLICT(route_id) DO UPDATE SET record_id=excluded.record_id,marker=excluded.marker,operation_id=excluded.operation_id,record_digest=excluded.record_digest,observed=excluded.observed',(route_id,definition['zone_id'],record['id'],intent['marker'],intent['operation_id'],_digest(record),time.time()))
        db.execute("UPDATE ingress_dns_intents SET state='verified',record_id=? WHERE operation_id=?",(record['id'],intent['operation_id']))
    return {'route_id':route_id,'record_id':record['id'],'zone_id':definition['zone_id'],'dns':'verified','origin':'unverified','tls':'unverified','ready':False}

def apply(db,route_id,operation_id,key_path,*,preview_digest,adopt_record_id=None,transport=None,authorize):
    """No retries of a journaled write; resume only rereads its exact marker."""
    if not isinstance(operation_id,str) or not re.fullmatch('[A-Za-z0-9-]{16,100}',operation_id) or not isinstance(preview_digest,str) or not re.fullmatch('[a-f0-9]{64}',preview_digest) or adopt_record_id is not None and not re.fullmatch('[a-f0-9]{32}',str(adopt_record_id)):raise DNSError('Invalid DNS operation or preview identity.')
    initialize(db);transport=transport or request
    authorize_action=authorize
    action='adopt' if adopt_record_id is not None else 'write'
    authorize=lambda:authorize_action(action)
    definition,records=_records(db,route_id,key_path,transport,authorize)
    fingerprint=_digest({'route_id':route_id,'definition':_digest(definition),'preview_digest':preview_digest,'adopt_record_id':adopt_record_id})
    previous=db.execute('SELECT * FROM ingress_dns_intents WHERE operation_id=?',(operation_id,)).fetchone()
    if previous:
        if previous['route_id']!=route_id or previous['fingerprint']!=fingerprint:raise DNSError('DNS operation identity belongs to different settings.')
        return _verified(db,route_id,previous,definition,records,key_path,authorize)
    if _preview_digest(definition,records)!=preview_digest:raise DNSError('DNS preview changed; refresh before accepting.')
    owner=db.execute('SELECT * FROM ingress_dns_ownership WHERE route_id=?',(route_id,)).fetchone()
    if owner:
        if adopt_record_id is not None or owner['zone_id']!=definition['zone_id'] or len(records)!=1 or records[0]['id']!=owner['record_id'] or records[0]['comment']!=owner['marker'] or _digest(records[0])!=owner['record_digest']:raise DNSError('Owned DNS record changed; explicit reconciliation is required.')
        method='PATCH';record_id=owner['record_id']
    elif records:
        if len(records)!=1 or records[0]['id']!=adopt_record_id:raise DNSError('Existing DNS requires explicit adoption of the exact previewed record.')
        if records[0]['type']!=definition['dns_type'] or records[0]['comment'].startswith('deploy-monitor:'):raise DNSError('Existing DNS record belongs to another operation or incompatible record type.')
        method='PATCH';record_id=adopt_record_id
    else:
        if adopt_record_id is not None:raise DNSError('The record selected for adoption no longer exists.')
        method='POST';record_id=None
    with registry.transaction(db):
        current,token=_context(db,route_id,key_path,authorize)
        if current!=definition:raise DNSError('Domain intent changed before DNS mutation.')
        if db.execute("SELECT 1 FROM ingress_dns_intents WHERE route_id=? AND state!='verified'",(route_id,)).fetchone():raise DNSError('A prior DNS write remains unresolved; resume that operation.')
        if db.execute('SELECT 1 FROM ingress_dns_intents WHERE operation_id=?',(operation_id,)).fetchone():raise DNSError('DNS operation was claimed concurrently; reread its outcome.')
        marker='deploy-monitor:'+uuid.uuid4().hex
        db.execute('INSERT INTO ingress_dns_intents VALUES(?,?,?,?,?,?,?,?)',(operation_id,route_id,fingerprint,marker,method,record_id,'write-intent',time.time()))
    # Read the exact name again after journaling, then reauthorize immediately before effect.
    try:
        current,before=_records(db,route_id,key_path,transport,authorize)
        if current!=definition or before!=records:raise DNSError('DNS changed before mutation; intent retained for reconciliation.')
        current,token=_context(db,route_id,key_path,authorize)
        if current!=definition:raise DNSError('Domain intent changed immediately before DNS mutation.')
        path='/zones/'+definition['zone_id']+'/dns_records'+('/'+record_id if record_id else '')
        _call(transport,method,path,token,_payload(definition,marker))
    except DNSError:
        pass  # A failed response cannot prove that the provider did not apply the write.
    _,after=_records(db,route_id,key_path,transport,authorize)
    intent=db.execute('SELECT * FROM ingress_dns_intents WHERE operation_id=?',(operation_id,)).fetchone()
    return _verified(db,route_id,intent,definition,after,key_path,authorize)
