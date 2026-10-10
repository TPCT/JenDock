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
  "awaiting-validation": "Awaiting approval",
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
];
const projectTabs = {};
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
          : ["running", "exited", "created"].includes(status) ? "neutral" : "pending"),
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
  observationFailed ||
  !data?.snapshots[name] ||
  Date.now() / 1000 - data.snapshots[name].updated > 90 ||
  data.snapshots.worker?.value.state !== "running";
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
  const value = await r.json();
  if (!r.ok) {
    if (r.status === 401 && path !== "login") showLogin();
    throw new Error(value.error || "Request failed. Please try again.");
  }
  return value;
}
function showLogin() {
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
    toast(result.message || "Done.");
    await refresh(true);
    return result;
  } finally {
    actionBusy = false;
  }
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
    row.forEach((value) => {
      const td = el("td");
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
function projectsTable(items) {
  return table(
    ["Project", "Tracked branch", "Services", "Latest run", "Actions"],
    items.map(([name, p]) => {
      const services = projectImages(p),
        last = data.jobs.find((j) => j.project === name);
      const count = observedText(
        el("span"),
        "containers",
        services.length
          ? `${services.filter((c) => c.State === "running" && !c.Status.includes("unhealthy")).length} / ${services.length} running`
          : "No services observed",
        "Stale observation",
      );
      return [
        subtitle(p.label || name, p.repository, "#project/" + name),
        subtitle(p.branch, p.automatic ? "Auto-deploy on" : "Auto-deploy off"),
        count,
        last ? badge(last.status) : el("span", "No runs yet", "hint"),
        link("Open project", "#project/" + name),
      ];
    }),
  );
}
function overview() {
  const root = $("content");
  root.append(resources());
  const problematic = data.jobs.filter(
    (j) =>
      retryStates.includes(j.status) &&
      data.jobs.find((x) => x.project === j.project)?.id === j.id,
  );
  const active = data.jobs.filter((j) => activeStates.includes(j.status));
  root.append(
    section("In progress", link("View all runs", "#jobs")),
    jobTable(active),
  );
  if (problematic.length) {
    root.append(section("Needs attention"), jobTable(problematic));
  }
  root.append(
    section(
      "Projects",
      link(
        "View " + Object.keys(data.projects).length + " projects",
        "#projects",
      ),
    ),
    projectsTable(Object.entries(data.projects).slice(0, 8)),
  );
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
    `${Object.values(data.projects).filter((p) => p.enabled).length} approved for deployment out of ${Object.keys(data.projects).length}`,
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
      el("strong", c.Names, "identifier"),
      badge(
        c.Status.includes("unhealthy")
          ? "Unhealthy"
          : c.Status.includes("(healthy)")
            ? "Healthy"
            : c.State,
      ),
    );
    const image = el("code", c.Image, "service-image");
    card.append(head, image, el("p", c.Status, "hint"));
    const facts = el("div", "", "service-facts");
    facts.append(
      el("span", "CPU " + (c.CPUPerc || "—")),
      el("span", "Memory " + (c.MemUsage || "—")),
    );
    card.append(facts);
    if(project)card.append(button("Manage container",()=>openContainer(project,c.Names),"secondary manage-container"));
    group.append(card);
  }
  return group;
}
function projectView(name) {
  const p = data.projects[name];
  if (!p)
    return $("content").append(
      empty("Project not found", "Return to Projects."),
    );
  const root = $("content"),
    summary = el("div", "", "project-summary");
  const repo = link(p.repository, "https://github.com/" + p.repository);
  repo.target = "_blank";
  repo.rel = "noopener";
  const identity = el("div");
  identity.append(repo, el("p", "Tracked branch: " + p.branch, "hint"));
  summary.append(
    identity,
    button(
      "New build",
      () => openBuild(name, p.branch),
      "primary",
      !p.build_ready,
    ),
  );
  root.append(summary);
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
  root.append(tabs);
  if (current === "services") {
    root.append(
      observedText(
        el("p", "", "hint"),
        "containers",
        "Live services · " + date(data.snapshots.containers?.updated),
        "Stale service observations · " +
          date(data.snapshots.containers?.updated),
      ),
      serviceGroup(projectImages(p), name),
    );
    const recent = data.jobs.filter((j) => j.project === name).slice(0, 3);
    root.append(
      section(
        "Latest runs",
        button(
          "View all runs",
          () => {
            projectTabs[name] = "runs";
            render(true);
          },
          "quiet",
        ),
      ),
      jobTable(recent),
    );
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
    branchOptions(name, select, p);
    const row = el("div", "", "form-row");
    row.append(label, select);
    panel.append(row);
    const checkLabel = el("label", "", "checkbox"),
      check = el("input");
    check.type = "checkbox";
    check.id = "auto-deploy";
    check.checked = !!p.automatic;
    check.disabled = !p.enabled;
    checkLabel.append(
      check,
      document.createTextNode(
        "Deploy automatically when this branch receives a push",
      ),
    );
    panel.append(checkLabel);
    if (!p.enabled)
      panel.append(
        el(
          "p",
          p.build_ready
            ? "Build validation is available. Production deployment still needs approval."
            : "Build setup is incomplete. Image definitions and tests are required.",
          "notice",
        ),
      );
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
  filters.append(project, status);
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
  const j = data.jobs.find((j) => j.id === Number(id));
  if (!j)
    return $("content").append(
      empty("Run not found", "Return to Runs to view recent activity."),
    );
  const root = $("content"),
    summary = el("div", "", "job-summary"),
    info = el("div");
  info.append(
    badge(j.status),
    el(
      "p",
      `${projectLabel(j.project)} · ${j.branch || data.projects[j.project]?.branch} · ${j.actor || "github"}`,
    ),
    el("code", j.sha),
  );
  const actions = el("div", "", "actions");
  if (retryStates.includes(j.status))
    actions.append(
      button(
        "Rebuild",
        () =>
          openBuild(
            j.project,
            j.branch || data.projects[j.project].branch,
            j.id,
          ),
        "primary",
      ),
    );
  if (["queued", "validation-queued", "awaiting-validation"].includes(j.status))
    actions.append(
      button(
        "Cancel queued run",
        async () => {
          try {
            await action({
              action: "cancel",
              project: j.project,
              job_id: j.id,
            });
          } catch (e) {
            toast(e.message);
          }
        },
        "secondary danger",
      ),
    );
  actions.append(link("Open project", "#project/" + j.project));
  summary.append(info, actions);
  root.append(summary);
  const strip = el("div", "", "step-strip"),
    order =
      j.mode === "validate"
        ? ["queued", "building", "testing", "validated"]
        : ["queued", "building", "testing", "deploying", "succeeded"],
    idx = order.indexOf(j.status === "validation-queued" ? "queued" : j.status),
    labels =
      j.mode === "validate"
        ? ["Queued", "Build", "Test", "Result"]
        : ["Queued", "Build", "Test", "Deploy", "Verify"];
  labels.forEach((label, i) =>
    strip.append(
      el(
        "span",
        label,
        ["succeeded", "validated"].includes(j.status) || idx > i
          ? "done"
          : idx === i
            ? "current"
            : "",
      ),
    ),
  );
  root.append(strip);
  if (j.detail)
    root.append(
      el(
        "p",
        j.detail,
        ["failed", "interrupted"].includes(j.status) ? "error" : "notice",
      ),
    );
  if (j.retry_of) root.append(el("p", "Retry of run #" + j.retry_of, "hint"));
  root.append(section("Execution timeline"));
  const events = data.events.filter((e) => e.job_id === j.id).reverse(),
    timeline = el("ol", "", "timeline panel");
  timeline.append(
    eventRow({
      created: j.created,
      status: "queued",
      detail: "Run created and commit recorded.",
    }),
  );
  events.forEach((e) => timeline.append(eventRow(e)));
  root.append(
    timeline,
    el(
      "p",
      "The timeline records execution stages. Raw output remains protected on the server because it may contain sensitive data.",
      "hint",
    ),
  );
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
    list.replaceChildren(
      entries.length
        ? projectsTable(entries)
        : empty("No matching projects", "Try a project or service name."),
    );
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
    [page, key] = route.split("/");
  const editing =
    document.activeElement?.matches("input,select") || document.activeElement?.closest(".select-control") || $("build-dialog").open || $("container-dialog").open || $("operation-dialog").open;
  if (!force && route === currentRoute && editing) {
    updateStatus();
    return;
  }
  const saved =
    !force && route === currentRoute
      ? [...$("content").querySelectorAll("input,select")].map((n) => ({
          value: n.value,
          checked: n.checked,
        }))
      : [];
  currentRoute = route;
  const titles = {
    overview: ["Overview", "Production health and deployment activity."],
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
      "Production services, build history and deployment settings.",
    ],
    job: ["Run #" + key, "Execution stages and recorded result."],
  };
  const [title, description] = titles[page] || titles.overview;
  $("page-title").textContent = title;
  $("page-description").textContent = description;
  document.title = title + " | Shiftcodes";
  $("breadcrumb").replaceChildren(link("Console", "#overview"));
  if (["project", "job"].includes(page))
    $("breadcrumb").append(
      link(
        page === "project" ? "Projects" : "Runs",
        page === "project" ? "#projects" : "#jobs",
      ),
    );
  $("breadcrumb").append(el("span", title));
  document.querySelectorAll("[data-nav]").forEach((a) => {
    const active =
      a.dataset.nav ===
      (page === "project" ? "projects" : page === "job" ? "jobs" : page);
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
      job: () => jobView(key),
    })[page] || overview
  )();
  [...$("content").querySelectorAll("input,select")].forEach((n, i) => {
    const old = saved[i];
    if (!old) return;
    if (
      n.tagName !== "SELECT" ||
      [...n.options].some((o) => o.value === old.value)
    )
      n.value = old.value;
    if (n.type === "checkbox") n.checked = old.checked;
    n.dispatchEvent(new Event(n.tagName === "SELECT" ? "change" : "input"));
  });
  enhanceSelects($("content"));
  updateStatus();
}
function updateStatus() {
  $("environment-note").hidden = data.environment !== "local";
  updateObservationLabels();
  $("project-count").textContent = Object.keys(data.projects).length;
  const old = stale("containers");
  $("connection").textContent = old
    ? "Current status unavailable — showing stale observations"
    : "Live · updates arrive automatically";
  $("observed").textContent =
    "Services observed: " + date(data.snapshots.containers?.updated);
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
    updateObservationLabels();
    $("error").textContent = e.message;
    $("error").hidden = false;
    $("connection").textContent =
      "Refresh failed — displayed observations are stale";
  } finally {
    busy = false;
    $("refresh").disabled = false;
  }
}
function openBuild(name, branch, retryOf = null) {
  const p = data.projects[name],
    observation = data.snapshots["branches:" + name],
    sha = observation?.value.branches?.[branch];
  if (!sha || Date.now() / 1000 - observation.updated > 600) {
    toast("Refresh GitHub branches in project settings, then try again.");
    return;
  }
  if (!p.build_ready) {
    toast("Build definitions and tests are not configured for this project.");
    return;
  }
  buildContext = {
    project: name,
    branch,
    sha,
    retryOf,
    request_id: crypto.randomUUID(),
  };
  $("dialog-title").textContent = retryOf
    ? "Rebuild run #" + retryOf
    : "New build";
  const branchLabel = el("label", "Build branch");
  branchLabel.htmlFor = "build-branch";
  const branchSelect = el("select");
  branchSelect.id = "build-branch";
  branchOptions(name, branchSelect, p);
  branchSelect.value = branch;
  const commit = el("code", sha, "build-commit");
  branchSelect.addEventListener("change", () => {
    buildContext.branch = branchSelect.value;
    buildContext.sha = observation.value.branches[branchSelect.value];
    buildContext.request_id = crypto.randomUUID();
    commit.textContent = buildContext.sha;
  });
  $("build-summary").replaceChildren(
    el("strong", p.label || name),
    branchLabel,
    branchSelect,
    el("span", "Commit to build", "hint"),
    commit,
  );
  if (retryOf)
    $("build-summary").append(
      el(
        "p",
        "This builds the latest commit on the selected branch, which may differ from the failed run.",
        "hint",
      ),
    );
  $("deploy-mode").disabled = !p.enabled;
  document.querySelector("[name=mode][value=validate]").checked = true;
  $("build-limit").textContent = p.enabled
    ? "Deploy updates this project. This branch choice does not change the webhook settings."
    : "Production deployment requires approval. Build validation is available.";
  $("build-error").hidden = true;
  $("build-submit").disabled = false;
  enhanceSelects($("build-summary"));
  $("build-dialog").showModal();
}
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
    $("build-submit").disabled = false;
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
    render();
  });
  liveConnection.addEventListener("expired", () => showLogin());
  liveConnection.onerror = () => {
    observationFailed = true;
    updateObservationLabels();
    $("connection").textContent =
      "Connection interrupted · reconnecting automatically";
  };
}
setInterval(() => {
  if (session) updateObservationLabels();
}, 5000);
