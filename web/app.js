'use strict';
const $ = (id) => document.getElementById(id);
const states = {'queued':'في انتظار البناء','building':'جارٍ البناء','testing':'جارٍ الاختبار','deploying':'جارٍ النشر','recovering':'جارٍ استعادة النسخة السابقة','rolled-back':'تم الرجوع للنسخة السابقة','succeeded':'اكتمل النشر','failed':'فشل','superseded':'استبدله تعديل أحدث','awaiting-validation':'بانتظار اعتماد البناء','needs-review':'تحتاج مراجعة الترحيل','interrupted':'توقفت المهمة'};
let containers = [];
let busy = false;
function node(tag, text, className) { const e = document.createElement(tag); e.textContent = text; if(className)e.className = className; return e; }
function date(seconds) { return new Intl.DateTimeFormat('ar-EG',{dateStyle:'medium',timeStyle:'short'}).format(new Date(seconds*1000)); }
function renderContainers() {
  const search = $('search').value.toLowerCase();
  const rows = containers.filter(c => ((c.Names || '') + (c.Image || '')).toLowerCase().includes(search));
  $('container-list').replaceChildren(...rows.map(c => {
    const tr = document.createElement('tr');
    tr.append(node('td',c.Names,'identifier'),node('td',c.Image,'identifier'),node('td',c.Status));return tr;
  }));
  if (!rows.length) {const tr=node('tr','');const td=node('td','لا توجد خدمات مطابقة.');td.colSpan=3;tr.append(td);$('container-list').append(tr);}
}
function render(data) {
  const worker = data.snapshots.worker;
  const stale = !worker || Date.now()/1000-worker.updated>90;
  $('worker').textContent = stale ? 'لا توجد قراءة حديثة' : ({running:'يعمل',unavailable:'تعذر قراءة Docker','recovery-failed':'توقفت العمليات؛ الاستعادة تحتاج مراجعة'}[worker.value.state] || 'حالة غير معروفة');
  $('connection').textContent = stale ? 'اللوحة متصلة؛ قراءة عامل النشر قديمة' : 'متصل — آخر قراءة من السيرفر';
  $('updated').textContent = date(data.observed_at);
  const disk = data.snapshots.disk;
  $('disk').textContent = disk ? (disk.value.free/1073741824).toFixed(1)+' GiB' : 'لم تصل بيانات';
  const resources=data.snapshots.resources?.value;
  $('memory').textContent=resources?(resources.memory.MemAvailable/1073741824).toFixed(1)+' GiB':'لم تصل بيانات';
  $('load').textContent=resources?resources.load_1m.toFixed(2)+' / '+resources.cpu_count+' CPU':'لم تصل بيانات';
  containers=data.snapshots.containers?.value || [];
  const unhealthy=containers.filter(c=>c.State!=='running'||c.Status.includes('unhealthy'));
  $('health-summary').textContent=containers.length?`${containers.length} خدمة مراقبة · ${unhealthy.length} خدمة تحتاج مراجعة${stale?' — القراءة قديمة':''}`:'لم تصل بيانات الخدمات.';
  $('project-list').replaceChildren(...Object.entries(data.projects).map(([name,p])=>{
    const row=node('article','','project');const title=node('div','','project-title');
    title.append(node('h3',p.label || name),node('span',p.enabled?'النشر التلقائي مفعّل':'بانتظار الاعتماد',p.enabled?'badge good':'badge pending'));
    const images=[...new Set(containers.filter(c=>(p.image_prefixes||[]).some(prefix=>c.Image.startsWith(prefix))).map(c=>c.Image))];
    row.append(title,node('p',p.repository+' · '+p.branch,'identifier'),node('p',p.reason || 'يُنشر الفرع المحدد فقط بعد نجاح الاختبارات.','hint'));
    if(images.length)row.append(node('p','الصور المستخدمة حاليًا'+(stale?' (قراءة قديمة)':'')+':','hint'),node('p',images.join(' · '),'identifier'));
    return row;
  }));
  $('job-list').replaceChildren(...data.jobs.map(j=>{
    const row=node('article','','job');const head=node('div','','project-title');
    head.append(node('h3',j.project),node('span',states[j.status] || j.status,'badge '+(j.status==='succeeded'?'good':['failed','interrupted'].includes(j.status)?'bad':'pending')));
    row.append(head,node('code',j.sha.slice(0,12)),node('time',date(j.updated)),node('p',j.detail || 'تم استلام التعديل وتسجيل المهمة.'));return row;
  }));
  if (!data.jobs.length) $('job-list').append(node('p','لم تصل عمليات نشر بعد. ستظهر هنا مهام GitHub وحالتها الفعلية.','empty'));
  containers=data.snapshots.containers?.value || [];renderContainers();
}
async function refresh() {
  if(busy)return;busy=true;$('refresh').disabled=true;
  try {
    const response=await fetch('./api/state',{cache:'no-store',credentials:'same-origin'});
    if(!response.ok)throw new Error(response.status===401?'انتهت المصادقة. أعد تحميل الصفحة وسجّل الدخول.':'تعذر تحميل الحالة. اضغط تحديث للمحاولة مرة أخرى.');
    render(await response.json());$('error').hidden=true;
  } catch(e) {$('error').textContent=e.message;$('error').hidden=false;$('connection').textContent='تعذر التحديث — البيانات المعروضة قديمة';}
  finally{busy=false;$('refresh').disabled=false;}
}
$('refresh').addEventListener('click',refresh);$('search').addEventListener('input',renderContainers);
refresh();setInterval(refresh,15000);
