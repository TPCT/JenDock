"use strict";
const $ = (id) => document.getElementById(id);
const stateNames = {
  queued: "في الانتظار",
  "validation-queued": "اختبار في الانتظار",
  building: "جارٍ البناء",
  testing: "جارٍ الاختبار",
  deploying: "جارٍ النشر",
  recovering: "جارٍ الاستعادة",
  "rolled-back": "استُعيدت النسخة السابقة",
  succeeded: "اكتمل النشر",
  validated: "نجح اختبار البناء",
  failed: "فشل",
  superseded: "يوجد تعديل أحدث",
  "awaiting-validation": "بانتظار اعتماد البناء",
  "needs-review": "مراجعة ترحيل البيانات",
  interrupted: "توقفت المهمة",
  cancelled: "أُلغيت",
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
  n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const date = (v) =>
  v
    ? new Intl.DateTimeFormat("ar-EG", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(v * 1000))
    : "لم تصل قراءة";
const badge = (status) =>
  el(
    "span",
    stateNames[status] || status,
    "badge " +
      (["succeeded", "validated", "سليمة", "مفعّل"].includes(status)
        ? "good"
        : ["failed", "interrupted", "تعذر الرصد"].includes(status)
          ? "bad"
          : "pending"),
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
    throw new Error("تعذر الاتصال باللوحة. راجع اتصالك وأعد المحاولة.");
  }
  const value = await r.json();
  if (!r.ok) {
    if (r.status === 401 && path !== "login") showLogin();
    throw new Error(value.error || "تعذر تنفيذ الطلب. حاول مرة أخرى.");
  }
  return value;
}
function showLogin() {
  session = null;
  $("app").hidden = true;
  $("boot").hidden = true;
  $("login").hidden = false;
  if ($("build-dialog").open) $("build-dialog").close();
  document.title = "تسجيل الدخول | Shiftcodes";
}
async function action(body) {
  if (actionBusy) throw new Error("انتظر اكتمال الطلب الحالي.");
  actionBusy = true;
  try {
    const result = await api("action", body);
    toast(result.message || "تم التنفيذ.");
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
      j.branch || "فرع السجل السابق",
      "#project/" + j.project,
    ),
    el("code", j.sha.slice(0, 12)),
    badge(j.status),
    j.mode === "validate" ? "بناء واختبار" : "نشر",
    date(j.updated),
    j.actor || "github",
  ]);
}
function jobTable(jobs) {
  return jobs.length
    ? table(
        [
          "العملية",
          "المشروع / الفرع",
          "Commit",
          "الحالة",
          "النوع",
          "آخر تحديث",
          "بواسطة",
        ],
        jobRows(jobs),
      )
    : empty(
        "لا توجد عمليات مطابقة",
        "ابدأ من صفحة المشروع أو انتظر وصول تعديل من GitHub.",
      );
}
function resources() {
  const n = el("div", "", "resources");
  const disk = data.snapshots.disk,
    res = data.snapshots.resources,
    worker = data.snapshots.worker;
  const values = [
    [
      "عامل النشر",
      worker?.value.state === "running" && !stale("worker")
        ? "متاح"
        : "يحتاج مراجعة",
      worker?.value.state === "unavailable"
        ? "تعذر رصد Docker"
        : date(worker?.updated),
      null,
      "worker",
    ],
    [
      "المساحة المتاحة",
      disk ? (disk.value.free / 1073741824).toFixed(1) + " GiB" : "—",
      disk
        ? "من " + (disk.value.total / 1073741824).toFixed(1) + " GiB"
        : "لم تصل بيانات",
      disk ? 100 * (1 - disk.value.free / disk.value.total) : null,
      "disk",
    ],
    [
      "الذاكرة المتاحة",
      res
        ? (res.value.memory.MemAvailable / 1073741824).toFixed(1) + " GiB"
        : "—",
      res
        ? "من " + (res.value.memory.MemTotal / 1073741824).toFixed(1) + " GiB"
        : "لم تصل بيانات",
      res
        ? 100 * (1 - res.value.memory.MemAvailable / res.value.memory.MemTotal)
        : null,
      "resources",
    ],
    [
      "حمل السيرفر",
      res ? res.value.load_1m.toFixed(2) : "—",
      res ? "متوسط دقيقة / " + res.value.cpu_count + " CPU" : "لم تصل بيانات",
      null,
      "resources",
    ],
  ];
  for (const [label, value, note, percent, key] of values) {
    const part = el("div", "", "resource");
    const metric = el("strong", value);
    if (key === "worker") observedText(metric, key, value, "غير مؤكد");
    part.append(
      el("span", label),
      metric,
      observedText(
        el("small"),
        key,
        note,
        "قراءة قديمة أو غير متاحة · " + date(data.snapshots[key]?.updated),
      ),
    );
    if (percent !== null) {
      const bar = el("div", "", "meter"),
        progress = el("progress");
      progress.max = 100;
      progress.value = percent;
      progress.setAttribute("aria-label", label + " — نسبة المستخدم");
      bar.append(progress);
      part.append(bar);
    }
    n.append(part);
  }
  return n;
}
function projectsTable(items) {
  return table(
    ["المشروع", "فرع النشر التلقائي", "الخدمات", "آخر عملية", "التحكم"],
    items.map(([name, p]) => {
      const services = projectImages(p),
        last = data.jobs.find((j) => j.project === name);
      const count = observedText(
        el("span"),
        "containers",
        services.length
          ? `${services.filter((c) => c.State === "running" && !c.Status.includes("unhealthy")).length} / ${services.length} تعمل`
          : "لم تصل خدمات",
        "قراءة قديمة",
      );
      return [
        subtitle(p.label || name, p.repository, "#project/" + name),
        subtitle(p.branch, p.automatic ? "تلقائي مفعّل" : "تلقائي متوقف"),
        count,
        last ? badge(last.status) : el("span", "لا توجد عمليات", "hint"),
        link("إدارة المشروع", "#project/" + name),
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
    section("العمليات الجارية", link("كل العمليات", "#jobs")),
    jobTable(active),
  );
  if (problematic.length) {
    root.append(section("تحتاج متابعتك"), jobTable(problematic));
  }
  root.append(
    section(
      "مشاريع السيرفر",
      link(
        "عرض " + Object.keys(data.projects).length + " مشروعًا",
        "#projects",
      ),
    ),
    projectsTable(Object.entries(data.projects).slice(0, 8)),
  );
}
function projectsView() {
  const root = $("content"),
    toolbar = el("div", "", "toolbar"),
    label = el("label", "البحث عن مشروع"),
    input = el("input");
  input.type = "search";
  input.placeholder = "الاسم أو مستودع GitHub";
  input.setAttribute("aria-label", "البحث عن مشروع");
  label.append(input);
  const summary = el(
    "p",
    `${Object.values(data.projects).filter((p) => p.enabled).length} مشروع معتمد للنشر من أصل ${Object.keys(data.projects).length}`,
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
        : empty("لا توجد مشاريع مطابقة", "جرّب اسم الموقع أو اسم المستودع."),
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
    const option = el("option", "لم تُجلب الفروع بعد");
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
function projectView(name) {
  const p = data.projects[name];
  if (!p)
    return $("content").append(
      empty("المشروع غير موجود", "ارجع إلى قائمة المشاريع."),
    );
  const root = $("content"),
    layout = el("div", "", "project-layout"),
    main = el("div", "", "project-main"),
    side = el("div", "", "project-side");
  const panel = el("div", "", "panel panel-body");
  panel.append(el("h2", "مصدر البناء"));
  const repo = link(p.repository, "https://github.com/" + p.repository);
  repo.target = "_blank";
  repo.rel = "noopener";
  repo.className = "identifier";
  panel.append(repo);
  const row = el("div", "", "form-row"),
    label = el("label", "فرع GitHub");
  label.htmlFor = "branch-select";
  const select = el("select");
  select.id = "branch-select";
  row.append(label, select);
  panel.append(row);
  const observation = branchOptions(name, select, p),
    sha = el("p", "", "identifier"),
    observed = el(
      "p",
      observation
        ? observation.value.error
          ? "تعذر جلب الفروع؛ اضغط تحديث الفروع."
          : "آخر جلب: " + date(observation.updated)
        : "جارٍ انتظار قراءة GitHub.",
      "hint",
    );
  const updateSha = () => {
    sha.textContent =
      data.snapshots["branches:" + name]?.value.branches?.[select.value] || "";
  };
  select.addEventListener("change", updateSha);
  updateSha();
  row.append(sha, observed);
  const refreshBranches = button("تحديث الفروع", async () => {
    refreshBranches.disabled = true;
    try {
      await action({ action: "refresh-branches", project: name });
    } catch (e) {
      toast(e.message);
    } finally {
      refreshBranches.disabled = false;
    }
  });
  const build = button(
    "بناء جديد",
    () => openBuild(name, select.value),
    "primary",
    !p.build_ready || !select.value,
  );
  const actions = el("div", "", "actions");
  actions.append(build, refreshBranches);
  panel.append(actions);
  if (!p.build_ready)
    panel.append(
      el(
        "p",
        "إعداد الصور والاختبارات لهذا المشروع لم يكتمل بعد. سيظهر زر البناء متاحًا بعد تجهيزها.",
        "notice",
      ),
    );
  else if (!p.enabled)
    panel.append(
      el(
        "p",
        "يمكنك اختبار البناء. النشر على الإنتاج ينتظر اعتماد اختبارات المشروع.",
        "notice",
      ),
    );
  const settings = el("div", "", "panel panel-body");
  settings.append(
    el("h2", "النشر من GitHub"),
    el(
      "p",
      "احفظ الفرع المحدد أعلاه ليكون مصدر الـWebhook. الحفظ لا يبدأ عملية نشر.",
      "hint",
    ),
  );
  const checkLabel = el("label", "", "checkbox"),
    check = el("input");
  check.type = "checkbox";
  check.id = "auto-deploy";
  check.checked = !!p.automatic;
  check.disabled = !p.enabled;
  checkLabel.append(
    check,
    document.createTextNode("النشر تلقائيًا عند push على الفرع المحفوظ"),
  );
  settings.append(checkLabel);
  const save = button("حفظ إعداد النشر", async () => {
    save.disabled = true;
    try {
      await action({
        action: "settings",
        project: name,
        branch: select.value,
        automatic: check.checked,
      });
    } catch (e) {
      toast(e.message);
    } finally {
      save.disabled = false;
    }
  });
  settings.append(
    el("p", "الفرع المحفوظ حاليًا: " + p.branch, "hint"),
    el("div", "", "actions"),
  );
  settings.lastChild.append(save);
  main.append(
    panel,
    section("إعداد النشر التلقائي"),
    settings,
    section("سجل المشروع"),
    jobTable(data.jobs.filter((j) => j.project === name).slice(0, 15)),
  );
  const runtime = el("div", "", "panel panel-body");
  runtime.append(
    el("h2", "ما يعمل الآن"),
    observedText(
      el("p", "", "hint"),
      "containers",
      "آخر رصد · " + date(data.snapshots.containers?.updated),
      "قراءة قديمة · " + date(data.snapshots.containers?.updated),
    ),
  );
  const images = projectImages(p);
  if (!images.length)
    runtime.append(
      empty(
        "لم تُرصد خدمات",
        "قد تكون الخدمات متوقفة أو إعداد الربط لم يكتمل.",
      ),
    );
  for (const c of images) {
    const item = el("div", "", "image-row");
    item.append(
      el("strong", c.Names, "identifier"),
      el("p", c.Image, "identifier"),
      el("p", c.Status, "hint"),
    );
    runtime.append(item);
  }
  side.append(runtime);
  const policy = el("div", "", "panel panel-body");
  policy.append(
    el("h2", "قواعد التنفيذ"),
    el(
      "p",
      "بناء واحد على السيرفر في كل مرة. الاختبارات معزولة عن بيانات الإنتاج. الترحيلات الجديدة لقاعدة البيانات تتوقف للمراجعة.",
      "hint",
    ),
  );
  side.append(section("ضوابط المشروع"), policy);
  layout.append(main, side);
  root.append(layout);
}
function jobsView() {
  const toolbar = el("div", "", "toolbar"),
    filters = el("div", "", "filters"),
    project = el("select"),
    status = el("select");
  project.setAttribute("aria-label", "تصفية العمليات حسب المشروع");
  status.setAttribute("aria-label", "تصفية العمليات حسب الحالة");
  for (const [value, text] of [
    ["", "كل المشاريع"],
    ...Object.entries(data.projects).map(([n, p]) => [n, p.label || n]),
  ]) {
    const o = el("option", text);
    o.value = value;
    project.append(o);
  }
  for (const [value, text] of [
    ["", "كل الحالات"],
    ["active", "الجارية والمنتظرة"],
    ["failed", "فشلت أو تحتاج إعادة محاولة"],
    ["succeeded", "اكتمل النشر"],
    ["validated", "نجح الاختبار فقط"],
  ]) {
    const o = el("option", text);
    o.value = value;
    status.append(o);
  }
  filters.append(project, status);
  toolbar.append(
    filters,
    el("p", "آخر 100 عملية · حفظ السجل 30 يومًا", "hint"),
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
      empty(
        "العملية غير موجودة في السجل المتاح",
        "ارجع إلى العمليات لعرض أحدث المهام.",
      ),
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
        "إعادة البناء",
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
        "إلغاء الانتظار",
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
  actions.append(link("فتح المشروع", "#project/" + j.project));
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
        ? ["استلام", "بناء الصور", "اختبارات", "نتيجة الاختبار"]
        : ["استلام", "بناء الصور", "اختبارات", "نشر", "تحقق"];
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
  if (j.retry_of)
    root.append(el("p", "إعادة محاولة للعملية #" + j.retry_of, "hint"));
  root.append(section("سجل خطوات التنفيذ"));
  const events = data.events.filter((e) => e.job_id === j.id).reverse(),
    timeline = el("ol", "", "timeline panel");
  timeline.append(
    eventRow({
      created: j.created,
      status: "queued",
      detail: "سُجلت العملية وحُفظ معرّف Git.",
    }),
  );
  events.forEach((e) => timeline.append(eventRow(e)));
  root.append(
    timeline,
    el(
      "p",
      "يظهر هنا سجل مراحل التنفيذ. المخرجات الخام تبقى محمية على السيرفر لأنها قد تحتوي بيانات حساسة.",
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
  const toolbar = el("div", "", "toolbar"),
    label = el("label", "البحث في الخدمات"),
    input = el("input");
  input.type = "search";
  input.placeholder = "اسم الخدمة أو الصورة";
  input.setAttribute("aria-label", "البحث في الخدمات");
  label.append(input);
  toolbar.append(
    label,
    observedText(
      el("p", "", "hint"),
      "containers",
      "آخر رصد · " + date(data.snapshots.containers?.updated),
      "قراءة قديمة · " + date(data.snapshots.containers?.updated),
    ),
  );
  const list = el("div"),
    update = () => {
      const rows = containers().filter((c) =>
        (c.Names + " " + c.Image)
          .toLowerCase()
          .includes(input.value.toLowerCase()),
      );
      list.replaceChildren(
        rows.length
          ? table(
              ["الخدمة", "الصورة", "التشغيل", "فحص الخدمة", "CPU", "الذاكرة"],
              rows.map((c) => [
                el("span", c.Names, "identifier"),
                el("code", c.Image),
                c.State,
                c.Status,
                c.CPUPerc || "غير متاح",
                c.MemUsage || "غير متاح",
              ]),
            )
          : empty("لا توجد خدمات مطابقة", "غيّر عبارة البحث أو حدّث الحالة."),
      );
    };
  input.addEventListener("input", update);
  $("content").append(toolbar, list);
  update();
}
function auditView() {
  const names = {
    login: "تسجيل الدخول",
    logout: "تسجيل الخروج",
    build: "بدء بناء",
    retry: "إعادة البناء",
    settings: "تعديل إعداد النشر",
    cancel: "إلغاء الانتظار",
  };
  $("content").append(
    el("p", "سجل تغييرات المسؤول محفوظ لمدة 90 يومًا.", "hint"),
    table(
      ["التوقيت", "المستخدم", "العملية", "المشروع", "التفاصيل"],
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
    document.activeElement?.matches("input,select") || $("build-dialog").open;
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
    overview: ["نظرة عامة", "حالة المشاريع وعمليات النشر التي تحتاج متابعتك."],
    projects: ["المشاريع", "مصدر GitHub وإعداد النشر لكل موقع."],
    jobs: [
      "عمليات البناء والنشر",
      "تتبّع كل عملية من الفرع المختار حتى النتيجة.",
    ],
    containers: ["الخدمات", "الصور الفعلية وحالة حاويات Docker على السيرفر."],
    audit: ["سجل الإدارة", "تغييرات الإعداد وطلبات التشغيل المسجلة."],
    project: [
      data.projects[key]?.label || key,
      "اختيار المصدر، بدء البناء، ومراجعة ما يعمل الآن.",
    ],
    job: ["العملية #" + key, "خطوات التنفيذ والنتيجة المسجلة."],
  };
  const [title, description] = titles[page] || titles.overview;
  $("page-title").textContent = title;
  $("page-description").textContent = description;
  document.title = title + " | Shiftcodes";
  $("breadcrumb").replaceChildren();
  if (["project", "job"].includes(page))
    $("breadcrumb").append(
      link(
        page === "project" ? "المشاريع" : "العمليات",
        page === "project" ? "#projects" : "#jobs",
      ),
    );
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
  updateStatus();
}
function updateStatus() {
  $("environment-note").hidden = data.environment !== "local";
  updateObservationLabels();
  $("project-count").textContent = Object.keys(data.projects).length;
  const old = stale("containers");
  $("connection").textContent = old
    ? "تعذر تأكيد الحالة الحالية — البيانات السابقة قديمة"
    : "متصل · قراءة Docker حديثة";
  $("observed").textContent =
    "آخر رصد للخدمات: " + date(data.snapshots.containers?.updated);
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
    $("connection").textContent = "تعذر التحديث — البيانات المعروضة قديمة";
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
    toast("حدّث فروع GitHub من صفحة المشروع ثم أعد المحاولة.");
    return;
  }
  if (!p.build_ready) {
    toast("إعداد البناء والاختبارات غير مكتمل لهذا المشروع.");
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
    ? "إعادة بناء العملية #" + retryOf
    : "بناء جديد";
  $("build-summary").replaceChildren(
    el("strong", p.label || name),
    el("p", "الفرع: " + branch, "identifier"),
    el("code", sha),
  );
  if (retryOf)
    $("build-summary").append(
      el(
        "p",
        "ستُبنى أحدث نسخة في هذا الفرع، وقد تختلف عن النسخة التي فشلت.",
        "hint",
      ),
    );
  $("deploy-mode").disabled = !p.enabled;
  document.querySelector("[name=mode][value=validate]").checked = true;
  $("build-limit").textContent = p.enabled
    ? "النشر يغيّر خدمات هذا المشروع. اختيار الفرع هنا لا يغيّر إعداد الـWebhook."
    : "النشر على الإنتاج غير متاح حتى اعتماد الاختبارات. اختبار البناء متاح.";
  $("build-error").hidden = true;
  $("build-submit").disabled = false;
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
  $("show-password").textContent = show ? "إخفاء" : "إظهار";
  $("show-password").setAttribute(
    "aria-label",
    show ? "إخفاء كلمة المرور" : "إظهار كلمة المرور",
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
  render(true);
  window.scrollTo(0, 0);
});
async function enter() {
  $("boot").hidden = true;
  $("login").hidden = true;
  $("app").hidden = false;
  $("account-name").textContent = session.username;
  await refresh(true);
}
(async () => {
  try {
    session = await api("session");
    await enter();
  } catch {
    showLogin();
  }
})();
setInterval(() => refresh(), 15000);
