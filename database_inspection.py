"""Worker-owned preview, acceptance and bounded reads for registered databases."""
import hashlib
import json
import math
import re
import time
import uuid

import database_bindings as bindings
import database_candidates as candidates
import registry
from application_operations import OperationError


class InspectionError(ValueError):
    def __init__(self,message,code='inspection_failed'):
        super().__init__(message)
        self.code=code


DRIVER_ERRORS={
    'connection_failed':'Could not connect with this inspection account. Check the account and database availability.',
    'timeout':'Database inspection timed out. Retry after the database is available.',
    'permission_denied':'The inspection account cannot read this database object.',
    'privileged_credential':'Use a dedicated read-only account without write or administrative grants.',
    'row_policy_required':'Choose permitted columns in the binding before browsing rows.',
    'unsupported_columns':'This table contains column types that the row inspector does not yet support.',
    'unsupported_pagination':'Row browsing requires a supported single integer primary key.',
    'unsupported_relation':'Row browsing is not supported for this relation type.',
    'unsupported_schema_owner':'The schema ownership does not satisfy the inspection policy.',
    'table_unavailable':'The selected table is unavailable. Reload the table list.',
    'cursor_stale':'This page cursor is no longer valid. Reload from the first page.',
    'identity_changed':'Database identity changed. Verify the binding again.',
    'result_limit':'This result exceeds the inspection limit. Request a smaller page.',
    'invalid_policy':'The saved inspection policy is invalid. Review its configuration.',
    'invalid_request':'The inspection request is invalid. Reload and try again.',
    'query_failed':'The database could not complete this inspection. Reload and try again.',
}


def initialize(db):
    bindings.initialize(db)
    db.execute('''CREATE TABLE IF NOT EXISTS database_previews(
        id TEXT PRIMARY KEY,owner TEXT NOT NULL,project TEXT NOT NULL,environment TEXT NOT NULL,
        binding_id TEXT NOT NULL,binding_revision INTEGER NOT NULL,definition_digest TEXT NOT NULL,
        candidate TEXT NOT NULL,engine_digest TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS database_acceptances(
        owner TEXT NOT NULL,project TEXT NOT NULL,environment TEXT NOT NULL,request_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,result TEXT NOT NULL,
        PRIMARY KEY(owner,project,environment,request_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS database_accepted_bindings(
        binding_id TEXT PRIMARY KEY,binding_revision INTEGER NOT NULL,definition_digest TEXT NOT NULL,
        candidate TEXT NOT NULL,engine_digest TEXT NOT NULL,accepted REAL NOT NULL,preview_id TEXT NOT NULL)''')


def digest(value):
    return hashlib.sha256(registry.canonical(value).encode()).hexdigest()


class InspectorService:
    def __init__(self,db,authority,*,resolve_candidate,helper,key_path,cursor_key):
        self.db,self.authority=db,authority
        self.resolve_candidate,self.helper=resolve_candidate,helper
        self.key_path,self.cursor_key=key_path,cursor_key
        initialize(db)

    def options(self,project,environment,reference):
        principal=self.authority.recheck()
        bindings._authorize(self.db,principal,project,environment,'databases:manage',self.authority.recheck)
        candidate=self.resolve_candidate(project,environment,reference,kind='adopted-compose')
        if (candidate.get('project')!=project or candidate.get('environment')!=environment
                or candidate.get('reference')!=reference):
            raise InspectionError('Database ownership options changed. Reload the container.')
        self.authority.recheck()
        self._registered(candidate)
        return {'candidate':candidates.public(candidate)}

    def _binding(self,project,environment,identifier,revision,scope='databases:manage'):
        bindings._revision(revision)
        principal=self.authority.recheck()
        bindings._authorize(self.db,principal,project,environment,scope,self.authority.recheck)
        row=bindings.get_binding(self.db,principal,project,environment,identifier,revalidate=self.authority.recheck)
        if row['revision']!=revision or row['status']!='unverified':
            raise InspectionError('Database binding changed, is disabled, or has stale credentials.')
        return row

    def _candidate(self,row):
        definition=row['definition'];ownership=definition['ownership']
        candidate=self.resolve_candidate(row['project'],row['environment'],ownership['reference'],kind=ownership['kind'])
        if (candidate['recipe_revision']!=ownership['revision'] or candidate['engine']!=definition['engine']
                or candidate['database']!=definition['database_name'] or candidate['port']!=definition['database_port']):
            raise InspectionError('Database configuration does not match registered ownership.')
        if definition['adapter_version']!=1 or not re.fullmatch('sha256:[a-f0-9]{64}',self.helper.image):
            raise InspectionError('Database verifier configuration is unsupported.')
        candidate=dict(candidate,verifier={'image':self.helper.image,'adapter_version':1})
        candidate['fingerprint']=digest({k:v for k,v in candidate.items() if k!='fingerprint'})
        return candidate

    def _registered(self,candidate):
        project=registry.get_project(self.db,candidate['project'])
        env=registry.get_environment(self.db,candidate['project'],candidate['environment'])
        if (project['revision']!=candidate['project_revision'] or env['revision']!=candidate['environment_revision']
                or env['recipe_revision']!=candidate['recipe_revision']):
            raise InspectionError('Registered database ownership changed.')

    def _run(self,row,candidate,request,scope='databases:manage'):
        definition=row['definition']
        secret=bindings.credential_for_use(self.db,row['project'],row['environment'],definition['secret_id'],definition['secret_revision'],self.key_path)
        def revalidate(target):
            current=self._binding(row['project'],row['environment'],row['id'],row['revision'],scope)
            if current['definition_digest']!=row['definition_digest'] or target!=candidate['container_id']:
                raise InspectionError('Database inspection identity changed.')
            if self._candidate(current)['fingerprint']!=candidate['fingerprint']:
                raise InspectionError('Database container or storage changed. Verify the binding again.')
        revalidate(candidate['container_id'])
        payload={'database':definition['database_name'],'user':secret['username'],'password':secret['password'],
            'port':definition['database_port'],'schemas':definition['allowed_schemas'],
            'row_columns':definition.get('row_columns',{}),'cursor_key':self.cursor_key,
            'cursor_context':{'owner':self.authority.principal.subject_id,'actor_kind':self.authority.principal.actor_kind,'binding':row['id'],'revision':row['revision'],
                'definition':row['definition_digest'],'candidate':candidate['fingerprint']},'request':request}
        response=self.helper.inspect(candidate['container_id'],payload,revalidate=revalidate)
        revalidate(candidate['container_id'])
        if isinstance(response,dict) and set(response)=={'error'}:
            code=response['error']
            if isinstance(code,str) and code in DRIVER_ERRORS:
                raise InspectionError(DRIVER_ERRORS[code],code)
        if not isinstance(response,dict) or set(response)!={'result'}:
            raise InspectionError('Database connection or inspection policy could not be verified.')
        result=response['result']
        if not isinstance(result,dict) or set(result)!={'engine','identity','data','next_cursor','page_size','observed_at'}:
            raise InspectionError('Database verifier returned an invalid result.')
        observed=result['observed_at']
        if type(observed) not in (int,float) or not math.isfinite(observed) or not 0<=time.time()-observed<=60:
            raise InspectionError('Database observation is stale.')
        if request.get('action')=='schemas' and (not isinstance(result['data'],list) or any(s not in definition['allowed_schemas'] for s in result['data'])):
            raise InspectionError('Database verifier returned unexpected schemas.')
        identity=result.get('identity') if isinstance(result,dict) else None
        if (not isinstance(identity,dict) or result.get('engine')!='postgresql'
                or identity.get('database')!=definition['database_name'] or identity.get('user')!=secret['username']
                or not isinstance(identity.get('database_oid'),str) or not identity['database_oid'].isdigit()
                or not isinstance(identity.get('server_version'),str) or not identity['server_version'].isdigit()
                or identity.get('cluster_identity_verified') is not False):
            raise InspectionError('Database engine returned unexpected identity evidence.')
        return result,digest(identity)

    def preview(self,project,environment,binding_id,revision):
        row=self._binding(project,environment,binding_id,revision)
        candidate=self._candidate(row)
        _,engine_digest=self._run(row,candidate,{'action':'schemas'})
        now=time.time();identifier=uuid.uuid4().hex
        with registry.transaction(self.db):
            current=self._binding(project,environment,binding_id,revision)
            if current['definition_digest']!=row['definition_digest']:raise InspectionError('Binding changed.')
            self._registered(candidate)
            self.db.execute('INSERT INTO database_previews VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,self.authority.principal.subject_id,project,environment,binding_id,revision,
                 row['definition_digest'],registry.canonical(candidate),engine_digest,now,now+300))
        return {'preview_id':identifier,'binding_id':binding_id,'binding_revision':revision,
            'candidate':candidates.public(candidate),'expires':now+300,'cluster_identity_verified':False}

    def accept(self,project,environment,preview_id,request_id):
        bindings._id(preview_id)
        bindings._request_context(self.authority.recheck(),project,environment,'accept',request_id)
        if request_id is None:raise InspectionError('A request ID is required.')
        principal=self.authority.recheck()
        bindings._authorize(self.db,principal,project,environment,'databases:manage',self.authority.recheck)
        key=(principal.subject_id,project,environment,request_id);fingerprint=digest({'preview_id':preview_id})
        def replay():
            prior=self.db.execute('SELECT * FROM database_acceptances WHERE owner=? AND project=? AND environment=? AND request_id=?',key).fetchone()
            if prior:
                if prior['request_digest']!=fingerprint:raise InspectionError('Acceptance request ID conflicts.')
                return json.loads(prior['result'])
        prior=replay()
        if prior:
            self.authority.recheck()
            return prior
        preview=self.db.execute('SELECT * FROM database_previews WHERE id=? AND owner=? AND project=? AND environment=?',(preview_id,principal.subject_id,project,environment)).fetchone()
        if preview is None or preview['expires']<=time.time():raise InspectionError('Database preview expired. Verify again.')
        row=self._binding(project,environment,preview['binding_id'],preview['binding_revision'])
        candidate=self._candidate(row)
        if row['definition_digest']!=preview['definition_digest'] or registry.canonical(candidate)!=preview['candidate']:
            raise InspectionError('Database preview is stale. Verify again.')
        _,engine_digest=self._run(row,candidate,{'action':'schemas'})
        if engine_digest!=preview['engine_digest']:raise InspectionError('Database engine identity changed.')
        now=time.time()
        receipt={'binding_id':row['id'],'binding_revision':row['revision'],'preview_id':preview_id,'accepted':now,'state_requires_reload':True}
        with registry.transaction(self.db):
            self._binding(project,environment,row['id'],row['revision']);self._registered(candidate)
            prior=replay()
            if prior:return prior
            if preview['expires']<=time.time():raise InspectionError('Database preview expired. Verify again.')
            self.db.execute('INSERT OR REPLACE INTO database_accepted_bindings VALUES(?,?,?,?,?,?,?)',
                (row['id'],row['revision'],row['definition_digest'],registry.canonical(candidate),engine_digest,now,preview_id))
            self.db.execute('INSERT INTO database_acceptances VALUES(?,?,?,?,?,?)',(*key,fingerprint,registry.canonical(receipt)))
            bindings._event(self.db,principal,project,environment,row['id'],row['revision'],'binding-accepted')
        self.authority.recheck()
        return receipt

    def read(self,project,environment,binding_id,revision,request):
        if not isinstance(request,dict):raise InspectionError('Invalid database read.')
        scope='databases:data:read' if request.get('action')=='rows' else 'databases:read'
        row=self._binding(project,environment,binding_id,revision,scope)
        accepted=self.db.execute('SELECT * FROM database_accepted_bindings WHERE binding_id=?',(binding_id,)).fetchone()
        candidate=self._candidate(row)
        if (accepted is None or accepted['binding_revision']!=revision or accepted['definition_digest']!=row['definition_digest']
                or accepted['candidate']!=registry.canonical(candidate)):
            raise InspectionError('Verify and accept this database binding before reading.')
        result,engine_digest=self._run(row,candidate,request,scope)
        if engine_digest!=accepted['engine_digest']:raise InspectionError('Database identity changed. Verify again.')
        return dict(result, scope={
            'project':row['project'],'environment':row['environment'],
            'binding_id':row['id'],'binding_revision':row['revision'],
            'definition_digest':row['definition_digest'],
            'container_id':candidate['container_id'],
            'candidate_fingerprint':candidate['fingerprint'],
        })
