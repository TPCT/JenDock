'use strict';
let metricWindow='15m';
const bytes=v=>v==null?'—':v>=1073741824?(v/1073741824).toFixed(1)+' GiB':v>=1048576?(v/1048576).toFixed(1)+' MiB':v>=1024?(v/1024).toFixed(1)+' KiB':Math.round(v)+' B';
const percent=(used,total)=>total>0?100*used/total:null;
const metricTime=t=>new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(t*1000));
const metricPercent=v=>v==null?'—':v.toFixed(1)+'%';
function svgNode(tag,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));return n;}
const metricDefinitions=[
 {key:'cpu',title:'CPU',value:m=>m.cpu==null?'Measuring…':metricPercent(m.cpu),note:m=>m.cpu_count+' CPUs · load '+m.load_1m.toFixed(2),series:[{label:'CPU',value:r=>r.cpu,color:'#5746cf'}],format:metricPercent,max:100},
 {key:'memory',title:'Memory',value:m=>bytes(m.memory_used),note:m=>bytes(m.memory_total)+' total · '+metricPercent(percent(m.memory_used,m.memory_total)),series:[{label:'RAM',value:r=>percent(r.memory_used,r.memory_total),color:'#237252'}],format:metricPercent,max:100},
 {key:'network',title:'Network',value:m=>m.network_rx==null||m.network_tx==null?'Measuring…':bytes(m.network_rx+m.network_tx)+'/s',note:m=>m.interface+' · receive + transmit',series:[{label:'Receive',value:r=>r.network_rx,color:'#5746cf'},{label:'Transmit',value:r=>r.network_tx,color:'#237252'}],format:v=>v==null?'—':bytes(v)+'/s'},
 {key:'disk',title:'Disk',value:m=>bytes(m.disk_used),note:m=>bytes(m.disk_total-m.disk_used)+' free · '+metricPercent(percent(m.disk_used,m.disk_total)),series:[{label:'Used',value:r=>percent(r.disk_used,r.disk_total),color:'#8b610d'}],format:metricPercent,max:100}
];
function metricChart(def,rows){
 const panel=el('section','','metric-chart');panel.dataset.metric=def.key;
 const head=el('div','','chart-heading'),identity=el('div');identity.append(el('h2',def.title),el('strong','—','metric-value'),el('p','','metric-note'));head.append(identity);panel.append(head);
 const values=rows.flatMap(r=>def.series.map(s=>s.value(r))).filter(Number.isFinite);
 if(rows.length<2||!values.length){panel.append(empty('Collecting history','The graph starts after two samples.'));return panel;}
 const width=window.innerWidth<600?360:640,left=96,right=width-12,high=def.max||Math.max(1,...values)*1.15,start=rows[0].observed,end=rows.at(-1).observed;
 const x=t=>left+Math.max(0,(t-start)/(end-start||1))*(right-left),y=v=>140-Math.max(0,Math.min(1,v/high))*120;
 const chart=svgNode('svg',{viewBox:'0 0 '+width+' 176',role:'img','aria-label':def.title+' history over '+metricWindow});
 for(const v of [0,high/2,high]){chart.append(svgNode('line',{x1:left,x2:right,y1:y(v),y2:y(v),class:'chart-grid'}));const label=svgNode('text',{x:left-8,y:y(v)+4,'text-anchor':'end',class:'chart-axis'});label.textContent=def.max?Math.round(v)+'%':def.format(v);chart.append(label);}
 const gap=Math.max(45,({'15m':900,'1h':3600,'24h':86400}[metricWindow]/120)*2);
 for(const s of def.series){let points=[],previous=null;const flush=()=>{if(points.length)chart.append(svgNode('polyline',{points:points.join(' '),fill:'none',stroke:s.color,'stroke-width':2,'vector-effect':'non-scaling-stroke'}));points=[];};for(const r of rows){const v=s.value(r);if(!Number.isFinite(v)||(previous&&r.observed-previous>gap))flush();if(Number.isFinite(v))points.push(x(r.observed)+','+y(v));previous=r.observed;}flush();}
 for(const [t,anchor] of [[start,'start'],[end,'end']]){const label=svgNode('text',{x:x(t),y:167,'text-anchor':anchor,class:'chart-axis'});label.textContent=metricTime(t);chart.append(label);}
 const cursor=svgNode('line',{x1:x(end),x2:x(end),y1:15,y2:140,class:'chart-cursor'});cursor.hidden=true;cursor.setAttribute('visibility','hidden');chart.append(cursor);panel.append(chart);
 const foot=el('div','','chart-footer'),legend=el('div','','chart-values');
 for(const s of def.series){const item=el('span',s.label),dot=el('span','','legend-dot');dot.style.background=s.color;item.prepend(dot);legend.append(item);}
 if(def.series.length>1)foot.append(legend);
 const inspector=el('div','','sample-inspector');inspector.hidden=true;
 const detail=el('output'),scrub=el('input');scrub.type='range';scrub.min='0';scrub.max=String(rows.length-1);scrub.value=scrub.max;scrub.setAttribute('aria-label','Inspect '+def.title+' measurements');
 const show=()=>{const row=rows[Number(scrub.value)];detail.textContent=metricTime(row.observed)+' · '+def.series.map(s=>s.label+' '+def.format(s.value(row))).join(' · ');cursor.setAttribute('x1',x(row.observed));cursor.setAttribute('x2',x(row.observed));};scrub.addEventListener('input',show);
 const resume=button('Resume live',()=>{document.activeElement.blur();render(true);},'quiet');inspector.append(detail,scrub,resume);
 const inspect=button('Inspect history',()=>{inspector.hidden=false;inspect.hidden=true;cursor.setAttribute('visibility','visible');show();scrub.focus();},'quiet');foot.append(inspect);panel.append(foot,inspector);return panel;
}
function serverMonitoring(root){
 const observation=data.snapshots.telemetry,m=observation?.value;
 const bar=el('div','','monitoring-toolbar'),meta=el('p','','host-meta');if(m)meta.textContent='Uptime '+Math.floor(m.uptime/86400)+'d '+Math.floor(m.uptime%86400/3600)+'h · Swap '+bytes(m.swap_used)+' / '+bytes(m.swap_total);
 const range=el('div','','metric-range');range.setAttribute('role','group');range.setAttribute('aria-label','Graph time range');range.append(el('span','History'));for(const name of ['15m','1h','24h']){const b=button(name,()=>{metricWindow=name;render(true);},'quiet');b.setAttribute('aria-pressed',String(name===metricWindow));range.append(b);}bar.append(meta,range);root.append(bar);
 const warning=el('p','','notice');warning.id='metrics-warning';warning.hidden=true;root.append(warning);
 if(!m){root.append(empty('Waiting for server measurements','Host readings will appear when the collector connects.'));return;}
 const charts=el('div','','monitoring-charts');for(const def of metricDefinitions)charts.append(metricChart(def,data.metric_history?.[metricWindow]||[]));root.append(charts);
 const alerts=el('p','','notice');alerts.id='resource-alerts';alerts.hidden=true;root.append(alerts);
 root.append(el('p','History retained for 24 hours · one sample every 15 seconds','retention-note'));
}
function deploymentIssues(jobs){
 const list=el('div','','deployment-issues');
 for(const j of jobs){const row=el('article','','deployment-issue'),identity=el('div','','issue-identity');identity.append(link(projectLabel(j.project),'#project/'+j.project),el('small',(j.branch||'Branch not recorded')+' · '+date(j.updated)));
 const reason=el('p',(['awaiting-validation','auto-deploy-disabled'].includes(j.status)?'This recorded push has not been deployed.':j.status==='failed'?'Build or deployment failed. Open the run for diagnostics.':j.detail||'Review the recorded result before retrying.'));
 row.append(identity,badge(j.status),reason,link('Review #'+j.id,'#job/'+j.id));list.append(row);}return list;
}
function updateMonitoringStatus(){
 if(!document.querySelector('.monitoring-toolbar')||!data)return;
 const observation=data.snapshots.telemetry,m=observation?.value,old=!m||observationFailed||Date.now()/1000-observation.updated>30||data.snapshots['telemetry-error']?.value.error;
 const warning=document.querySelector('#metrics-warning');warning.hidden=!old;warning.textContent=observation?'Host measurements unavailable. History shows the last readings from '+date(observation.updated)+'.':'Waiting for the host collector.';
 document.querySelector('.monitoring-charts')?.classList.toggle('measurements-stale',!!old);
 for(const def of metricDefinitions){const panel=document.querySelector('[data-metric="'+def.key+'"]');if(panel){panel.querySelector('.metric-value').textContent=old?'—':def.value(m);panel.querySelector('.metric-note').textContent=old?'Historical readings below':def.note(m);}}
 const alerts=document.querySelector('#resource-alerts');if(alerts){const warnings=[];if(!old){if(m.cpu>=90)warnings.push('CPU above 90%');if(percent(m.memory_used,m.memory_total)>=90)warnings.push('Memory above 90%');if(percent(m.disk_used,m.disk_total)>=90)warnings.push('Disk above 90%');}alerts.hidden=!warnings.length;alerts.textContent=warnings.join(' · ');}
}
