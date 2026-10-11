import test from 'node:test';import assert from 'node:assert/strict';
import {blankDomain,domainProblems,domainRequest,domainScope,domainPins,saveDomainRequest,restoreDomainRequest,adoptionRequest,canOpenDomain,domainDisplayStatus,domainExecutorMessage,domainCertificateView} from './domains.js';
const values={...blankDomain(),hostname:'Dev.Example.Test.',service_id:'service',zone_id:'zone',dns_type:'A',target:'192.0.2.1'};
const preview={definition:{...values,hostname:'dev.example.test',project:'app',environment:'development'},...Object.fromEntries(domainPins.map(k=>[k,k==='preview_fingerprint'?'a'.repeat(64):1]))};
test('create captures exact scope and all preview pins',()=>{const r=domainRequest('app','development',values,preview,'stable-request-1234');assert.equal(r.values.hostname,'dev.example.test');assert.equal(r.values.expected_project_revision,1);assert.throws(()=>domainRequest('app','production',values,preview,'stable-request-1234'));assert.throws(()=>domainRequest('app','development',{...values,target:'other'},preview,'stable-request-1234'));});
test('uncertain request survives reload with same id and scope',()=>{const data=new Map();const storage={setItem:(k,v)=>data.set(k,v),getItem:k=>data.get(k)};const r=domainRequest('app','development',values,preview,'stable-request-1234');saveDomainRequest(storage,'k',r);assert.deepEqual(restoreDomainRequest(storage,'k','app','development'),r);assert.equal(restoreDomainRequest(storage,'k','app','production'),null);assert.throws(()=>saveDomainRequest(null,'k',r));assert.throws(()=>saveDomainRequest({setItem(){throw Error();}},'k',r));});
test('field validation uses eligible service zone and trusted origin',()=>{const options={services:[{id:'service'}],zones:[{id:'zone',name:'example.test'}],origins:[{dns_type:'A',target:'192.0.2.1'}]};assert.deepEqual(domainProblems(values,options),{});assert.ok(domainProblems({...values,hostname:'evil-example.test'},options).hostname);assert.ok(domainProblems(values,{...options,services:[]}).service_id);assert.ok(domainProblems({...values,target:'foreign'},options).dns_type);});
test('adoption captures reviewed exact provider identity',()=>{const op={id:'op',project:'app',environment:'development',next_action:'review-dns-adoption',adoption_preview:{preview_digest:'b'.repeat(64),records:[{id:'record'}]}};assert.equal(adoptionRequest('app','development',op,'record').record_id,'record');assert.throws(()=>adoptionRequest('app','development',op,'other'));assert.throws(()=>adoptionRequest('app','production',op,'record'));});
test('HTTP verification alone never exposes application link',()=>{assert.equal(canOpenDomain({ready:false,origin:{status:'http-verified'}}),false);assert.throws(()=>domainScope({project:'other',environment:'development'},'app','development'));});

test('executor messages distinguish disabled, missing, stale and unavailable evidence',()=>{
 const status=(state,extra={})=>({executor_available:false,executor:{enabled:true,configured:true,state,stale:false,...extra}});
 assert.match(domainExecutorMessage(status('disabled',{enabled:false,stale:true})),/disabled/);
 assert.match(domainExecutorMessage(status('unavailable',{configured:false})),/configuration is missing or invalid/);
 assert.match(domainExecutorMessage(status('unavailable')),/cannot currently reach/);
 assert.match(domainExecutorMessage(status('stale',{stale:true})),/out of date/);
 assert.match(domainExecutorMessage(status('blocked')),/needs recovery/);
 assert.match(domainExecutorMessage(status('unobserved',{configured:false,stale:true})),/No domain worker observation/);
 assert.match(domainExecutorMessage(status('invalid-observation',{configured:false,stale:true})),/could not be verified/);
 assert.match(domainExecutorMessage({executor_available:false}),/has not been verified/);
 assert.equal(domainExecutorMessage({executor_available:true}),'');
});

test('public link and ready label expire without a server refresh',()=>{
  const route={status:'ready',ready:true,dns:{status:'verified'},origin:{status:'verified'},tls:{status:'verified',not_after:200},external:{status:'verified',expires_at:100}};
  assert.equal(canOpenDomain(route,99),true);assert.equal(domainDisplayStatus(route,99),'Ready');
  assert.equal(canOpenDomain(route,100),false);assert.equal(domainDisplayStatus(route,100),'Verification required');
  assert.equal(canOpenDomain({...route,external:undefined},99),false);
  assert.equal(canOpenDomain({...route,external:{status:'verified',expires_at:Infinity}},99),false);
});

test('certificate expiry independently removes the application link',()=>{
 const route={ready:true,dns:{status:'verified'},origin:{status:'verified'},tls:{status:'verified',not_after:100},external:{status:'verified',expires_at:200}};
 assert.equal(canOpenDomain(route,99),true);assert.equal(canOpenDomain(route,100),false);
 for(const expiry of [undefined,Infinity,NaN,0])assert.equal(canOpenDomain({...route,tls:{status:'verified',not_after:expiry}},99),false);
});
test('certificate presentation distinguishes missing evidence, expiry and renewal checks',()=>{
 assert.equal(domainCertificateView({},100).status,'Unavailable');
 assert.equal(domainCertificateView({tls:{not_after:100}},100).status,'Expired');
 const view=domainCertificateView({tls:{not_after:200},renewal:{status:'complete',next_check_at:180,updated_at:90}},100);
 assert.equal(view.renewal,'Last check completed');assert.equal(view.status,'Valid');assert.equal(view.nextCheck,180);
 assert.equal(domainCertificateView({tls:{not_after:200}},100).renewal,'No attempt recorded');
 const failed=domainCertificateView({renewal:{status:'retrying',error_code:'private-error',next_check_at:120}},100);
 assert.equal(failed.renewal,'Retry scheduled');assert.equal(failed.warning,true);assert.doesNotMatch(JSON.stringify(failed),/private-error/);
 assert.equal(domainCertificateView({renewal:{status:'unexpected',next_check_at:Infinity}},100).nextCheck,null);
 assert.equal(domainCertificateView({renewal:{status:'running',next_check_at:90}},100).nextCheck,null);
});
