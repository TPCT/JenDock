"use strict";
const $ = (id) => document.getElementById(id);
const stateNames = {
  queued: "Queued",
  "validation-queued": "Validation queued",
  building: "Building",
  testing: "Testing",
  deploying: "Deploying",
  recovering: "Recovering",
  "rolled-back": "Rolled back",
  succeeded: "Deployed",
  validated: "Build verified",
  failed: "Failed",
  superseded: "Superseded",
  "awaiting-validation": "Not started",
  "auto-deploy-disabled": "Auto-deploy off",
  "needs-review": "Migration review",
  interrupted: "Interrupted",
  cancelled: "Cancelled",
};
const activeStates = [
  "queued",
  "validation-queued",
  "building",
  "testing",
  "deploying",
  "recovering",
];
const retryStates = [
  "failed",
  "interrupted",
  "rolled-back",
  "cancelled",
  "needs-review",
  "superseded",
  "awaiting-validation",
  "auto-deploy-disabled",
];
const projectTabs = {};
const settingsDrafts = {};
let liveConnection = null;
let data = null,
  session = null,
  busy = false,
  actionBusy = false,
  observationFailed = false,
  currentRoute = "",
  buildContext = null,
  toastTimer = null;
const el = (tag, text = "", cls = "") => {
  const n = document.createElement(tag);
  n.textContent = englishMessage(text);
  if (cls) n.className = cls;
  return n;
};
const date = (v) =>
  v
    ? new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(v * 1000))
    : "No observation yet";
const badge = (status) =>
  el(
    "span",
    stateNames[status] || status,
    "badge " +
      (["succeeded", "validated", "Healthy", "Enabled"].includes(status)
        ? "good"
        : ["failed", "interrupted", "Unavailable", "Unhealthy"].includes(status)
          ? "bad"
          : ["running", "Running", "exited", "created"].includes(status) ? "neutral" : "pending"),
  );
const link = (text, href) => {
  const n = el("a", text);
  n.href = href;
  return n;
};
const button = (text, fn, cls = "secondary", disabled = false) => {
  const n = el("button", text, cls);
  n.type = "button";
  n.disabled = disabled;
  n.addEventListener("click", fn);
  return n;
};
const stale = (name) =>
  observationFailed || (!data?.preview && (
  !data?.snapshots[name] ||
  Date.now() / 1000 - data.snapshots[name].updated > 90 ||
  data.snapshots.worker?.value.state !== "running"));
const containers = () => data?.snapshots.containers?.value || [];
const projectImages = (p) =>
  containers().filter(
    (c) =>
      (p.container_prefixes || []).some((prefix) =>
        c.Names.startsWith(prefix),
      ) ||
      (p.image_prefixes || []).some((prefix) => c.Image.startsWith(prefix)),
  );
const projectLabel = (name) => data.projects[name]?.label || name;
function observedText(node, key, liveText, staleText) {
  node.dataset.observation = key;
  if(data?.preview)liveText=liveText.replace("Live", "Captured");
  node.dataset.liveText = liveText;
  node.dataset.staleText = staleText;
  node.textContent = stale(key) ? staleText : liveText;
  return node;
}
function updateObservationLabels() {
  document.querySelectorAll("[data-observation]").forEach((node) => {
    const old = stale(node.dataset.observation);
    node.textContent = old ? node.dataset.staleText : node.dataset.liveText;
    node.dataset.stale = String(old);
  });
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 6000);
}
async function api(path, body) {
  const options = { cache: "no-store", credentials: "same-origin" };
  if (body) {
    options.method = "POST";
    options.headers = {
      "Content-Type": "application/json",
      "X-CSRF-Token": session?.csrf || "",
    };
    options.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch("./api/" + path, options);
  } catch {
    throw new Error("Unable to connect. Check your connection and try again.");
  }
  let value;try{value=await r.json();}catch{throw new Error("The server returned an unreadable response. Try again or check service availability.");}
  if (!r.ok) {
    if (r.status === 401 && path !== "login") showLogin();
    throw new Error(value.error || "Request failed. Please try again.");
  }
  return value;
}
function showLogin() {
  closeTerminal();
  for(const key of Object.keys(settingsDrafts))delete settingsDrafts[key];
  closeContainer();
  $("operation-dialog").close();
  setNavigation(false);
  liveConnection?.close();
  liveConnection = null;
  session = null;
  $("app").hidden = true;
  $("boot").hidden = true;
  $("login").hidden = false;
  if ($("build-dialog").open) $("build-dialog").close();
  document.title = "Sign in | Shiftcodes";
}
async function action(body) {
  if (actionBusy) throw new Error("Wait for the current request to finish.");
  actionBusy = true;
  try {
    const result = await api("action", body);
    if(body.action==='settings')delete settingsDrafts[body.project];
    toast(result.message || "Done.");
    await refresh(true);
    return result;
  } finally {
    actionBusy = false;
  }
}
function formField(labelText, control) {
  const field=el('div','','form-field'),label=el('label',labelText);
  if(!control.id)control.id='field-'+crypto.randomUUID();
  label.htmlFor=control.id;field.append(label,control);return field;
}
function section(title, actionNode) {
  const n = el("div", "", "section-head");
  n.append(el("h2", title));
  if (actionNode) n.append(actionNode);
  return n;
}
function empty(title, description) {
  const n = el("div", "", "empty");
  n.append(el("h3", title), el("p", description));
  return n;
}
function table(headers, rows) {
  const wrap = el("div", "", "panel table-scroll"),
    t = el("table"),
    thead = el("thead"),
    tr = el("tr"),
    tbody = el("tbody");
  headers.forEach((h) => {
    const th = el("th", h);
    th.scope = "col";
    tr.append(th);
  });
  thead.append(tr);
  for (const row of rows) {
    const r = el("tr");
    row.forEach((value, index) => {
      const td = el("td");
      td.dataset.label=headers[index];
      td.append(
        typeof value === "string" ? document.createTextNode(value) : value,
      );
      r.append(td);
    });
    tbody.append(r);
  }
  t.append(thead, tbody);
  wrap.append(t);
  return wrap;
}
function subtitle(main, sub, href) {
  const n = el("div");
  n.append(
    href ? link(main, href) : el("strong", main),
    el("span", sub, "sub identifier"),
  );
  return n;
}
function jobRows(jobs) {
  return jobs.map((j) => [
    link("#" + j.id, "#job/" + j.id),
    subtitle(
      projectLabel(j.project),
      j.branch || "Legacy branch record",
      "#project/" + j.project,
    ),
    el("code", j.sha.slice(0, 12)),
    badge(j.status),
    j.mode === "validate" ? "Build and test" : "Deploy",
    date(j.updated),
    j.actor || "github",
  ]);
}
function jobTable(jobs) {
  return jobs.length
    ? table(
        [
          "Run",
          "Project / branch",
          "Commit",
          "Status",
          "Mode",
          "Updated",
          "Triggered by",
        ],
        jobRows(jobs),
      )
    : empty(
        "No matching runs",
        "Start a build from a project, or push to its configured GitHub branch.",
      );
}
function resources() {
  const n = el("div", "", "resources");
  const disk = data.snapshots.disk,
    res = data.snapshots.resources,
    worker = data.snapshots.worker;
  const values = [
    [
      "Deployment worker",
      worker?.value.state === "running" && !stale("worker")
        ? "Available"
        : "Needs attention",
      worker?.value.state === "unavailable"
        ? "Docker observation unavailable"
        : date(worker?.updated),
      null,
      "worker",
    ],
    [
      "Free disk space",
      disk ? (disk.value.free / 1073741824).toFixed(1) + " GiB" : "—",
      disk
        ? "of " + (disk.value.total / 1073741824).toFixed(1) + " GiB"
        : "No data yet",
      disk ? 100 * (1 - disk.value.free / disk.value.total) : null,
      "disk",
    ],
    [
      "Available memory",
      res
        ? (res.value.memory.MemAvailable / 1073741824).toFixed(1) + " GiB"
        : "—",
      res
        ? "of " + (res.value.memory.MemTotal / 1073741824).toFixed(1) + " GiB"
        : "No data yet",
      res
        ? 100 * (1 - res.value.memory.MemAvailable / res.value.memory.MemTotal)
        : null,
      "resources",
    ],
    [
      "System load",
      res ? res.value.load_1m.toFixed(2) : "—",
      res
        ? "1-minute average / " + res.value.cpu_count + " CPU"
        : "No data yet",
      null,
      "resources",
    ],
  ];
  for (const [label, value, note, percent, key] of values) {
    const part = el("div", "", "resource");
    const metric = el("strong", value);
    if (key === "worker") observedText(metric, key, value, "Unconfirmed");
    part.append(
      el("span", label),
      metric,
      observedText(
        el("small"),
        key,
        note,
        "Stale or unavailable · " + date(data.snapshots[key]?.updated),
      ),
    );
    if (percent !== null) {
      const bar = el("div", "", "meter"),
        progress = el("progress");
      progress.max = 100;
      progress.value = percent;
      progress.setAttribute("aria-label", label + " — usage percentage");
      bar.append(progress);
      part.append(bar);
    }
    n.append(part);
  }
  return n;
}
function uiIcon(name){
  const paths={projects:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',overview:'M3 12h4l3-8 4 16 3-8h4',jobs:'M8 4h12v16H8 M4 8h8 M4 16h8',containers:'M12 3 3 8v9l9 5 9-5V8L12 3 M3 8l9 5 9-5 M12 13v9',audit:'M12 8v4l3 2 M4 5v5h5 M4 10a8 8 0 1 1 1 8',branch:'M6 3v12a4 4 0 0 0 4 4h2 M18 7v3a4 4 0 0 1-4 4H6 M4 3a2 2 0 1 0 4 0a2 2 0 1 0-4 0 M16 5a2 2 0 1 0 4 0a2 2 0 1 0-4 0',arrow:'M5 12h14 M14 7l5 5-5 5',terminal:'M5 6l5 6-5 6 M13 18h6',github:'M8 20v-4 M16 20v-4 M8 16c-4-1-5-5-3-8V4l4 2h6l4-2v4c2 3 1 7-3 8H8'};
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.classList.add('ui-icon');
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name]||paths.projects);svg.append(path);return svg;
}
function projectsTable(items) {
  const list=el('div','','project-directory');
  for(const [name,p] of items){
    const services=projectImages(p),last=data.jobs.find(j=>j.project===name),running=services.filter(c=>c.State==='running'&&!c.Status.includes('unhealthy')).length;
    const row=el('article','','directory-row'),identity=el('div','','directory-identity'),title=el('h2');title.append(link(p.label||name,'#project/'+name));
    identity.append(title,el('p',p.repository,'hint'));
    const source=el('div','','directory-source');source.append(el('span',p.branch,'branch-name'),el('small',p.automatic?'Automatic deployment':'Manual deployment'));
    const runtime=el('div','','directory-runtime');runtime.append(badge(stale('containers')?'Unavailable':!services.length?'Not observed':running===services.length?'Running':'Needs attention'),el('small',services.length?running+' / '+services.length+' services':'No service observation'));
    const activity=el('div','','directory-activity');if(last){activity.append(link('Run #'+last.id,'#job/'+last.id),badge(last.status));}else activity.append(el('span','No runs yet','hint'));
    row.append(identity,source,runtime,activity);list.append(row);
  }return list;
}
function overview() {
  const root=$('content'),active=data.jobs.filter(j=>activeStates.includes(j.status));
  const fleet=el('div','','fleet-summary');
  const all=containers(),running=all.filter(c=>c.State==='running').length;
  fleet.append(link(Object.keys(data.projects).length+' projects','#projects'),link(stale('containers')?'Service status unavailable':running+' / '+all.length+' services running','#containers'),link(active.length?active.length+' runs in progress':'No active deployments','#jobs'));
  const unhealthy=all.filter(c=>c.Status.includes('unhealthy')).length;if(unhealthy&&!stale('containers'))fleet.append(link(unhealthy+' unhealthy services','#containers'));
  root.append(fleet);serverMonitoring(root);
  if(active.length)root.append(section('In progress',link('All runs','#jobs')),jobTable(active));
  const problematic=data.jobs.filter(j=>retryStates.includes(j.status)&&data.jobs.find(x=>x.project===j.project)?.id===j.id);
  root.append(section('Deployment issues',link('Run history','#jobs')));
  if(problematic.length)root.append(deploymentIssues(problematic));else root.append(el('p','No unresolved deployment runs.','quiet-state'));
}
function projectsView() {
  const root = $("content"),
    toolbar = el("div", "", "toolbar"),
    label = el("label", "Search projects"),
    input = el("input");
  input.type = "search";
  input.placeholder = "Project name or repository";
  input.setAttribute("aria-label", "Search projects");
  label.append(input);
  const summary = el(
    "p",
    `${Object.keys(data.projects).length} projects · ${Object.values(data.projects).filter((p) => p.automatic).length} with ${data.preview ? "proposed auto-deploy" : "auto-deploy"} on`,
    "hint",
  );
  toolbar.append(label, summary);
  const list = el("div");
  const update = () => {
    const items = Object.entries(data.projects).filter(([n, p]) =>
      (n + " " + p.label + " " + p.repository)
        .toLowerCase()
        .includes(input.value.toLowerCase()),
    );
    list.replaceChildren(
      items.length
        ? projectsTable(items)
        : empty(
            "No projects found",
            "Try a project name or GitHub repository.",
          ),
    );
  };
  input.addEventListener("input", update);
  root.append(toolbar, list);
  update();
}
function branchOptions(name, select, p) {
  const observation = data.snapshots["branches:" + name],
    branches = observation?.value.branches || {},
    selected = select.value || p.branch;
  select.replaceChildren();
  const entries = Object.keys(branches).sort();
  if (!entries.length) {
    const option = el("option", "Branches not loaded");
    option.value = "";
    select.append(option);
  }
  for (const branch of entries) {
    const option = el("option", branch);
    option.value = branch;
    select.append(option);
  }
  if (entries.includes(selected)) select.value = selected;
  else if(entries.length&&selected){const missing=el("option",selected+" (unavailable)");missing.value=selected;missing.disabled=true;select.append(missing);select.value=selected;}
  select.disabled = !entries.length;
  return observation;
}
function serviceGroup(items, project) {
  const group = el("div", "", "service-grid");
  if (!items.length)
    return empty(
      "No services observed",
      "Check the project mapping or wait for the next observation.",
    );
  for (const c of items) {
    const card = el("article", "", "service-card");
    const head = el("div", "", "service-head");
    head.append(
      el("strong", c.Names.replace(/-1$/, ""), "identifier"),
      badge(
        c.Status.includes("unhealthy")
          ? "Unhealthy"
          : c.Status.includes("(healthy)")
            ? "Healthy"
            : c.State,
      ),
    );
    const image = el("code", c.Image, "service-image");
    card.append(head, image);
    const facts = el("div", "", "service-facts");
    facts.append(
      el("span", "CPU " + (c.CPUPerc || "—")),
      el("span", "Memory " + (c.MemUsage || "—")),
    );
    card.append(facts);
    if(project){const manage=button("Manage",()=>openContainer(project,c.Names),"secondary manage-container");manage.id="manage-"+project+"-"+c.Names;card.append(manage);}
    group.append(card);
  }
  return group;
}
function projectView(name) {
  const p = data.projects[name];
  if (!p)
    return $("content").append(
      empty("Project not found", "This project is not configured in this console."),link("View projects","#projects"),
    );
  const root = $("content"),
    summary = el("div", "", "project-summary");
  const repo = link(p.repository, "https://github.com/" + p.repository);
  repo.target = "_blank";
  repo.rel = "noopener";
  const identity = el("div");
  identity.append(repo, el("p", "Production branch: " + p.branch, "hint"));
  const mode=button(p.automatic?"Auto-deploy on":"Auto-deploy off",()=>{projectTabs[name]="settings";render(true);},"mode-control");identity.append(mode);
  summary.append(
    identity,
    button(
      "Build & deploy",
      () => openBuild(name, p.branch),
      "primary",
      !p.build_ready,
    ),
  );
  root.append(summary);
  const services=projectImages(p),running=services.filter(c=>c.State==='running').length;
  const summaryLine=el('div','','project-health');summaryLine.append(badge(stale('containers')?'Unavailable':!services.length?'No services observed':running===services.length&&!services.some(c=>c.Status.includes('unhealthy'))?(services.every(c=>c.Status.includes('(healthy)'))?'Healthy':'Running'):'Needs attention'),el('span',running+' of '+services.length+' services running'));
  root.append(summaryLine);
  if(!p.build_ready)root.append(el('p','Build setup pending. Container management is available below.','build-availability'));
  const tabs = el("div", "", "project-tabs");
  tabs.setAttribute("role", "tablist");
  const current = projectTabs[name] || "services";
  for (const [key, label] of [
    ["services", "Services (" + projectImages(p).length + ")"],
    ["runs", "Runs"],
    ["settings", "Settings"],
  ]) {
    const t = button(
      label,
      () => {
        projectTabs[name] = key;
        render(true);
      },
      "project-tab",
    );
    t.setAttribute("role", "tab");
    t.setAttribute("aria-selected", String(current === key));
    tabs.append(t);
  }
  tabs.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    const options=[...tabs.children],index=options.indexOf(document.activeElement);if(index<0)return;
    event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?options.length-1:(index+(event.key==='ArrowRight'?1:-1)+options.length)%options.length;
    options[next].click();document.querySelector('.project-tabs [aria-selected=true]')?.focus();
  });
  for(const t of tabs.children)t.tabIndex=t.getAttribute('aria-selected')==='true'?0:-1;
  root.append(tabs);
  if (current === "services") {
    root.append(serviceGroup(projectImages(p), name));
  } else if (current === "runs") {
    root.append(jobTable(data.jobs.filter((j) => j.project === name)));
  } else {
    const panel = el("div", "", "panel panel-body settings-panel");
    panel.append(
      el("h2", "Deployment settings"),
      el(
        "p",
        "The tracked branch controls GitHub push deployments. A manual build can use another branch without changing this setting.",
        "hint",
      ),
    );
    const label = el("label", "Tracked branch");
    label.htmlFor = "branch-select";
    const select = el("select");
    select.id = "branch-select";
    const draft=settingsDrafts[name];
    branchOptions(name, select, p);
    if(draft&&[...select.options].some(o=>o.value===draft.branch))select.value=draft.branch;
    else if(draft){const missing=el('option',draft.branch+' (unavailable)');missing.value=draft.branch;missing.disabled=true;select.append(missing);select.value=draft.branch;}
    const row = el("div", "", "form-row");
    row.append(label, select);
    panel.append(row);
    const checkLabel = el("label", "", "checkbox"),
      check = el("input");
    check.type = "checkbox";
    check.id = "auto-deploy";
    check.checked = draft ? draft.automatic : !!p.automatic;
    check.disabled = false;
    checkLabel.append(
      check,
      document.createTextNode(
        "Deploy automatically when this branch receives a push",
      ),
    );
    panel.append(checkLabel);
    if(!p.build_ready)panel.append(el('p','Automatic deployment is not connected for this project yet. Its running services remain manageable. Saving a branch does not complete the server build configuration.','notice'));
    const draftStatus=el('p',draft?'Unsaved settings':'All changes saved','draft-status');
    const remember=()=>{if(select.value===p.branch&&check.checked===!!p.automatic){delete settingsDrafts[name];draftStatus.textContent='All changes saved';}else{settingsDrafts[name]={branch:select.value,automatic:check.checked};draftStatus.textContent='Unsaved settings';}};
    select.addEventListener('change',remember);check.addEventListener('change',remember);
    panel.append(draftStatus);
    const actions = el("div", "", "actions");
    actions.append(
      button(
        "Save settings",
        async () => {
          try {
            await action({
              action: "settings",
              project: name,
              branch: select.value,
              automatic: check.checked,
            });
          } catch (e) {
            toast(e.message);
          }
        },
        "primary",
        !select.value,
      ),
      button("Refresh branches", async () => {
        try {
          await action({ action: "refresh-branches", project: name });
        } catch (e) {
          toast(e.message);
        }
      }),
    );
    actions.append(button('Discard changes',()=>{delete settingsDrafts[name];render(true);},'secondary'));
    panel.append(
      actions,
      el("p", "Saving settings does not start a deployment.", "hint"),
    );
    root.append(panel);
  }
}
function jobsView() {
  const toolbar = el("div", "", "toolbar"),
    filters = el("div", "", "filters"),
    project = el("select"),
    status = el("select");
  project.setAttribute("aria-label", "Filter runs by project");
  status.setAttribute("aria-label", "Filter runs by status");
  for (const [value, text] of [
    ["", "All projects"],
    ...Object.entries(data.projects).map(([n, p]) => [n, p.label || n]),
  ]) {
    const o = el("option", text);
    o.value = value;
    project.append(o);
  }
  for (const [value, text] of [
    ["", "All statuses"],
    ["active", "Active and queued"],
    ["failed", "Failed or needs retry"],
    ["succeeded", "Deployed"],
    ["validated", "Validated only"],
  ]) {
    const o = el("option", text);
    o.value = value;
    status.append(o);
  }
  filters.append(formField("Project",project),formField("Status",status));
  toolbar.append(
    filters,
    el("p", "Latest 100 runs · retained for 30 days", "hint"),
  );
  const list = el("div"),
    update = () =>
      list.replaceChildren(
        jobTable(
          data.jobs.filter(
            (j) =>
              (!project.value || j.project === project.value) &&
              (!status.value ||
                (status.value === "active"
                  ? activeStates.includes(j.status)
                  : status.value === "failed"
                    ? retryStates.includes(j.status)
                    : j.status === status.value)),
          ),
        ),
      );
  project.addEventListener("change", update);
  status.addEventListener("change", update);
  $("content").append(toolbar, list);
  update();
}
function jobView(id) {
  const j=data.jobs.find(j=>j.id===Number(id)),root=$('content');
  if(!j){root.append(empty('Run not found','This run is not in the recent history.'),link('View recent runs','#jobs'));return;}
  const p=data.projects[j.project],events=data.events.filter(e=>e.job_id===j.id).sort((a,b)=>a.created-b.created);
  const waiting=['awaiting-validation','auto-deploy-disabled'].includes(j.status);
  const hero=el('section','','run-overview');
  const heading=el('div','','run-heading');heading.append(link(projectLabel(j.project),'#project/'+j.project),badge(j.status));
  const descriptions={
    'awaiting-validation':p.build_ready?'This push was recorded under the previous deployment policy. Start a deployment below.':'This push was received, but this project has no build recipe yet. No deployment has started.',
    'auto-deploy-disabled':'Automatic deployment is turned off for this project. You can deploy manually or turn it on in project settings.',
    queued:'Queued for the deployment worker. This run starts automatically when the worker is available.',
    'validation-queued':'Waiting to build and test. This run will not change production.',
    building:'Building Docker images from the recorded Git commit.',testing:'Checking the candidate images before deployment.',deploying:'Updating project services and checking their runtime health.',
    succeeded:'The deployment finished and runtime checks passed.',validated:'Build and tests passed. Production has not changed.',
    superseded:'A newer push replaced this queued run. Open the project for its latest deployment.',cancelled:'This run was cancelled before execution.',
    'rolled-back':'Deployment checks failed. The previous images were restored.',recovering:'Restoring the previous deployment after a failed runtime check.'
  };
  hero.append(heading,el('h2',waiting?'Deployment has not started':stateNames[j.status]||j.status),el('p',descriptions[j.status]||j.detail||'Review the recorded result and retry when the cause is resolved.'));
  const facts=el('dl','','run-facts');
  for(const [label,value] of [['Branch',j.branch||p.branch],['Commit',j.sha.slice(0,12)],['Triggered by',j.actor||'GitHub'],['Created',date(j.created)]]){
    const pair=el('div'),valueNode=el('dd',value);if(label==='Commit'){const commitLink=link(value,'https://github.com/'+p.repository+'/commit/'+j.sha);commitLink.target='_blank';commitLink.rel='noopener';valueNode.replaceChildren(commitLink);valueNode.title=j.sha;}
    pair.append(el('dt',label),valueNode);facts.append(pair);
  }
  hero.append(facts);
  const actions=el('div','','run-actions');
  if(waiting||j.status==='validated'||retryStates.includes(j.status)){
    if(p.build_ready)actions.append(button(waiting?'Build & deploy':j.status==='validated'?'Deploy from this branch':j.mode==='validate'?'Retry build & tests':'Retry deployment',()=>openBuild(j.project,j.branch||p.branch,j.id),'primary'));
    else actions.append(el('p','Build setup required: image definitions, test commands and service mapping are missing.','notice'));
  }
  if(['queued','validation-queued', 'awaiting-validation','auto-deploy-disabled'].includes(j.status))actions.append(button('Cancel run',async()=>{try{await action({action:'cancel',project:j.project,job_id:j.id});}catch(e){toast(e.message);}},'quiet danger'));
  actions.append(button('Project settings',()=>{projectTabs[j.project]='settings';location.hash='project/'+j.project;},'secondary'));
  hero.append(actions);root.append(hero);
  const order=j.mode==='validate'?['queued','building','testing','validated']:['queued','building','testing','deploying','succeeded'];
  const labels=j.mode==='validate'?['Queue','Build images','Run tests','Complete']:['Queue','Build images','Run tests','Deploy services','Verify'];
  const normalized=j.status==='validation-queued'?'queued':j.status;
  const idx=order.indexOf(normalized),passed=['succeeded','validated'].includes(j.status);
  const pipeline=el('section','','run-pipeline');pipeline.append(el('h2','Deployment progress'));
  const steps=el('ol','','execution-steps');
  order.forEach((stage,i)=>{
    const completed=passed||(idx>=0&&idx>i),current=idx===i&&!passed;
    const recorded=events.some(e=>e.status===stage);
    const row=el('li','','execution-step '+(completed?'complete':current?'current':''));
    const marker=el('span',String(i+1),'step-number');marker.setAttribute('aria-hidden','true');
    const text=el('div');text.append(el('strong',labels[i]),el('span',completed?'Completed':current?'In progress':recorded?'Recorded in activity':'Not started','hint'));row.append(marker,text);steps.append(row);
  });pipeline.append(steps);root.append(pipeline);
  if(j.detail)root.append(el('p',j.detail,['failed','interrupted','needs-review'].includes(j.status)?'error':'notice'));
  if(j.retry_of)root.append(link('Based on run #'+j.retry_of,'#job/'+j.retry_of));
  root.append(section('Run activity'));
  const timeline=el('ol','','timeline panel');
  timeline.append(eventRow({created:j.created,status:waiting?j.status:'queued',detail:'Run created for '+(j.branch||p.branch)+' at '+j.sha.slice(0,12)+'.'}));
  events.forEach(e=>timeline.append(eventRow(e)));root.append(timeline);
}
function eventRow(e) {
  const li = el("li"),
    body = el("div");
  body.append(badge(e.status), el("p", e.detail));
  li.append(el("time", date(e.created)), body);
  return li;
}
function containersView() {
  const root = $("content"),
    toolbar = el("div", "", "toolbar"),
    label = el("label", "Find a project or service"),
    input = el("input");
  input.type = "search";
  input.placeholder = "Project, service or image";
  label.append(input);
  toolbar.append(label);
  const list = el("div");
  const update = () => {
    const query = input.value.toLowerCase();
    const entries = Object.entries(data.projects).filter(([name, p]) =>
      [
        name,
        p.label,
        p.repository,
        ...projectImages(p).map((c) => c.Names + " " + c.Image),
      ]
        .join(" ")
        .toLowerCase()
        .includes(query),
    );
    list.replaceChildren();
    if(!entries.length)list.append(empty("No matching projects", "Try a project or service name."));
    for(const [name,p] of entries){
      const projectMatch=(name+' '+p.label+' '+p.repository).toLowerCase().includes(query);
      const matching=projectImages(p).filter(c=>projectMatch||(c.Names+' '+c.Image).toLowerCase().includes(query));
      list.append(section(p.label||name,link('Open project','#project/'+name)),serviceGroup(matching,name));
    }
    const owned = new Set(
      Object.values(data.projects).flatMap((p) =>
        projectImages(p).map((c) => c.Names),
      ),
    );
    const infrastructure = containers().filter(
      (c) =>
        !owned.has(c.Names) &&
        (c.Names + " " + c.Image).toLowerCase().includes(query),
    );
    if (infrastructure.length)
      list.append(
        section("Shared infrastructure"),
        serviceGroup(infrastructure),
      );
  };
  input.addEventListener("input", update);
  root.append(toolbar, list);
  update();
}
function auditView() {
  const names = {
    login: "Sign in",
    logout: "Sign out",
    build: "Start build",
    retry: "Rebuild",
    settings: "Update deployment settings",
    cancel: "Cancel queued run",
  };
  if(data.control_runs?.length) $("content").append(section("Container operations"),table(["Time","Project / service","Operation","Result"],data.control_runs.map(r=>[date(r.created),subtitle(r.project,r.container),r.operation,subtitle(r.status,r.detail)])));
  if(!data.audit.length){$("content").append(empty("No activity recorded",data.preview?"Operator history is not included in this design preview.":"Sign-ins and management actions will appear here."));return;}
  $("content").append(
    el("p", "Administrative activity is retained for 90 days.", "hint"),
    table(
      ["Time", "User", "Run", "Project", "Details"],
      data.audit.map((a) => [
        date(a.created),
        a.actor,
        names[a.action] || a.action,
        a.project || "—",
        el("code", a.detail || "—"),
      ]),
    ),
  );
}
function render(force = false) {
  if (!data || !session) return;
  const route = location.hash.slice(1) || "overview",
    [page, rawKey, rawName] = route.split("/"),key=decodeURIComponent(rawKey||""),containerName=decodeURIComponent(rawName||"");
  if(route!==currentRoute && containerContext && Object.keys(containerContext.changes).length){confirmDraftNavigation(route);return;}
  const editing =
    document.activeElement?.matches("input,select") || document.activeElement?.closest(".select-control") || $("build-dialog").open || page==="container" || $("operation-dialog").open;
  if (!force && route === currentRoute && editing) {
    updateStatus();
    return;
  }
  const focused=!force&&route===currentRoute&&$("content").contains(document.activeElement)?document.activeElement:null;
  const focusMatch=focused?{tag:focused.tagName,id:focused.id,href:focused.getAttribute('href'),label:focused.getAttribute('aria-label'),text:focused.textContent}:null;
  // Settings drafts own branch/automatic state. Never copy generated select-search
  // inputs into unrelated fields when a live update rebuilds the page.
  const controlKey=n=>n.tagName+':'+(n.getAttribute('aria-label')||n.id||n.name||n.placeholder||'');
  const restorable=()=>[...$('content').querySelectorAll('input,select')].filter(n=>!n.closest('.select-popover')&&!n.matches('#branch-select,#auto-deploy,[type=range]'));
  const saved=new Map(!force&&route===currentRoute?restorable().map(n=>[controlKey(n),{value:n.value,checked:n.checked}]):[]);
  if(page!=="container"){closeTerminal();containerContext=null;}
  currentRoute = route;
  const titles = {
    overview: ["Overview", ""],
    projects: [
      "Projects",
      "Your production applications, each with one tracked branch.",
    ],
    jobs: [
      "Runs",
      "Follow builds, tests and deployments from source to result.",
    ],
    containers: [
      "Services",
      "All containers grouped by project. Open a project to inspect its services.",
    ],
    audit: ["Activity", "Configuration changes and operator actions."],
    project: [
      data.projects[key]?.label || key,
      "",
    ],
    container: [containerName, "Manage this service"],
    job: ["Run #" + key, ""],
  };
  const [title, description] = titles[page] || titles.overview;
  $("app").dataset.page=page;
  $("page-title").textContent = title;
  $("page-description").textContent = description;
  $("page-description").hidden = !description;
  document.title = title + " | Shiftcodes";
  $("breadcrumb").replaceChildren(link("Console", "#overview"));
  if (["project", "job"].includes(page))
    $("breadcrumb").append(
      link(
        page === "project" ? "Projects" : "Runs",
        page === "project" ? "#projects" : "#jobs",
      ),
    );
  if(page==="container")$("breadcrumb").append(link("Projects","#projects"),link(projectLabel(key),"#project/"+key));
  $("breadcrumb").hidden = !["project","container","job"].includes(page);
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if(!a.querySelector("svg"))a.prepend(uiIcon(a.dataset.nav));
    const active =
      a.dataset.nav ===
      (["project","container"].includes(page) ? "projects" : page === "job" ? "jobs" : page);
    if (active) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  $("content").replaceChildren();
  (
    ({
      overview,
      projects: projectsView,
      jobs: jobsView,
      containers: containersView,
      audit: auditView,
      project: () => projectView(key),
      container: () => containerView(key,containerName),
      job: () => jobView(key),
    })[page] || overview
  )();
  for(const n of restorable()){
    const old=saved.get(controlKey(n));if(!old)continue;
    if(n.tagName!=='SELECT'||[...n.options].some(o=>o.value===old.value))n.value=old.value;
    if(n.type==='checkbox')n.checked=old.checked;
    n.dispatchEvent(new Event(n.tagName==='SELECT'?'change':'input'));
  }
  enhanceSelects($("content"));
  if(focusMatch){const match=[...$("content").querySelectorAll('button,a,[role=tab]')].find(n=>n.tagName===focusMatch.tag&&(focusMatch.id?n.id===focusMatch.id:focusMatch.href?n.getAttribute('href')===focusMatch.href:focusMatch.label?n.getAttribute('aria-label')===focusMatch.label:n.textContent===focusMatch.text));match?.focus({preventScroll:true});}
  updateStatus();
}
function updateStatus() {
  if(!data){$("refresh").hidden=false;$("connection").textContent='Unable to load monitoring data';$("connection").dataset.stale='true';return;}
  updateMonitoringStatus();
  $("environment-note").hidden = data.environment !== "local";
  $("environment-note").textContent=data.preview?"Design preview · Captured services with proposed deployment settings. Production actions are disconnected."+(data.test_scenario?" Synthetic test: "+data.test_scenario:""):"Local preview — actions here do not change production.";
  updateObservationLabels();
  updateContainerLive();
  $("project-count").textContent = Object.keys(data.projects).length;
  const old = stale("containers");
  $("connection").textContent = data.preview?"Captured server state":old
    ? "Current status unavailable — showing stale observations"
    : "Live updates";
  $("observed").textContent = "Updated " + date(data.snapshots.containers?.updated);
  $("refresh").hidden=!old;
  $("connection").dataset.stale=String(old);
}
async function refresh(force = false) {
  if (busy || !session) return;
  busy = true;
  $("refresh").disabled = true;
  try {
    data = await api("state");
    observationFailed = false;
    $("error").hidden = true;
    render(force);
  } catch (e) {
    observationFailed = true;
    updateStatus();
    $("error").textContent = e.message;
    $("error").hidden = false;
    $("connection").textContent =
      "Refresh failed — displayed observations are stale";
  } finally {
    busy = false;
    $("refresh").disabled = false;
  }
}
async function refreshBuildBranches(name,branch,retryOf){
  $('dialog-title').textContent='Choose a build';$('build-summary').replaceChildren(el('p','Fetching current branches from GitHub…','notice'));
  $('build-submit').disabled=true;$('build-error').hidden=true;$('build-dialog').showModal();
  try{
    await api('action',{action:'refresh-branches',project:name});
    for(let attempt=0;attempt<20;attempt++){
      await new Promise(resolve=>setTimeout(resolve,1000));if(!$('build-dialog').open)return;
      data=await api('state');const observed=data.snapshots['branches:'+name];
      if(observed&&!observed.value.error&&Date.now()/1000-observed.updated<60){$('build-dialog').close();openBuild(name,branch,retryOf);return;}
    }
    throw new Error('GitHub branches are still unavailable. Close this window and try again.');
  }catch(e){$('build-error').textContent=e.message;$('build-error').hidden=false;}
}
function openBuild(name, branch, retryOf = null) {
  const p = data.projects[name],observation=data.snapshots["branches:"+name];
  if(!observation||observation.value.error||(!data.preview&&Date.now()/1000-observation.updated>600)) {
    refreshBuildBranches(name,branch,retryOf);return;
  }
  if (!p.build_ready) {
    toast("Build definitions and tests are not configured for this project.");
    return;
  }
  const requestedBranch=branch,branches=observation.value.branches||{};
  if(!Object.hasOwn(branches,branch))branch='';
  const sha=branches[branch]||'';
  buildContext = {
    project: name,
    branch,
    sha,
    retryOf,
    request_id: crypto.randomUUID(),
  };
  $("dialog-title").textContent = retryOf
    ? "Retry run #" + retryOf
    : "New build";
  const branchLabel = el("label", "Build branch");
  branchLabel.htmlFor = "build-branch";
  const branchSelect = el("select");
  branchSelect.id = "build-branch";
  branchOptions(name, branchSelect, p);
  if(!branch){const placeholder=el('option','Choose an available branch');placeholder.value='';placeholder.disabled=true;branchSelect.prepend(placeholder);}
  branchSelect.value = branch;
  const commit = el("code", sha||"Select a branch to view its commit", "build-commit");
  branchSelect.addEventListener("change", () => {
    buildContext.branch = branchSelect.value;
    buildContext.sha = observation.value.branches[branchSelect.value];
    buildContext.request_id = crypto.randomUUID();
    commit.textContent = buildContext.sha;
    updateBuildActionLabel();
  });
  $("build-summary").replaceChildren(
    el("strong", p.label || name),
    branchLabel,
    branchSelect,
    el("span", "Commit to build", "hint"),
    commit,
  );
  if(!branch)$("build-summary").append(el("p","The original branch "+requestedBranch+" is no longer available. Choose another branch to continue.","notice"));
  if (retryOf)
    $("build-summary").append(
      el(
        "p",
        "This builds the latest commit on the selected branch. It may differ from the source run; review the commit before starting.",
        "hint",
      ),
    );
  $("deploy-mode").disabled = !p.build_ready;
  const previous=data.jobs.find(j=>j.id===Number(retryOf));
  const deploy=!!p.build_ready&&!(previous?.mode==='validate'&&previous.status!=='validated');
  document.querySelector("[name=mode][value=deploy]").checked=deploy;
  document.querySelector("[name=mode][value=validate]").checked=!deploy;
  $("build-limit").textContent = p.build_ready
    ? "Deploy updates this project. This branch choice does not change the webhook settings."
    : "Deployment setup is incomplete. You can run build validation.";
  $("build-error").hidden = true;
  $("build-submit").disabled = false;
  updateBuildActionLabel();
  enhanceSelects($("build-summary"));
  $("build-dialog").showModal();
}
function updateBuildActionLabel(){
  $("build-submit").disabled=!buildContext?.sha;
  $("build-submit").textContent=document.querySelector("[name=mode]:checked")?.value==='deploy'?'Queue deployment':'Queue build & tests';
}
document.querySelectorAll('[name=mode]').forEach(input=>input.addEventListener('change',updateBuildActionLabel));
$("build-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("build-submit").disabled = true;
  $("build-error").hidden = true;
  try {
    const b = buildContext,
      result = await action({
        action: b.retryOf ? "retry" : "build",
        project: b.project,
        branch: b.branch,
        sha: b.sha,
        job_id: b.retryOf,
        request_id: b.request_id,
        mode: document.querySelector("[name=mode]:checked").value,
      });
    $("build-dialog").close();
    location.hash = "job/" + result.job_id;
    render(true);
  } catch (error) {
    $("build-error").textContent = error.message;
    $("build-error").hidden = false;
  } finally {
    updateBuildActionLabel();
  }
});
for (const id of ["dialog-close", "dialog-cancel"])
  $(id).addEventListener("click", () => $("build-dialog").close());
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-submit").disabled = true;
  $("login-error").hidden = true;
  try {
    session = await api("login", {
      username: $("username").value,
      password: $("password").value,
    });
    $("password").value = "";
    await enter();
  } catch (error) {
    $("login-error").textContent = error.message;
    $("login-error").hidden = false;
  } finally {
    $("login-submit").disabled = false;
  }
});
$("show-password").addEventListener("click", () => {
  const show = $("password").type === "password";
  $("password").type = show ? "text" : "password";
  $("show-password").textContent = show ? "Hide" : "Show";
  $("show-password").setAttribute(
    "aria-label",
    show ? "Hide password" : "Show password",
  );
});
$("logout").addEventListener("click", async () => {
  try {
    await api("action", { action: "logout" });
    showLogin();
  } catch (e) {
    toast(e.message);
  }
});
$("refresh").addEventListener("click", () => refresh(true));
window.addEventListener("hashchange", () => {
  setNavigation(false);
  render(true);
  window.scrollTo(0, 0);
});
async function enter() {
  $("boot").hidden = true;
  $("login").hidden = true;
  $("app").hidden = false;
  $("account-name").textContent = session.username;
  await refresh(true);
  startLiveUpdates();
}
(async () => {
  try {
    session = await api("session");
    await enter();
  } catch {
    showLogin();
  }
})();
function startLiveUpdates() {
  liveConnection?.close();
  liveConnection = new EventSource("./api/events");
  liveConnection.addEventListener("state", (event) => {
    data = JSON.parse(event.data);
    observationFailed = false;
    $("error").hidden = true;
    updateContainerLive();
    render();
  });
  liveConnection.addEventListener("expired", () => showLogin());
  liveConnection.onerror = () => {
    observationFailed = true;
    updateStatus();
    $("connection").textContent =
      "Connection interrupted · reconnecting automatically";
  };
}
setInterval(() => {
  if (session) updateStatus();
}, 5000);
