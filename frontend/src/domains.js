export const domainPins = ['preview_fingerprint','expected_environment_revision','expected_source_revision','expected_integration_revision','expected_origin_revision','expected_policy_revision','expected_project_revision'];
export const blankDomain = () => ({service_id:'',hostname:'',zone_id:'',dns_type:'',target:'',proxied:true,ttl:1});
export function domainScope(value, project, environment) {
  if (!value || value.project !== project || value.environment !== environment) throw Error('The domain response belongs to a different environment. Reload to continue.');
  return value;
}
export function domainProblems(values, options) {
  const errors={};const host=values.hostname.trim().toLowerCase().replace(/\.$/,'');
  if (!/^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(host) || /^\d+(?:\.\d+){3}$/.test(host)) errors.hostname='Enter a hostname without https://, a port, or a path.';
  const zone=options?.zones?.find(x=>x.id===values.zone_id);
  if (!zone) errors.zone_id='Choose a verified DNS zone.';
  else if (host!==zone.name && !host.endsWith('.'+zone.name)) errors.hostname='Use a hostname within the selected DNS zone.';
  if (!options?.services?.some(x=>x.id===values.service_id)) errors.service_id='Choose an eligible running application.';
  if (!options?.origins?.some(x=>x.dns_type===values.dns_type && x.target===values.target)) errors.dns_type='Choose a configured origin.';
  if (!Number.isInteger(Number(values.ttl)) || !(Number(values.ttl)===1 || Number(values.ttl)>=60 && Number(values.ttl)<=86400)) errors.ttl='Use Automatic (1), or 60–86400 seconds.';
  return errors;
}
export function domainValues(values) {return {...values,hostname:values.hostname.trim().toLowerCase().replace(/\.$/,''),ttl:Number(values.ttl)};}
export function domainRequest(project, environment, values, preview, requestId) {
  domainScope(preview?.definition,project,environment);
  if (!/^[A-Za-z0-9-]{16,100}$/.test(requestId||'')) throw Error('A stable request ID is required.');
  const normalized=domainValues(values);
  for (const key of Object.keys(normalized)) if (normalized[key]!==preview.definition[key]) throw Error('Settings changed. Review a fresh preview.');
  const pins={};for (const key of domainPins) {if (!(key in preview)) throw Error('Preview evidence is incomplete. Reload the preview.');pins[key]=preview[key];}
  return {action:'domain-create',project,environment,request_id:requestId,values:{...normalized,...pins}};
}
export function domainStorageKey(username,project,environment) {return 'monitor-domain-request:'+JSON.stringify([username,project,environment]);}
export function saveDomainRequest(storage,key,request) {
  if (!storage) throw Error('Browser session storage is unavailable. Enable it before connecting a domain so an interrupted request can be recovered.');
  try {const text=JSON.stringify(request);storage.setItem(key,text);if(storage.getItem(key)!==text) throw Error();}
  catch {throw Error('The request could not be saved for recovery. No domain request was sent.');}
}
export function restoreDomainRequest(storage,key,project,environment) {
  try {const value=JSON.parse(storage?.getItem(key)||'null');if (!value) return null;domainScope(value,project,environment);if(value.action!=='domain-create'||!/^[A-Za-z0-9-]{16,100}$/.test(value.request_id)||!value.values||domainPins.some(k=>!(k in value.values)))return null;
    if(['service_id','hostname','zone_id','dns_type','target'].some(k=>typeof value.values[k]!=='string'||value.values[k].length>300)||typeof value.values.proxied!=='boolean'||!Number.isInteger(value.values.ttl)||!/^[a-f0-9]{64}$/.test(value.values.preview_fingerprint||''))return null;return value;}catch{return null;}
}
export function adoptionRequest(project,environment,operation,recordId) {
  domainScope(operation,project,environment);
  const preview=operation.adoption_preview;
  if(operation.next_action!=='review-dns-adoption'||!preview?.records?.some(x=>x.id===recordId)||!/^[a-f0-9]{64}$/.test(preview.preview_digest||''))throw Error('Refresh and review the current DNS record before adopting it.');
  return {action:'domain-adopt',project,environment,operation_id:operation.id,record_id:recordId,preview_digest:preview.preview_digest};
}
export const domainStatus = value => String(value||'unverified').replaceAll('-',' ').replace(/^./,c=>c.toUpperCase());
export function domainTime(value) {return Number.isFinite(value)&&value>0?new Date(value*1000).toLocaleString('en-GB'):'Observation time unavailable';}
export function canOpenDomain(route) {return route?.ready===true&&route?.tls?.status==='verified'&&route?.origin?.status==='verified'&&route?.dns?.status==='verified';}

export function domainExecutorMessage(value) {
  const executor=value?.executor;
  if (value?.executor_available===true) return '';
  if (!executor) return 'Domain worker availability has not been verified.';
  if (executor.enabled===false || executor.state==='disabled') return 'Domain execution is disabled on this installation.';
  if (executor.state==='unobserved') return 'No domain worker observation has been received yet.';
  if (executor.state==='invalid-observation') return 'The domain worker observation could not be verified.';
  if (executor.state==='stale' || executor.stale===true) return 'The last domain worker observation is out of date. Waiting for a fresh availability check.';
  if (executor.state==='blocked') return 'The domain worker is blocked by an operation that needs recovery.';
  if (executor.configured===false) return 'Domain worker configuration is missing or invalid.';
  return 'The domain worker cannot currently reach the host routing service.';
}
