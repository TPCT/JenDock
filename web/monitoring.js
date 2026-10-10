'use strict';
let metricWindow='15m';
const bytes=v=>v==null?'—':v>=1073741824?(v/1073741824).toFixed(1)+' GiB':v>=1048576?(v/1048576).toFixed(1)+' MiB':v>=1024?(v/1024).toFixed(1)+' KiB':Math.round(v)+' B';
const percent=(used,total)=>total>0?100*used/total:null;
const metricTime=t=>new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(t*1000));
function svgNode(tag,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));return n;}
function metricChart(title,series,rows,format,maxValue){
  const panel=el('section','','metric-chart'),head=el('div','','chart-heading');head.append(el('h3',title));panel.append(head);
  const chart=svgNode('svg',{viewBox:'0 0 640 190',role:'img','aria-label':title+' over '+metricWindow});
  const values=rows.flatMap(r=>series.map(s=>s.value(r))).filter(v=>Number.isFinite(v));
  if(rows.length<2||!values.length){panel.append(empty('Collecting measurements','A graph appears after two real samples. Missing history is never filled with estimated data.'));return panel;}
  const high=maxValue||Math.max(1,...values)*1.15,start=rows[0].observed,end=rows.at(-1).observed;
  const x=t=>48+Math.max(0,(t-start)/(end-start||1))*580,y=v=>155-Math.max(0,Math.min(1,v/high))*135;
  for(const v of [0,high/2,high]){chart.append(svgNode('line',{x1:48,x2:628,y1:y(v),y2:y(v),class:'chart-grid'}));const label=svgNode('text',{x:42,y:y(v)+4,'text-anchor':'end',class:'chart-axis'});label.textContent=maxValue?Math.round(v)+'%':format(v);chart.append(label);}
  const gap=Math.max(45,({'15m':900,'1h':3600,'24h':86400}[metricWindow]/120)*2);
  for(const s of series){let points=[];let previous=null;const flush=()=>{if(points.length)chart.append(svgNode('polyline',{points:points.join(' '),fill:'none',stroke:s.color,'stroke-width':2.5,'vector-effect':'non-scaling-stroke'}));points=[];};for(const r of rows){const v=s.value(r);if(!Number.isFinite(v)||(previous&&r.observed-previous>gap))flush();if(Number.isFinite(v))points.push(x(r.observed)+','+y(v));previous=r.observed;}flush();}
  for(const [t,anchor] of [[start,'start'],[end,'end']]){const label=svgNode('text',{x:x(t),y:181,'text-anchor':anchor,class:'chart-axis'});label.textContent=metricTime(t);chart.append(label);}
  const cursor=svgNode('line',{x1:x(end),x2:x(end),y1:15,y2:155,class:'chart-cursor'});chart.append(cursor);panel.append(chart);
  const legend=el('div','','chart-values'),time=el('time','','hint');const show=index=>{const row=rows[index];legend.replaceChildren();for(const s of series){const item=el('span',s.label+': '+format(s.value(row)));const dot=el('span','','legend-dot');dot.style.background=s.color;item.prepend(dot);legend.append(item);}time.textContent=metricTime(row.observed);cursor.setAttribute('x1',x(row.observed));cursor.setAttribute('x2',x(row.observed));};
  const scrub=el('input');scrub.type='range';scrub.min='0';scrub.max=String(rows.length-1);scrub.value=scrub.max;scrub.setAttribute('aria-label','Inspect '+title+' measurements');scrub.addEventListener('input',()=>show(Number(scrub.value)));scrub.addEventListener('focus',()=>{pause.hidden=false;});const pause=button('Resume live graphs',()=>{document.activeElement.blur();render(true);},'secondary');pause.hidden=true;show(rows.length-1);panel.append(legend,time,scrub,pause);return panel;
}
function serverMonitoring(root){
  const observation=data.snapshots.telemetry,m=observation?.value,old=!observation||observationFailed||Date.now()/1000-observation.updated>30||data.snapshots['telemetry-error']?.value.error;
  const header=el('div','','monitoring-heading');header.append(el('h2','Server monitoring'),el('span',observation?(old?'Measurements unavailable · last seen ':'Updated ')+date(observation.updated):'Waiting for the host collector','hint'));header.classList.add('monitoring-live-header');root.append(header);
  if(!m){root.append(empty('Server measurements are not available yet','CPU, memory, disk and network history starts when the host collector is connected.'));return;}
  if(old)root.append(el('p','These are the last recorded measurements. The collector is not currently providing fresh data.','notice'));
  const stats=el('div','','server-stats');stats.id='server-stats';
  for(const [label,value,note] of [['CPU usage',m.cpu==null?'Measuring…':m.cpu.toFixed(1)+'%',m.cpu_count+' logical CPUs · load '+m.load_1m.toFixed(2)],['Memory used',bytes(m.memory_used),bytes(m.memory_total)+' total · '+percent(m.memory_used,m.memory_total).toFixed(1)+'%'],['Disk used',bytes(m.disk_used),bytes(m.disk_total-m.disk_used)+' available'],['Network traffic',m.network_rx==null||m.network_tx==null?'Measuring…':bytes(m.network_rx+m.network_tx)+'/s',m.interface+' · receive + transmit']]){const part=el('div','','server-stat');part.append(el('span',label),el('strong',old?'—':value),el('small',old?'Last reading is shown in the history':note));stats.append(part);}root.append(stats);
  const status=el('div','','server-summary');const all=containers();status.append(el('span',all.filter(c=>c.State==='running').length+' / '+all.length+' containers running'),el('span','Host uptime '+Math.floor(m.uptime/86400)+'d '+Math.floor(m.uptime%86400/3600)+'h'),el('span','Swap '+bytes(m.swap_used)+' / '+bytes(m.swap_total)));root.append(status);
  const warnings=[];if(!old){if(m.cpu>=90)warnings.push('CPU usage is above 90%.');if(percent(m.memory_used,m.memory_total)>=90)warnings.push('Memory usage is above 90%.');if(percent(m.disk_used,m.disk_total)>=90)warnings.push('Disk usage is above 90%.');}
  const unhealthy=all.filter(c=>c.Status.includes('unhealthy'));if(unhealthy.length)warnings.push(unhealthy.length+' containers report unhealthy status.');
  if(warnings.length){const alerts=el('div','','notice');alerts.append(el('strong','Current resource alerts'),el('p',warnings.join(' ')),link('Inspect services','#containers'));root.append(alerts);}
  const range=el('div','','metric-range');range.setAttribute('role','group');range.setAttribute('aria-label','Graph time range');range.append(el('span','History'));for(const name of ['15m','1h','24h']){const b=button(name,()=>{metricWindow=name;render(true);},'secondary');b.setAttribute('aria-pressed',String(name===metricWindow));range.append(b);}range.append(el('span','24-hour retention · samples every 15s','hint'));root.append(range);
  const rows=data.metric_history?.[metricWindow]||[],charts=el('div','','monitoring-charts'),pct=v=>v==null?'—':v.toFixed(1)+'%';
  charts.append(metricChart('CPU usage',[{label:'CPU',value:r=>r.cpu,color:'#5746cf'}],rows,pct,100),metricChart('Memory usage',[{label:'RAM',value:r=>percent(r.memory_used,r.memory_total),color:'#237252'}],rows,pct,100),metricChart('Network throughput',[{label:'Receive',value:r=>r.network_rx,color:'#5746cf'},{label:'Transmit',value:r=>r.network_tx,color:'#237252'}],rows,v=>v==null?'—':bytes(v)+'/s'),metricChart('Disk usage',[{label:'Disk',value:r=>percent(r.disk_used,r.disk_total),color:'#8b610d'}],rows,pct,100));root.append(charts);
}
function deploymentIssues(jobs){
  const list=el('div','','deployment-issues');
  for(const j of jobs){const card=el('article','','deployment-issue'),title=el('div','','issue-heading');title.append(link(projectLabel(j.project),'#project/'+j.project),badge(j.status));card.append(title,el('p',j.detail||(['awaiting-validation','auto-deploy-disabled'].includes(j.status)?'This push has not been deployed. Review the recorded run and current branch before starting.':'The last run did not complete. Review its recorded failure before retrying.')),el('small','Run #'+j.id+' · '+(j.branch||'Branch not recorded')+' · '+date(j.updated)));const actions=el('div','','actions');actions.append(link('Review run #'+j.id,'#job/'+j.id));card.append(actions);list.append(card);}return list;
}

function updateMonitoringStatus(){
 const root=document.querySelector('.monitoring-live-header');if(!root||!data)return;
 const observation=data.snapshots.telemetry,m=observation?.value;
 const old=!m||observationFailed||Date.now()/1000-observation.updated>30||data.snapshots['telemetry-error']?.value.error;
 root.querySelector('.hint').textContent=observation?(old?'Measurements unavailable · last seen ':'Updated ')+date(observation.updated):'Waiting for the host collector';
 document.querySelector('.monitoring-charts')?.classList.toggle('measurements-stale',!!old);
 const values=m?[m.cpu==null?'Measuring…':m.cpu.toFixed(1)+'%',bytes(m.memory_used),bytes(m.disk_used),m.network_rx==null||m.network_tx==null?'Measuring…':bytes(m.network_rx+m.network_tx)+'/s']:[];
 document.querySelectorAll('#server-stats strong').forEach((n,i)=>n.textContent=old?'—':values[i]);
}
