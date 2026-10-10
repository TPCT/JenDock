"use strict";
// Native selects retain the form value; the visible control is a searchable listbox.
function searchableSelect(select) {
  if (select.dataset.enhanced) return;
  select.dataset.enhanced = "true";
  const wrap = document.createElement("div"); wrap.className = "select-control";
  select.before(wrap); wrap.append(select); select.hidden = true; select.tabIndex = -1;
  const trigger = document.createElement("button"); trigger.type = "button"; trigger.className = "select-trigger";
  trigger.setAttribute("role","combobox"); trigger.setAttribute("aria-expanded","false"); trigger.setAttribute("aria-haspopup","listbox");
  const label = select.labels?.[0]?.textContent || select.getAttribute("aria-label") || "Select an option";
  trigger.setAttribute("aria-label",label); trigger.disabled = select.disabled;
  const popover = document.createElement("div"); popover.className = "select-popover"; popover.hidden = true; popover.setAttribute("popover","manual");
  const search = document.createElement("input"); search.type = "search"; search.placeholder = "Search options…"; search.setAttribute("aria-label","Search " + label.toLowerCase());
  const list = document.createElement("div"); list.setAttribute("role","listbox"); list.id = "options-" + crypto.randomUUID(); trigger.setAttribute("aria-controls",list.id);search.setAttribute("role","combobox");search.setAttribute("aria-controls",list.id);search.setAttribute("aria-expanded","true");
  let active = 0;
  const sync = () => {trigger.textContent = select.selectedOptions[0]?.textContent || "Choose an option";};
  const close = (focus = false) => {if(popover.matches(":popover-open"))popover.hidePopover();popover.hidden = true; trigger.setAttribute("aria-expanded","false"); if(focus)trigger.focus();};
  const draw = () => {
    const options = [...select.options].filter(o => o.textContent.toLowerCase().includes(search.value.toLowerCase()));
    list.replaceChildren();active = Math.min(active,Math.max(0,options.length-1));
    options.forEach((option,index) => {
      const item = document.createElement("button"); item.type="button"; item.className="select-option"; item.textContent=option.textContent;
      item.setAttribute("role","option");item.setAttribute("aria-selected",String(option.value===select.value));item.tabIndex=-1;item.id=list.id+"-"+index;
      if(index===active){item.classList.add("active");search.setAttribute("aria-activedescendant",item.id);}
      item.addEventListener("click",()=>{select.value=option.value;select.dispatchEvent(new Event("change",{bubbles:true}));sync();close(true);});list.append(item);
    });
    if(!options.length){const note=document.createElement("p");note.textContent="No matching options";list.append(note);search.removeAttribute("aria-activedescendant");}
  };
  const open=()=>{popover.hidden=false;const rect=trigger.getBoundingClientRect();popover.style.width=rect.width+"px";popover.style.left=rect.left+"px";popover.style.top=Math.min(rect.bottom+4,innerHeight-180)+"px";popover.style.maxHeight=Math.max(160,innerHeight-Math.min(rect.bottom+4,innerHeight-180)-12)+"px";popover.showPopover();trigger.setAttribute("aria-expanded","true");search.value="";active=0;draw();search.focus();};
  trigger.addEventListener("click",()=>popover.hidden?open():close());
  trigger.addEventListener("keydown",event=>{if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();open();}});
  search.addEventListener("input",()=>{active=0;draw();});
  search.addEventListener("keydown",event=>{
    const items=[...list.querySelectorAll('[role=option]')];
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close(true);}
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();active=event.key==='Home'?0:event.key==='End'?items.length-1:Math.max(0,Math.min(items.length-1,active+(event.key==='ArrowDown'?1:-1)));draw();list.querySelector('.active')?.scrollIntoView({block:'nearest'});}
    if(event.key==='Enter'){event.preventDefault();items[active]?.click();}
  });
  wrap.addEventListener("focusout",event=>{if(!wrap.contains(event.relatedTarget))close();});
  select.addEventListener("change",sync);popover.append(search,list);wrap.append(trigger,popover);sync();
}
function enhanceSelects(scope=document) {scope.querySelectorAll('select').forEach(searchableSelect);}
let containerContext=null;
function closeContainer() {
  const project=containerContext?.project;
  containerContext=null;
  if(location.hash.startsWith('#container/'))location.hash=project?'project/'+project:'projects';
}
function openContainer(project,name) {
  location.hash='container/'+encodeURIComponent(project)+'/'+encodeURIComponent(name);
}
async function containerView(project,name) {
  const root=document.getElementById('content');
  const top=el('div','','service-workspace-head');
  top.append(link('Back to '+projectLabel(project),'#project/'+project));
  const body=el('div');body.id='container-detail';body.append(el('p','Loading container configuration…','hint'));root.append(top,body);
  closeTerminal();
  containerContext=null;
  try {
    const detail=await api('action',{action:'container-details',project,container:name});
    if(!body.isConnected)return;
    containerContext={project,name,detail,changes:{}};renderContainer('details');
  } catch(error){if(body.isConnected)body.replaceChildren(el('p',error.message,'error'),button('Try again',()=>{root.replaceChildren();containerView(project,name);}));}
}
function renderContainer(tab) {
  closeTerminal();
  const {project,name,detail:d}=containerContext,body=document.getElementById('container-detail');body.replaceChildren();
  body.dataset.tab=tab;
  const tabs=el('div','','project-tabs');
  for(const [key,label] of [['details','Details & version'],['environment','Environment'],['terminal','Terminal']]) {
    const t=button(label,()=>renderContainer(key),'project-tab');t.setAttribute('aria-pressed',String(tab===key));tabs.append(t);
  }
  body.append(tabs);
  if(tab==='terminal'){renderTerminal(body,containerContext);return;}
  if(tab==='details') {
    const facts=el('dl','','container-facts');
    for(const [label,value] of [['Status',d.state],['Health',d.health],['Image / version',d.image],['Image ID',d.image_id],['Git commit',d.commit||'Not recorded in this image'],['Created',d.image_created],['Started',d.started_at],['Restart count',String(d.restart_count)]]) {const valueNode=el('dd',value,'identifier');valueNode.dataset.fact=label;facts.append(el('dt',label),valueNode);}
    const actions=el('div','','actions');
    for(const [operation,label] of [['start','Start'],['stop','Stop'],['restart','Restart']]){const b=button(label,()=>confirmContainer(operation),'secondary',operation==='start'?d.state==='running':d.state!=='running');b.dataset.operation=operation;actions.append(b);}
    const overview=el('div','','service-runtime');overview.append(badge(d.state),el('code',d.image,'service-image'),actions);body.append(overview,el('p','Actions apply only to this container. Stopping a database or cache can affect other services in this project.','hint'),section('Version & runtime'),facts);
    if(d.mounts.length)body.append(section('Persistent mounts'),table(['Type','Container path','Access'],d.mounts.map(m=>[m.type,m.destination,m.read_only?'Read only':'Read / write'])));
  } else {
    body.append(el('p',d.environment_scope,'hint'),el('p','Values stay hidden by default. Changes persist in Compose and recreate this service. Changing a database password variable does not rotate existing database accounts; update connected services and database credentials together.','notice'));
    const form=el('form'),list=el('div','','environment-list'),changes=containerContext.changes;
    const search=el('input');search.type='search';search.placeholder='Find an environment variable';search.setAttribute('aria-label','Find an environment variable');
    const addRow=(key,value)=>{
      const row=el('div','','environment-row'),label=el('label',key),input=el('input');input.type='password';input.autocomplete='off';input.value=value;input.id='env-'+crypto.randomUUID();label.htmlFor=input.id;row.dataset.key=key.toLowerCase();
      input.addEventListener('input',()=>{if(input.value===d.environment[key])delete changes[key];else changes[key]=input.value;});
      const show=button('Show',()=>{input.type=input.type==='password'?'text':'password';show.textContent=input.type==='password'?'Show':'Hide';},'quiet');show.setAttribute('aria-label','Show or hide '+key);row.append(label,input,show);list.append(row);
    };
    Object.entries({...d.environment,...changes}).sort().forEach(([k,v])=>addRow(k,v));
    search.addEventListener('input',()=>{for(const row of list.children)row.hidden=!row.dataset.key.includes(search.value.toLowerCase());});
    const newKey=el('input');newKey.placeholder='NEW_VARIABLE';newKey.setAttribute('aria-label','New variable name');
    const add=button('Add variable',()=>{const key=newKey.value.trim();if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)||Object.hasOwn(d.environment,key)||[...list.children].some(r=>r.dataset.key===key.toLowerCase()))return toast('Enter a unique environment variable name.');changes[key]='';addRow(key,'');newKey.value='';search.value='';search.dispatchEvent(new Event('input'));},'secondary');
    const addWrap=el('div','','actions');addWrap.append(newKey,add);
    const draft=el('p',Object.keys(changes).length+' unsaved changes','draft-status');form.addEventListener('input',()=>{draft.textContent=Object.keys(changes).length+' unsaved changes';});
    const save=el('button','Review changes','primary');save.type='submit';const discard=button('Discard changes',()=>{containerContext.changes={};renderContainer('environment');},'secondary');
    const formActions=el("div","","environment-actions");formActions.append(save,discard);form.append(draft,search,list,addWrap,formActions);
    form.addEventListener('submit',event=>{event.preventDefault();if(!Object.keys(changes).length)return toast('No environment changes to save.');confirmContainer('environment-save',changes);});body.append(form);
  }
}
function confirmContainer(operation,changes) {
  const {project,name,detail:d}=containerContext,requestId=crypto.randomUUID(),dialog=document.getElementById('operation-dialog');
  document.getElementById('operation-title').textContent=operation==='environment-save'?'Apply environment changes?':operation[0].toUpperCase()+operation.slice(1)+' container?';
  const body=document.getElementById('operation-summary');body.replaceChildren(el('strong',name));
  body.append(el('p',operation==='environment-save'?'This recreates the service and briefly interrupts it if running. The previous configuration is restored if runtime checks fail.':'This changes the live container state. Other services may depend on it.'));
  if(changes)body.append(el('p','Changed keys: '+Object.keys(changes).join(', '),'identifier'));
  body.append(el('p','','operation-progress'));
  const confirm=document.getElementById('operation-confirm');confirm.disabled=false;document.getElementById('operation-error').hidden=true;
  confirm.onclick=async()=>{
    confirm.disabled=true;confirm.textContent='Applying…';dialog.dataset.pending='true';document.getElementById('operation-cancel').disabled=true;const error=document.getElementById('operation-error');error.hidden=true;
    try {
      const result=await api('action',{action:operation==='environment-save'?operation:'container-'+operation,project,container:name,container_id:d.container_id,revision:d.revision,changes,request_id:requestId});
      if(result.status!=='succeeded')throw new Error(result.message);
      dialog.close();containerContext.changes={};toast(result.message);await refresh(true);
    } catch(e){error.textContent=e.message;error.hidden=false;}finally{confirm.disabled=false;confirm.textContent='Apply';dialog.dataset.pending='false';document.getElementById('operation-cancel').disabled=false;}
  };dialog.showModal();
}

function setNavigation(open) {
  const mobile=matchMedia('(max-width: 680px)').matches;
  document.getElementById('app').classList.toggle('nav-open',open&&mobile);
  document.getElementById('nav-backdrop').hidden=!(open&&mobile);
  document.getElementById('nav-open').setAttribute('aria-expanded',String(open&&mobile));
  document.getElementById('sidebar').inert=mobile&&!open;
  document.querySelector('main').inert=mobile&&open;
  document.body.classList.toggle('navigation-open',open&&mobile);
  if(open&&mobile)document.getElementById('nav-close').focus();
}
document.addEventListener('DOMContentLoaded',()=>{
  document.getElementById('nav-open').onclick=()=>setNavigation(true);
  for(const id of ['nav-close','nav-backdrop'])document.getElementById(id).onclick=()=>{setNavigation(false);document.getElementById('nav-open').focus();};
  document.addEventListener('keydown',event=>{
    if(!document.getElementById('app').classList.contains('nav-open'))return;
    if(event.key==='Escape'){setNavigation(false);document.getElementById('nav-open').focus();}
    if(event.key==='Tab'){
      const items=[...document.getElementById('sidebar').querySelectorAll('a,button')].filter(n=>n.offsetParent!==null),first=items[0],last=items.at(-1);
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  });
  matchMedia('(max-width: 680px)').addEventListener('change',()=>setNavigation(false));setNavigation(false);
  document.getElementById('operation-cancel').onclick=()=>document.getElementById('operation-dialog').close();
  document.getElementById('operation-dialog').addEventListener('cancel',event=>{if(event.currentTarget.dataset.pending==='true')event.preventDefault();});
  document.getElementById('operation-dialog').addEventListener('close',()=>{document.getElementById('operation-confirm').onclick=null;});
});

function updateContainerLive() {
  if(!containerContext)return;
  const body=document.getElementById('container-detail');if(!body)return;
  const c=containers().find(c=>c.Names===containerContext.name),d=containerContext.detail;
  const replaced=c?.ID&&!d.container_id.startsWith(c.ID),unavailable=!c||stale('containers')||replaced;
  let warning=body.querySelector('.service-stale');
  if(unavailable&&!warning){warning=el('div','','notice service-stale');warning.append(el('p',replaced?'This service was replaced. Reload to inspect its new version.':!c?'This service is no longer in the latest observation.':'Current service status is unavailable.'),button('Reload service details',()=>{if(Object.keys(containerContext.changes).length){toast('Your environment draft is still open. Save or discard it before reloading.');return;}render(true);}));body.prepend(warning);}
  if(!unavailable&&warning)warning.remove();
  if(c&&!replaced){d.state=c.State;d.health=c.Status;}
  const state=body.querySelector('[data-fact="Status"]');if(state)state.textContent=unavailable?'Status unavailable':c.State;
  const health=body.querySelector('[data-fact="Health"]');if(health)health.textContent=unavailable?'Check current service details':c.Status;
  const summary=body.querySelector('.service-runtime .badge');if(summary)summary.replaceWith(badge(unavailable?'Unavailable':c.State));
  for(const b of body.querySelectorAll('[data-operation]'))b.disabled=unavailable||(b.dataset.operation==='start'?c.State==='running':c.State!=='running');
}

let draftNavigationDialog=null;
function confirmDraftNavigation(nextRoute){
  if(draftNavigationDialog)return;
  const dialog=el('dialog'),heading=el('h2','Leave without saving?');heading.id='draft-navigation-title';
  dialog.setAttribute('aria-labelledby',heading.id);
  const actions=el('div','','dialog-actions');
  const stay=button('Keep editing',()=>dialog.close(),'primary');
  const leave=button('Discard and leave',()=>{containerContext.changes={};dialog.close();location.hash=nextRoute;render(true);},'secondary');
  actions.append(stay,leave);dialog.append(heading,el('p','Your environment changes have not been applied. Stay to review them, or discard this draft.'),actions);
  history.replaceState(null,'','#'+currentRoute);
  dialog.addEventListener('close',()=>{dialog.remove();draftNavigationDialog=null;});
  document.body.append(dialog);draftNavigationDialog=dialog;dialog.showModal();stay.focus();
}
window.addEventListener('beforeunload',event=>{if(containerContext&&Object.keys(containerContext.changes).length){event.preventDefault();event.returnValue='';}});
