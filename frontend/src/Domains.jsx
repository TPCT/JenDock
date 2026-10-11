import React, {useCallback,useEffect,useRef,useState} from 'react';
import {Alert,Autocomplete,Box,Button,Chip,CircularProgress,Dialog,DialogActions,DialogContent,DialogTitle,FormControlLabel,IconButton,Link,Stack,Switch,Tooltip,Typography} from '@mui/material';
import Field from './Field.jsx';
import Hint from './Hint.jsx';
import {Refresh} from './icons.jsx';
import {creationSessionStorage} from './projectCreationStorage.js';
import {adoptionRequest,blankDomain,canOpenDomain,domainExecutorMessage,domainProblems,domainRequest,domainScope,domainStatus,domainStorageKey,domainTime,domainValues,restoreDomainRequest,saveDomainRequest} from './domains.js';

function Evidence({value,label}) {
  const evidence=typeof value==='object'&&value?value:{status:'unverified'};
  return <Box className="domain-evidence"><Typography component="dt" fontSize={12} color="text.secondary">{label}</Typography><Typography component="dd" fontSize={13} sx={{m:0}}>{domainStatus(evidence.status)}</Typography><Typography fontSize={12} color="text.secondary">{evidence.evidence==='recorded'?'Recorded evidence · ':''}{domainTime(evidence.observed_at)}</Typography></Box>;
}
const defaultError = error => error?.message || 'Domain information is unavailable. Try reloading this environment.';
export default function Domains({project,environment,username,active,api,onDirty,onPending,onContainers,onIntegrations}) {
  const storage=creationSessionStorage(), storageKey=domainStorageKey(username,project,environment);
  const [request,setRequest]=useState(()=>restoreDomainRequest(storage,storageKey,project,environment));
  const [draft,setDraft]=useState(()=>request?Object.fromEntries(Object.keys(blankDomain()).map(k=>[k,request.values[k]])):blankDomain());
  const [options,setOptions]=useState(null),[routes,setRoutes]=useState([]),[cursor,setCursor]=useState(null);
  const [loading,setLoading]=useState(false),[error,setError]=useState(''),[formError,setFormError]=useState(''),[busy,setBusy]=useState(false);
  const [createOpen,setCreateOpen]=useState(false),[submitted,setSubmitted]=useState(false),[preview,setPreview]=useState(null);
  const [detail,setDetail]=useState(null),[operation,setOperation]=useState(null),[detailOpen,setDetailOpen]=useState(false),[operationError,setOperationError]=useState('');
  const [adoption,setAdoption]=useState(null),[recordId,setRecordId]=useState('');
  const alive=useRef(true),sequence=useRef(0), refreshing=useRef(false), callbacks=useRef({onDirty,onPending});callbacks.current={onDirty,onPending};
  const label=environment==='production'?'Production':'Development';
  const dirty=!!request || JSON.stringify(draft)!==JSON.stringify(blankDomain());
  useEffect(()=>{callbacks.current.onDirty?.(dirty);return()=>callbacks.current.onDirty?.(false);},[dirty]);
  useEffect(()=>{callbacks.current.onPending?.(busy);return()=>callbacks.current.onPending?.(false);},[busy]);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;sequence.current++;};},[]);
  const call=useCallback((action,values={})=>api('action',{action,project,environment,...values}),[api,project,environment]);
  const reload=useCallback(async(append=false)=>{
    if(refreshing.current)return;refreshing.current=true;const ticket=++sequence.current;setLoading(true);
    try {
      const [catalog,list]=await Promise.all([call('domain-options'),call('domain-list',{limit:25,...(append&&cursor?{cursor}:{})})]);
      domainScope(catalog,project,environment);
      if(!Array.isArray(list.routes))throw Error('The domain list response is incomplete. Reload to try again.');
      list.routes.forEach(row=>domainScope(row,project,environment));
      if(!alive.current||ticket!==sequence.current)return;
      setOptions(catalog);setRoutes(previous=>append?[...previous,...list.routes.filter(row=>!previous.some(old=>old.id===row.id))]:list.routes);setCursor(list.next_cursor||null);setError('');
    } catch(failure){if(alive.current&&ticket===sequence.current)setError(defaultError(failure));}
    finally{refreshing.current=false;if(alive.current&&ticket===sequence.current)setLoading(false);}
  },[call,cursor,project,environment]);
  const reloadRef=useRef(reload);reloadRef.current=reload;
  const busyRef=useRef(busy);busyRef.current=busy;
  useEffect(()=>{if(!active)return;reloadRef.current();const timer=setInterval(()=>{if(!document.hidden&&!busyRef.current)reloadRef.current();},15000);return()=>clearInterval(timer);},[active]);
  const fetchOperation=useCallback(async id=>{
    const result=await call('domain-operation',{operation_id:id});domainScope(result.operation,project,environment);
    if(result.operation.id!==id)throw Error('The operation response does not match this domain.');
    return result.operation;
  },[call,project,environment]);
  useEffect(()=>{
    if(!active||!detailOpen||!operation?.id)return;
    let cancelled=false;const timer=setInterval(async()=>{
      if(document.hidden||busy)return;
      try {const result=await fetchOperation(operation.id);if(!cancelled){setOperation(result);setOperationError('');}}
      catch(failure){if(!cancelled)setOperationError(defaultError(failure));}
    },5000);return()=>{cancelled=true;clearInterval(timer);};
  },[active,detailOpen,operation?.id,fetchOperation,busy]);
  const update=(field,value)=>{setDraft(old=>({...old,[field]:value}));setPreview(null);setFormError('');};
  const problems=domainProblems(draft,options);
  const canConfigure=options?.executor_available===true && !!options.services?.length && !!options.zones?.length && !!options.origins?.length && !error;
  async function makePreview(){
    setSubmitted(true);if(Object.keys(problems).length)return;
    setBusy(true);setFormError('');
    try{const value=await call('domain-preview',{values:domainValues(draft)});domainScope(value.definition,project,environment);if(alive.current)setPreview(value);}
    catch(failure){if(alive.current)setFormError(defaultError(failure));}finally{if(alive.current)setBusy(false);}
  }
  async function create(){
    setBusy(true);setFormError('');
    let captured=request;
    try {
      if(!captured){captured=domainRequest(project,environment,draft,preview,crypto.randomUUID());saveDomainRequest(storage,storageKey,captured);setRequest(captured);}
      domainScope(captured,project,environment);
      const result=await api('action',captured);domainScope(result.route,project,environment);
      if(!result.operation?.id)throw Error('The server reply did not include an operation. Check the saved request.');
      if(!alive.current)return;
      try{storage?.removeItem(storageKey);}catch{/* Exact idempotent request remains safe to recover. */}
      setRequest(null);setPreview(null);setDraft(blankDomain());setCreateOpen(false);setSubmitted(false);
      setDetail({...captured.values,...result.route});setOperation(result.operation);setDetailOpen(true);setAdoption(null);setOperationError('');reloadRef.current();
    } catch(failure){
      if(!alive.current)return;
      if(failure.status>=400&&failure.status<500&&failure.code!=='idempotency_conflict'){
        try{storage?.removeItem(storageKey);}catch{}
        setRequest(null);setPreview(null);
      }
      setFormError(defaultError(failure));
    } finally{if(alive.current)setBusy(false);}
  }
  async function openDetail(row){
    setDetail(row);setOperation(null);setAdoption(null);setDetailOpen(true);setOperationError('');setBusy(true);
    try {
      const result=await call('domain-get',{route_id:row.id});domainScope(result.route,project,environment);
      if(result.route.id!==row.id)throw Error('The domain response changed unexpectedly.');
      const op=result.route.operation_id?await fetchOperation(result.route.operation_id):null;
      if(alive.current){setDetail(result.route);setOperation(op);}
    }catch(failure){if(alive.current)setOperationError(defaultError(failure));}finally{if(alive.current)setBusy(false);}
  }
  async function advance(adopt=false){
    setBusy(true);setOperationError('');
    try {
      const payload=adopt?adoptionRequest(project,environment,adoption,recordId):{action:'domain-resume',project,environment,operation_id:operation.id};
      const result=await api('action',payload);domainScope(result.operation,project,environment);
      if(result.operation.id!==operation.id)throw Error('The recovery response does not match this domain.');
      if(alive.current){setOperation(result.operation);setAdoption(null);reloadRef.current();}
    }catch(failure){if(alive.current)setOperationError(defaultError(failure));}finally{if(alive.current)setBusy(false);}
  }
  const service=options?.services?.find(x=>x.id===draft.service_id)||null,zone=options?.zones?.find(x=>x.id===draft.zone_id)||null;
  const origin=options?.origins?.find(x=>x.dns_type===draft.dns_type&&x.target===draft.target)||null;
  const shown=operation||detail;
  return <section className="domains-section" aria-label={`${label} domains`}>
    <Stack direction="row" useFlexGap flexWrap="wrap" alignItems="center" justifyContent="space-between" gap={1} sx={{mb:2}}>
      <Box><Typography variant="h6">Domains</Typography><Typography color="text.secondary" fontSize={13}>Connect a hostname to a running application in {label.toLowerCase()}.</Typography></Box>
      <Stack direction="row" spacing={1} alignItems="center"><Tooltip title="Reload domains" enterTouchDelay={0}><span><IconButton aria-label={`Reload ${label.toLowerCase()} domains`} disabled={loading||busy} onClick={()=>reload()}><Refresh/></IconButton></span></Tooltip><Button variant="contained" disabled={busy||(!canConfigure&&!request)} onClick={()=>{setCreateOpen(true);setFormError('');}}> {request?'Check saved request':'Connect domain'}</Button></Stack>
    </Stack>
    {error&&<Alert severity="error" sx={{mb:2}} action={<Button onClick={()=>reload()} disabled={loading}>Retry</Button>}>{error} {routes.length?'Last recorded domains are shown.':''}</Alert>}
    {request&&<Alert severity="warning" sx={{mb:2}} action={<Button disabled={busy} onClick={()=>setCreateOpen(true)}>Check request</Button>}>A saved connection request needs its outcome checked. Its original settings and request ID are preserved.</Alert>}
    {!loading&&options?.executor_available===false&&<Alert severity="info" sx={{mb:2}}>{domainExecutorMessage(options)} Existing domain records and recovery actions remain available.{options.executor?.observed_at && <Typography fontSize={12}>Last worker observation: {domainTime(options.executor.observed_at)}</Typography>}</Alert>}
    {!loading&&options&&(!options.services?.length||!options.zones?.length||!options.origins?.length)&&<Stack spacing={1} sx={{mb:2}}>
      {!options.services?.length&&<Alert severity="info" action={<Button onClick={onContainers}>View containers</Button>}>No eligible running application has a declared HTTP endpoint in this environment. Configure a build profile and use the container’s Start action when appropriate.</Alert>}
      {!options.zones?.length&&<Alert severity="info" action={<Button onClick={onIntegrations}>Integrations</Button>}>No verified Cloudflare zone is available.</Alert>}
      {!options.origins?.length&&<Alert severity="info">The installation’s public origin address has not been configured.</Alert>}
    </Stack>}
    {loading&&!options?<Stack direction="row" spacing={1} alignItems="center" sx={{py:2}} role="status"><CircularProgress size={18}/><Typography>Loading domains…</Typography></Stack>:!routes.length&&!error?<Box className="domains-empty"><Typography fontWeight={600}>No {label.toLowerCase()} domains</Typography><Typography color="text.secondary" fontSize={13} sx={{mt:1}}>Connect a hostname to an eligible application. DNS, origin, and TLS checks are tracked separately.</Typography></Box>:routes.length?<Box component="ul" className="domain-list">{routes.map(row=><Box component="li" className="domain-row" key={row.id}>
      <Box className="domain-identity"><Button className="domain-name" onClick={()=>openDetail(row)} disabled={busy}>{row.hostname}</Button><Typography color="text.secondary" fontSize={12}>{options?.services?.find(x=>x.id===row.service_id)?.name||'Application '+row.service_id}</Typography><Typography fontSize={12} sx={{mt:.5}}>{domainStatus(row.status)}</Typography></Box>
      <Box component="dl" className="domain-evidence-grid"><Evidence label="DNS" value={row.dns}/><Evidence label="Origin" value={row.origin}/><Evidence label="TLS" value={row.tls}/></Box>
      <Button onClick={()=>openDetail(row)} disabled={busy}>View details</Button>
    </Box>)}</Box>:null}
    {cursor&&<Button sx={{mt:2}} disabled={loading||busy} onClick={()=>reload(true)}>Load more domains</Button>}
    <Dialog open={active&&createOpen} onClose={()=>!busy&&setCreateOpen(false)} fullWidth maxWidth="sm" aria-labelledby={`domain-create-${environment}`}>
      <DialogTitle id={`domain-create-${environment}`}>{request?'Check connection request':'Connect domain'}<Typography color="text.secondary" fontSize={13}>{project} · {label}</Typography></DialogTitle>
      <DialogContent dividers><Stack spacing={2}>
        {formError&&<Alert severity="error">{formError}</Alert>}
        {request&&<Alert severity="warning">Check the original request to recover its result. Settings stay locked until the outcome is known; checking uses the same request ID.</Alert>}
        <Autocomplete options={options?.services||[]} value={service} getOptionLabel={x=>x.name} isOptionEqualToValue={(a,b)=>a.id===b.id} disabled={busy||!!request} onChange={(_,v)=>update('service_id',v?.id||'')} renderInput={params=><Field {...params} label="Application" required error={submitted&&!!problems.service_id} helperText={submitted?problems.service_id:'Only eligible running applications with an HTTP endpoint are listed.'}/>}/>
        <Field label="Hostname" value={draft.hostname} disabled={busy||!!request} required error={submitted&&!!problems.hostname} helperText={submitted?problems.hostname:'For example, app.example.com'} onChange={e=>update('hostname',e.target.value)} inputProps={{maxLength:253,autoCapitalize:'none',spellCheck:false}}/>
        <Autocomplete options={options?.zones||[]} value={zone} getOptionLabel={x=>x.name} isOptionEqualToValue={(a,b)=>a.id===b.id} disabled={busy||!!request} onChange={(_,v)=>update('zone_id',v?.id||'')} renderInput={params=><Field {...params} label="Cloudflare zone" required error={submitted&&!!problems.zone_id} helperText={submitted?problems.zone_id:undefined}/>}/>
        <Autocomplete options={options?.origins||[]} value={origin} getOptionLabel={x=>`${x.dns_type} · ${x.target}`} isOptionEqualToValue={(a,b)=>a.dns_type===b.dns_type&&a.target===b.target} disabled={busy||!!request} onChange={(_,v)=>{setDraft(old=>({...old,dns_type:v?.dns_type||'',target:v?.target||''}));setPreview(null);}} renderInput={params=><Field {...params} label="Public origin" required error={submitted&&!!problems.dns_type} helperText={submitted?problems.dns_type:'Origin addresses are configured by the installation administrator.'}/>}/>
        <Box><FormControlLabel control={<Switch checked={draft.proxied} disabled={busy||!!request} onChange={e=>{setDraft(old=>({...old,proxied:e.target.checked,ttl:e.target.checked?1:300}));setPreview(null);}}/>} label="Proxy traffic through Cloudflare"/><Hint label="About Cloudflare proxying">A Cloudflare edge certificate does not verify the application’s origin TLS. These checks remain separate.</Hint></Box>
        {!draft.proxied&&<Field label="DNS TTL (seconds)" type="number" value={draft.ttl} disabled={busy||!!request} onChange={e=>update('ttl',e.target.value)} error={submitted&&!!problems.ttl} helperText={problems.ttl&&submitted?problems.ttl:'Use 1 for Automatic, or 60–86400 seconds.'}/>}
        {preview&&<Box className="domain-preview"><Typography fontWeight={600} sx={{mb:1}}>Connection preview</Typography><Typography>{preview.definition.hostname} → {service?.name}</Typography><Typography color="text.secondary" fontSize={13}>{preview.definition.dns_type} · {preview.definition.target} · {label}</Typography><Typography color="text.secondary" fontSize={13} sx={{mt:1}}>The worker will check the gateway and DNS ownership. An existing DNS record requires an explicit adoption review. TLS readiness is verified separately.</Typography></Box>}
      </Stack></DialogContent>
      <DialogActions><Button disabled={busy} onClick={()=>setCreateOpen(false)}>Close</Button>{preview&&!request&&<Button disabled={busy} onClick={()=>setPreview(null)}>Edit settings</Button>}<Button variant="contained" disabled={busy||(!request&&!canConfigure)} onClick={request||preview?create:makePreview}>{busy?'Working…':request?'Check saved request':preview?'Connect domain':'Preview connection'}</Button></DialogActions>
    </Dialog>
    <Dialog open={active&&detailOpen} onClose={()=>!busy&&setDetailOpen(false)} fullWidth maxWidth="md" aria-labelledby={`domain-detail-${environment}`}>
      <DialogTitle id={`domain-detail-${environment}`}>{adoption?'Review existing DNS record':detail?.hostname||'Domain details'}<Typography color="text.secondary" fontSize={13}>{project} · {label}</Typography></DialogTitle>
      <DialogContent dividers><Stack spacing={2}>
        {operationError&&<Alert severity="error" action={<Button disabled={busy} onClick={()=>openDetail(detail)}>Reload</Button>}>{operationError}</Alert>}
        {busy&&!operation&&<Typography role="status">Loading operation…</Typography>}
        {shown&&<><Stack direction="row" alignItems="center" useFlexGap flexWrap="wrap" gap={1}><Chip size="small" label={domainStatus(shown.status)}/>{shown.phase&&<Typography fontSize={13}>Current step: {domainStatus(shown.phase)}</Typography>}</Stack><Typography color="text.secondary" fontSize={12}>Operation updated: {domainTime(operation?.updated_at)}</Typography><Box component="dl" className="domain-evidence-grid"><Evidence label="DNS" value={shown.dns}/><Evidence label="Origin" value={shown.origin}/><Evidence label="TLS" value={shown.tls}/></Box></>}
        {operation?.error_code&&<Alert severity="warning">The operation needs attention: {domainStatus(operation.error_code)}. Review the recorded state before resuming.</Alert>}
        {(operation?.status==='awaiting-tls'||operation?.next_action==='configure-tls')&&<Alert severity="info">HTTP origin setup is complete. Origin TLS configuration is still required; this domain is not ready for verified HTTPS access.</Alert>}
        {operation?.status==='awaiting-external'&&<Alert severity="info">Origin HTTPS is verified. Public application verification is still pending; this domain is not ready for verified external access.</Alert>}
        {shown?.executor_available===false&&<Alert severity="info">{domainExecutorMessage(shown)} Existing operation details and recovery actions remain available.{shown.executor?.observed_at && <Typography fontSize={12}>Last worker observation: {domainTime(shown.executor.observed_at)}</Typography>}</Alert>}
        {adoption?<><Typography>The existing DNS record will become managed by this domain operation. Review its identity and the proposed settings before adopting it.</Typography><Autocomplete options={adoption.adoption_preview?.records||[]} value={adoption.adoption_preview?.records?.find(x=>x.id===recordId)||null} getOptionLabel={x=>`${x.type} · ${x.name} · ${x.content||'Target unavailable'}`} isOptionEqualToValue={(a,b)=>a.id===b.id} disabled={busy} onChange={(_,v)=>setRecordId(v?.id||'')} renderInput={params=><Field {...params} label="Existing DNS record" required/>}/>{adoption.adoption_preview?.records?.filter(x=>x.id===recordId).map(record=><Box className="domain-preview" key={record.id}><Typography fontSize={13}>Existing: {record.type} {record.name} → {record.content||'Target unavailable'}</Typography><Typography fontSize={13}>Proxy: {record.proxied?'On':'Off'} · TTL: {record.ttl===1?'Automatic':record.ttl+' seconds'}</Typography><Typography color="text.secondary" fontSize={12} sx={{overflowWrap:'anywhere'}}>Record ID: {record.id}</Typography><Typography fontSize={13} sx={{mt:1}}>Proposed: {detail?.dns_type} {detail?.hostname} → {detail?.target}</Typography></Box>)}</>:operation?.next_action==='review-dns-adoption'&&<Alert severity="warning">An existing DNS record was found. Review it before allowing this operation to manage it.</Alert>}
        {canOpenDomain(shown)&&detail?.hostname&&<Link href={`https://${detail.hostname}`} target="_blank" rel="noopener">Open application</Link>}
      </Stack></DialogContent>
      <DialogActions><Button disabled={busy} onClick={()=>adoption?setAdoption(null):setDetailOpen(false)}>{adoption?'Cancel review':'Close'}</Button>{adoption?<Button variant="contained" disabled={busy||!recordId} onClick={()=>advance(true)}>Adopt selected DNS record</Button>:operation?.next_action==='review-dns-adoption'?<Button variant="contained" disabled={busy||!!operationError} onClick={()=>{setAdoption(JSON.parse(JSON.stringify(operation)));setRecordId('');}}>Review DNS record</Button>:['resume','review-and-resume'].includes(operation?.next_action)?<Button variant="contained" disabled={busy||!!operationError} onClick={()=>advance()}>Resume connection</Button>:null}</DialogActions>
    </Dialog>
  </section>;
}
