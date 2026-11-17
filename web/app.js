/* Job Agent dashboard – vanilla JS, hash routing */
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PAGES = { overview: "Overview", queue: "Approval Queue", applications: "Applications", resumes: "Resumes & Transcript", facts: "Career Facts", github: "GitHub", handshake: "Handshake", settings: "Settings", activity: "Activity Log" };
let currentPage = "overview";
let pollTimer = null;

// ---------- theme ----------
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch {}
  const dark = t === "dark", tg = $("#themeToggle");
  if (tg) {
    tg.setAttribute("aria-pressed", String(dark));
    tg.title = dark ? "Switch to the light theme" : "Switch to Aero dark";
    tg.setAttribute("aria-label", tg.title);
    tg.innerHTML = `<svg class="icon" style="width:16px;height:16px" aria-hidden="true"><use href="#${dark ? "i-sun" : "i-moon"}"/></svg><span>${dark ? "Light" : "Dark"}</span>`;
  }
}
let savedTheme = "light";
try { savedTheme = localStorage.getItem("theme") === "dark" ? "dark" : "light"; } catch {}
applyTheme(savedTheme);
const toggleTheme = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
$("#themeBtn").onclick = toggleTheme;
$("#themeToggle").onclick = toggleTheme;
$("#refreshBtn").onclick = () => route();

// ---------- api ----------
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}
const post = (p, body) => api(p, { method: "POST", ...(body ? { body } : {}) });

// ---------- time ----------
function toDate(iso) { if (!iso) return null; return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z"); }
function ago(iso) {
  const d = toDate(iso); if (!d) return "";
  const s = (Date.now() - d) / 1000;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} hr ago`; if (s < 86400 * 7) return `${Math.floor(s / 86400)} days ago`;
  return d.toLocaleDateString();
}
const fmtDate = (iso) => toDate(iso)?.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) ?? "";
const fmtTime = (iso) => toDate(iso)?.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) ?? "";

// ---------- UI helpers ----------
function toast(msg, ok = true) {
  const t = $("#toast");
  t.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#${ok ? "i-shield" : "i-info"}"/></svg><div>${esc(msg)}</div>`;
  t.classList.toggle("error", !ok);
  t.classList.add("show");
  t.onclick = () => t.classList.remove("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ok ? 4500 : 7000);
}
function dialog({ title, body, ok = "OK", cancel = "Cancel", icon = "i-info", caption = "Job Agent" }) {
  return new Promise((resolve) => {
    const prevFocus = document.activeElement;
    $("#dlgCaption").textContent = caption; $("#dlgTitle").textContent = title; $("#dlgBody").innerHTML = body;
    $("#dlgIcon").innerHTML = `<use href="#${icon}"/>`;
    $("#dlgOk").textContent = ok; $("#dlgCancel").textContent = cancel; $("#dlgCancel").style.display = cancel ? "" : "none";
    const ov = $("#overlay"); ov.classList.add("show"); $("#dlgOk").focus();
    const done = (v) => {
      ov.classList.remove("show"); document.removeEventListener("keydown", onKey); resolve(v);
      if (prevFocus && document.contains(prevFocus)) try { prevFocus.focus(); } catch {}
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(false);
      else if (e.key === "Tab") { // keep focus inside the dialog
        const btns = [$("#dlgOk"), $("#dlgCancel")].filter((b) => b.style.display !== "none");
        const i = btns.indexOf(document.activeElement);
        e.preventDefault(); btns[(i + (e.shiftKey ? btns.length - 1 : 1)) % btns.length].focus();
      }
    };
    document.addEventListener("keydown", onKey);
    $("#dlgOk").onclick = () => done(true); $("#dlgCancel").onclick = () => done(false);
  });
}
function meter(score) {
  if (score == null) return `<span class="muted">—</span>`;
  const cls = score >= 75 ? "" : score >= 55 ? "amber" : "red";
  return `<div class="meter-row" title="Match score ${score} of 100"><div class="meter ${cls}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${score}" aria-label="Match score"><i style="width:${score}%"></i></div><b>${score}</b></div>`;
}
const LOGO_COLORS = ["#2f74c0", "#2f9c12", "#c9701a", "#7b48a8", "#1f8a88", "#b83a5a", "#4a5f7a", "#8a7a12"];
function logo(name, size = 40) {
  const n = (name || "?").trim(); let h = 0; for (const c of n) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const c = LOGO_COLORS[h % LOGO_COLORS.length];
  const dim = size === 40 ? "" : `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.45)}px;border-radius:4px;flex-shrink:0;`;
  return `<div class="logo" aria-hidden="true" style="${dim}background:linear-gradient(180deg, ${c}cc, ${c})">${esc(n[0].toUpperCase())}</div>`;
}
const busy = (btn, on, label) => {
  if (!btn) return; btn.disabled = on; btn.setAttribute("aria-busy", String(on));
  if (on) { btn._t = btn.innerHTML; btn.innerHTML = `<span class="busy" aria-hidden="true"></span> ${label || "Working…"}`; }
  else if (btn._t) btn.innerHTML = btn._t;
};
// Run an async action with the button showing a spinner and disabled; errors become a toast.
async function withBusy(btn, label, fn) {
  if (btn?.disabled) return;
  busy(btn, true, label);
  try { return await fn(); }
  catch (e) { toast(e.message || "Something went wrong.", false); }
  finally { if (btn && document.contains(btn)) busy(btn, false); }
}

// ---------- chrome ----------
function setCrumbs(page) {
  $("#crumbs").innerHTML = `<svg class="icon" style="width:16px;height:16px"><use href="#i-briefcase"/></svg>
    <span>Job Agent</span><span class="sep">&#9656;</span><span>${PAGES[page]}</span>`;
  document.querySelectorAll("#nav a").forEach((a) => {
    const on = a.dataset.page === page;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    // On a phone the nav is a scrolling taskbar: keep the current page's button in view.
    if (on && matchMedia("(max-width: 720px)").matches) a.scrollIntoView({ inline: "nearest", block: "nearest" });
  });
}
const gmailUrl = (email) => `https://mail.google.com/mail/u/${email ? `?authuser=${encodeURIComponent(email)}` : "0/"}`;
async function checkGmail(btn) {
  await withBusy(btn, "Checking Gmail…", async () => { const r = await post("/api/gmail/check"); toast(r.message, r.ok); });
  route();
}

async function refreshChrome() {
  try {
    const o = await api("/api/overview");
    const c = $("#queueCount"); c.hidden = !o.awaiting; c.textContent = o.awaiting;
    const fc = $("#factCount"); fc.hidden = !o.facts_pending; fc.textContent = o.facts_pending;
    $("#demoBar").style.display = o.demo ? "" : "none";
    const gb = $("#gmailBadge"), n = o.interview_alerts.length;
    gb.hidden = !n; gb.textContent = n;
    $("#gmailBtn").onclick = () => window.open(gmailUrl(o.gmail_email), "_blank", "noopener");
    $("#gmailBtn").title = o.gmail_email ? `Open ${o.gmail_email} in a new tab` : "Open Gmail (connect your Handshake Gmail in Settings to open that account directly)";
    const r = o.run;
    $("#runStatus").innerHTML = r.running ? `<span class="busy"></span> ${esc(r.stage)}`
      : o.next_run ? `Next search ${fmtTime(o.next_run)}` : (r.last_run ? `Last search ${ago(r.last_run)}` : "Ready");
    $("#runBtn").disabled = r.running;
    $("#statusbar").innerHTML = `<span>${o.applied_today} of ${o.settings.daily_cap} applications today</span>
      <span>${o.settings.dry_run ? "Dry run is on (stops before Submit)" : "Live submit mode"}</span>
      <span>${o.settings.schedule_enabled ? "Schedule on" : "Schedule off"}</span>`;
    return o;
  } catch (e) { $("#statusbar").textContent = "Can't reach the agent server."; }
}
$("#runBtn").onclick = async (e) => {
  const btn = e.currentTarget;
  await withBusy(btn, "Starting…", async () => { const r = await post("/api/run"); toast(r.message, r.ok); });
  refreshChrome();
};
$("#clearDemo").onclick = async (e) => {
  const btn = e.currentTarget;
  if (await dialog({ title: "Clear the sample data?", body: "This removes the example jobs and applications. Your real data isn't touched.", ok: "Clear" })) {
    await withBusy(btn, "Clearing…", async () => { await post("/api/demo/clear"); toast("Sample data cleared."); });
    route();
  }
};
$("#globalSearch").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { sessionStorage.setItem("appFilter", e.target.value); location.hash = "#/applications"; route(); }
});

// ---------- pages ----------
const render = {};

render.overview = async () => {
  const o = await refreshChrome();
  if (!o) { view.innerHTML = `<div class="empty"><div class="big">Can't reach the agent server</div>Make sure the Job Agent is running, then click Refresh (&#8635;) in the title bar.</div>`; return; }
  const p = o.pipeline, total = Object.values(p).reduce((a, b) => a + b, 0) || 1;
  const seg = [["applied", "#3cb31a", "Applied"], ["queued", "#2f86d6", "Awaiting approval"], ["needs_manual", "#e0a316", "Needs you"], ["skipped", "#a3aeb9", "Skipped (low match)"]];
  const log = await api("/api/log?limit=8");
  const s = o.app_status;
  view.innerHTML = `
    <h1>Good ${new Date().getHours() < 12 ? "morning" : new Date().getHours() < 18 ? "afternoon" : "evening"}</h1>
    <p class="subtitle">Here's where your job search stands.</p>
    ${o.interview_alerts.length ? `<div class="interview-alert" role="status">
      <strong>${o.interview_alerts.length === 1 ? "An employer wants to talk to you" : `${o.interview_alerts.length} employers want to talk to you`}</strong>
      <ul>${o.interview_alerts.map((a) => `<li><span class="pill ${esc(a.status)}">${esc(a.status)}</span> ${esc(a.company)}, ${esc(a.title)}${a.note ? ` <span class="muted">· ${esc(a.note)}</span>` : ""}${a.link ? ` · <a href="${esc(a.link)}" target="_blank" rel="noopener">Open email</a>` : ""}</li>`).join("")}</ul></div>` : ""}
    <div class="toolbar"><button class="btn small" id="ovGmail">Check Gmail now</button>
      <span class="muted small">${o.gmail_email ? (o.gmail_checked_at ? `Last checked ${ago(o.gmail_checked_at)}` : "Not checked yet") : "Handshake Gmail not connected (Settings)"}${o.unverified ? ` · ${o.unverified} application(s) without a confirmation email` : ""}</span></div>
    <div class="gadgets">
      <div class="gadget g-green"><div class="label">Applied this week</div><div class="value">${o.applied_week}</div><div class="hint">${o.applied_total} total</div></div>
      <div class="gadget g-blue"><div class="label">Awaiting your approval</div><div class="value">${o.awaiting}</div><div class="hint"><a href="#/queue">Review now</a></div></div>
      <div class="gadget g-amber"><div class="label">Interviews</div><div class="value">${o.interviews}</div><div class="hint">from email updates</div></div>
      <div class="gadget g-teal"><div class="label">Average match</div><div class="value">${o.avg_match ?? "—"}</div><div class="hint">of jobs worth applying to</div></div>
    </div>
    <div class="grid2 mt">
      <div>
        <div class="panel"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-queue"/></svg>Pipeline</div><div class="panel-body">
          <div class="stack" role="img" aria-label="${seg.map(([k, , l]) => `${l}: ${p[k]}`).join(", ")}">${seg.map(([k, c]) => p[k] ? `<div style="flex:${p[k]};background:${c}" title="${p[k]}"></div>` : "").join("")}</div>
          <div class="legend">${seg.map(([k, c, l]) => `<span><i style="background:${c}"></i>${l}: <b>${p[k]}</b></span>`).join("")}</div>
        </div></div>
        <div class="panel mt"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-list"/></svg>Application outcomes</div><div class="panel-body">
          ${["Submitted", "Confirmed", "Interview", "Offer", "Rejected"].map((k) => `
            <div class="meter-row" style="margin:6px 0"><span style="width:90px;flex-shrink:0">${k}</span>
            <div class="meter ${k === "Rejected" ? "red" : k === "Submitted" || k === "Confirmed" ? "" : ""}"><i style="width:${o.applied_total ? (s[k] / o.applied_total) * 100 : 0}%"></i></div><b>${s[k]}</b></div>`).join("")}
        </div></div>
      </div>
      <div>
        <div class="panel"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-play"/></svg>Agent<span class="right muted small">${o.run.running ? `<span class="busy"></span> ${esc(o.run.stage)}` : "Idle"}</span></div><div class="panel-body">
          <p style="margin:0 0 10px">${o.next_run ? `Next automatic search at <b>${fmtTime(o.next_run)}</b>.` : "Automatic searches are off. Turn them on in Settings."}</p>
          ${o.run.last_error ? `<div class="note">Last run stopped: ${esc(o.run.last_error)}</div>` : ""}
          <div class="btn-row"><button class="btn primary" id="ovRun" ${o.run.running ? "disabled" : ""}>Run search now</button>
          <a class="btn" href="#/settings">Settings</a></div>
        </div></div>
        <div class="panel mt"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-pulse"/></svg>Recent activity<a class="right small" href="#/activity">See all</a></div><div class="panel-body" style="padding:4px 10px">
          ${feed(log)}
        </div></div>
      </div>
    </div>`;
  $("#ovGmail").onclick = (e) => checkGmail(e.currentTarget);
  $("#ovRun").onclick = async (e) => { await withBusy(e.currentTarget, "Starting…", async () => { const r = await post("/api/run"); toast(r.message, r.ok); }); setTimeout(route, 800); };
};

function feed(rows) {
  if (!rows.length) return `<div class="empty"><div class="big">No activity yet</div>Searches, applications and status updates will show up here.</div>`;
  const sym = { info: "i", success: "✓", warn: "!", error: "×" };
  return `<ul class="feed">${rows.map((r) => `<li><span class="lvl ${r.level}" role="img" aria-label="${esc(r.level)}">${sym[r.level] || "i"}</span>
    <time title="${esc(toDate(r.ts)?.toLocaleString())}">${ago(r.ts)}</time><span>${esc(r.message)}</span></li>`).join("")}</ul>`;
}

let batchTimer = null;
render.queue = async () => {
  const [jobs, o] = await Promise.all([api("/api/jobs?status=queued,needs_manual"), api("/api/overview")]);
  refreshChrome();
  const queued = jobs.filter((j) => j.status === "queued").sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const top = queued.slice(0, o.top_n);
  const where = (j) => [j.company, j.location].filter(Boolean).map(esc).join(" · ");
  view.innerHTML = `<h1>Approval Queue</h1>
    <p class="subtitle">The best matches from Handshake for your two resumes. Pick the ones you want, then click <b>Start</b> and the agent applies to each one.</p>
    <div class="panel"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-star"/></svg>Top ${top.length} match${top.length === 1 ? "" : "es"}
      <span class="right muted small">${o.settings.dry_run ? "Dry run: stops before each final Submit" : "Live: submits each application"}</span></div>
      ${top.length ? `<div class="panel-body flush"><table class="listview toplist"><thead><tr>
          <th class="col-check"><label class="check"><input type="checkbox" id="selAll" checked aria-label="Select all top matches"><span class="sr-only selall-text">Select all</span></label></th>
          <th>Job</th><th class="col-resume">Resume</th><th class="col-match">Match</th></tr></thead><tbody>
          ${top.map((j) => `<tr>
            <td class="col-check"><input type="checkbox" class="sel" value="${j.id}" checked aria-label="${esc(`Apply to ${j.title} at ${j.company}`)}"></td>
            <td><div class="cell-title" title="${esc(j.title)}">${esc(j.title)}</div>
              <div class="cell-sub" title="${esc([j.company, j.location].filter(Boolean).join(" · "))}">${where(j)}${j.is_demo ? ' <span class="pill demo">Sample</span>' : ""}</div></td>
            <td class="col-resume"><div class="pill-row"><span class="pill slot">Resume ${esc(j.resume_slot || "?")}</span>
              ${j.tailored ? `<span class="pill tailored" title="${esc(j.tailored.branch)}">${j.tailored.reused ? "Tailored · reused" : "Tailored"}</span>` : ""}</div>
              ${j.branch ? `<div class="cell-sub" style="margin-top:3px" title="${esc(j.branch)}">${esc(j.branch)}</div>` : ""}</td>
            <td class="col-match">${meter(j.score)}</td></tr>`).join("")}</tbody></table>
          <div class="panel-foot" id="batchArea"></div></div>`
        : `<div class="panel-body"><div class="empty" style="padding:24px 10px"><div class="big">No matches yet</div>Click <b>Run search now</b> and the agent will look through Handshake for you.</div></div>`}
      </div>
    <h2>All matches${jobs.length ? ` <span class="muted small">(${jobs.length})</span>` : ""}</h2>
    <div id="cards">${jobs.length ? "" : `<div class="empty"><div class="big">You're all caught up</div>No jobs are waiting. Run a search to find more.</div>`}</div>`;
  const wrap = $("#cards");
  for (const j of jobs) wrap.insertAdjacentHTML("beforeend", jobCard(j));
  wrap.querySelectorAll(".card-head").forEach((h) => {
    const toggle = () => { const open = h.parentElement.classList.toggle("open"); h.setAttribute("aria-expanded", String(open)); };
    h.addEventListener("click", (e) => { if (e.target.closest("button, a")) return; toggle(); });
    h.addEventListener("keydown", (e) => {
      if (e.target !== h || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault(); toggle();
    });
  });
  wrap.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => jobAction(b)));
  if (top.length) {
    const sync = () => {
      const all = [...view.querySelectorAll(".sel")], on = all.filter((c) => c.checked).length;
      all.forEach((c) => c.closest("tr").classList.toggle("unsel", !c.checked));
      const sa = $("#selAll"); sa.checked = on === all.length; sa.indeterminate = on > 0 && on < all.length;
      drawBatch(o);
    };
    $("#selAll").onchange = (e) => { view.querySelectorAll(".sel").forEach((c) => (c.checked = e.target.checked)); sync(); };
    view.querySelectorAll(".sel").forEach((c) => (c.onchange = sync));
    // clicking anywhere on a row toggles its checkbox
    view.querySelectorAll(".toplist tbody tr").forEach((tr) => tr.addEventListener("click", (e) => {
      if (e.target.closest("input, a, button")) return;
      const c = tr.querySelector(".sel"); c.checked = !c.checked; sync();
    }));
    drawBatch(o);
  }
};

function drawBatch(o) {
  const area = $("#batchArea");
  if (!area) return;
  const b = o.batch, ids = [...view.querySelectorAll(".sel:checked")].map((c) => +c.value);
  const jobsWord = (n) => `${n} job${n === 1 ? "" : "s"}`;
  if (b.running || b.results.length) {
    const pct = b.total ? Math.round((b.done / b.total) * 100) : 0;
    area.innerHTML = `<div class="batch" aria-live="polite">
      ${b.running ? `<div class="meter-row"><div class="meter" role="progressbar" aria-label="Applications done" aria-valuemin="0" aria-valuemax="${b.total}" aria-valuenow="${b.done}"><i style="width:${pct}%"></i></div><b>${b.done}/${b.total}</b></div>
        <p class="batch-status"><span class="busy" aria-hidden="true"></span><span>${esc(b.current || "Starting…")}</span></p>`
        : `<div><b>Finished.</b> <span class="muted">${b.results.filter((r) => r.ok).length} of ${b.results.length} went through.</span></div>`}
      ${b.results.length ? `<ul class="feed">${b.results.map((r) => `<li><span class="lvl ${r.ok ? "success" : "warn"}" role="img" aria-label="${r.ok ? "Done" : "Needs attention"}">${r.ok ? "✓" : "!"}</span>
        <span><b>${esc(r.title)}</b> <span class="muted">${esc(r.company)}</span><br>${esc(r.message)}</span></li>`).join("")}</ul>` : ""}
      <div class="btn-row">${b.running ? `<button class="btn" id="batchStop">Stop after this one</button>`
        : `<button class="btn green" id="batchStart" ${ids.length ? "" : "disabled"}>Start again (${ids.length})</button>`}</div></div>`;
  } else {
    area.innerHTML = `<div class="startbar"><button class="btn green big-start" id="batchStart" ${ids.length ? "" : "disabled"}>▶ Start applying to ${jobsWord(ids.length)}</button>
      <p class="hint">${ids.length ? "" : "<b>Tick at least one job to start.</b> "}Tailored Google Docs are made where needed, your resume is attached, and every application is logged to your Sheet. Anything the agent can't safely finish is marked "Needs you".</p></div>`;
  }
  $("#batchStop")?.addEventListener("click", (e) => withBusy(e.currentTarget, "Stopping…", async () => { const r = await post("/api/apply-batch/stop"); toast(r.message); }));
  $("#batchStart")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const ok = await dialog({
      title: `Apply to ${jobsWord(ids.length)}?`, icon: "i-shield", ok: "Start",
      body: `<p style="margin:0">${o.settings.dry_run ? "Dry run is on: the agent fills in each application and stops before the final Submit."
        : "The agent will submit each application on Handshake."} It pauses about half a minute between jobs and stops at your daily limit of ${o.settings.daily_cap}.</p>`,
    });
    if (!ok) return;
    await withBusy(btn, "Starting…", async () => {
      const r = await api("/api/apply-batch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_ids: ids }) });
      toast(r.message, r.ok);
      pollBatch();
    });
  });
  if (b.running) pollBatch();
}

function pollBatch() {
  clearTimeout(batchTimer);
  batchTimer = setTimeout(async () => {
    if (currentPage !== "queue") return;
    let o;
    try { o = await api("/api/overview"); } catch { pollBatch(); return; }
    drawBatch(o);
    if (!o.batch.running) render.queue();
  }, 3000);
}

function diffTable(edits) {
  return `<div class="diff"><div class="hdr"><div>Original</div><div>Tailored</div></div>
    ${edits.map((d) => `<div class="row"><div class="old">${esc(d.old)}</div><div class="new">${esc(d.new)}</div><div class="why">${esc(d.why)}</div></div>`).join("")}</div>`;
}

function jobCard(j) {
  const t = j.tailored;
  const docLink = t?.doc_url ? `<a class="btn small" href="${esc(t.doc_url)}" target="_blank"><svg class="icon" style="width:14px;height:14px" aria-hidden="true"><use href="#i-doc"/></svg>Open Google Doc</a>` : "";
  const resumeInfo = t ? `
      <div class="branchbox">
        <div><span class="pill branch" title="${esc(t.branch)}">${esc(t.branch)}</span>
          ${t.reused ? `Reusing the tailored Resume ${esc(t.slot)} first made for <b>${esc(t.made_for)}</b> (used ${t.uses}×).`
                     : `New one-off tailored copy of Resume ${esc(t.slot)} for this branch.`}
          ${t.doc_url ? "" : `<span class="muted">The Google Doc is created once your storage account is connected.</span>`}</div>
        <div class="muted small" style="margin-top:3px">Your base Resume ${esc(t.slot)} isn't changed. Edits you make in the Google Doc are included when the agent applies.</div>
      </div>
      ${t.edits.length ? `<div style="margin-top:8px">${diffTable(t.edits)}</div>` : ""}
      ${t.added ? `<div class="added mt"><span class="pill tailored">Added project</span> <b>${esc(t.added.title)}</b><div>• ${esc(t.added.text)}</div>
        <div class="muted small">${esc(t.added.why || "")} (from your confirmed Career Facts)</div></div>` : ""}
      ${t.rejected.length ? `<details class="mt"><summary class="muted">${t.rejected.length} edit(s) blocked by the checks (no new facts, same layout)</summary>
        <ul class="bullets">${t.rejected.map((r) => `<li>${esc(r.new)} <span class="muted">(${esc(r.problems.join(", "))})</span></li>`).join("")}</ul></details>` : ""}`
    : `<p class="muted" style="margin:4px 0">Sending your original Resume ${esc(j.resume_slot)}, unchanged.${j.branch ? ` Branch: <span class="pill branch" title="${esc(j.branch)}">${esc(j.branch)}</span>` : ""}</p>`;
  return `<div class="card" data-id="${j.id}">
    <div class="card-head" tabindex="0" aria-expanded="false" title="Show or hide details">
      ${logo(j.company)}
      <div style="min-width:0"><div class="card-title" title="${esc(j.title)}">${esc(j.title)}</div>
        <div class="card-sub"><span>${esc(j.company)}</span><span>${esc(j.location)}</span>
          <span class="pill slot">Resume ${esc(j.resume_slot || "?")}</span>${t ? `<span class="pill tailored">${t.reused ? "Tailored · reused" : "Tailored"}</span>` : ""}
          ${j.status === "needs_manual" ? `<span class="pill warn">Needs you</span>` : ""}${j.is_demo ? `<span class="pill demo">Sample</span>` : ""}
          <span class="muted">found ${ago(j.found_at)}</span></div></div>
      <div class="score">${meter(j.score)}</div>
      <div class="actions btn-row">
        ${j.status === "needs_manual"
          ? `<button class="btn small" data-act="open">Open in browser</button><button class="btn small green" data-act="mark">I applied</button>`
          : `<button class="btn small green" data-act="approve">Approve &amp; Submit</button>`}
        <button class="btn small" data-act="skip">Skip</button>
      </div>
    </div>
    <div class="card-body">
      ${j.note ? `<div class="note">${esc(j.note)}</div>` : ""}
      <div class="cols">
        <div><b>Why it fits</b><ul class="bullets">${j.reasons.map((r) => `<li>${esc(r)}</li>`).join("") || "<li class='muted'>—</li>"}</ul></div>
        <div><b>Gaps</b><ul class="bullets">${j.gaps.map((r) => `<li>${esc(r)}</li>`).join("") || "<li class='muted'>None noted</li>"}</ul></div>
      </div>
      ${j.pitch ? `<div class="mt"><b>Ready-to-paste answer</b> <button class="linkbtn" data-act="copy">Copy</button><div class="pitch mt" style="margin-top:6px">${esc(j.pitch)}</div></div>` : ""}
      <div class="mt"><b>Resume for this application</b></div>
      <div style="margin-top:6px">${resumeInfo}</div>
      <div class="btn-row mt">
        ${docLink}
        ${t ? `<button class="btn small" data-act="fresh">Tailor fresh for this job</button><button class="btn small" data-act="original">Use original resume</button>`
            : `<button class="btn small" data-act="tailor">Tailor for this job</button>`}
        <button class="btn small" data-act="open">View posting</button>
      </div>
    </div></div>`;
}

async function jobAction(b) {
  const card = b.closest(".card"), id = card.dataset.id, act = b.dataset.act;
  const title = card.querySelector(".card-title").textContent, company = card.querySelector(".card-sub span").textContent;
  try {
    if (act === "approve") {
      const o = await api("/api/overview");
      const ok = await dialog({
        title: `Apply to ${title}?`, icon: "i-shield", ok: o.settings.dry_run ? "Attach resume" : "Submit",
        body: `<p style="margin:0">${esc(company)}</p><p class="muted" style="margin:8px 0 0">${o.settings.dry_run
          ? "Dry run is on: the agent will attach your resume in its browser window and stop before Submit."
          : "The agent will attach your resume and submit the application on Handshake."}</p>`,
      });
      if (!ok) return;
      busy(b, true, "Applying…");
      const r = await post(`/api/jobs/${id}/approve`); toast(r.message, r.ok);
    } else if (act === "skip") { busy(b, true, "Skipping…"); await post(`/api/jobs/${id}/skip`); toast("Skipped."); }
    else if (act === "tailor" || act === "fresh") {
      busy(b, true, "Tailoring…");
      const r = await post(`/api/jobs/${id}/tailor${act === "fresh" ? "?fresh=true" : ""}`); toast(r.message, r.ok);
    }
    else if (act === "original") { busy(b, true, "Switching…"); await post(`/api/jobs/${id}/use-original`); toast("Will send the original resume. The tailored Doc is kept for its branch."); }
    else if (act === "open") { await withBusy(b, "Opening…", async () => { const r = await post(`/api/jobs/${id}/open`); toast(r.message); }); return; }
    else if (act === "mark") { busy(b, true, "Saving…"); await post(`/api/jobs/${id}/mark-applied`); toast("Recorded as applied."); }
    else if (act === "copy") { await navigator.clipboard.writeText(card.querySelector(".pitch").textContent); toast("Copied."); return; }
  } catch (e) { toast(e.message, false); }
  try { await render.queue(); } catch (e) { toast(e.message, false); }
}

let appSort = { key: "applied_at", dir: -1 };
const STATUSES = ["Submitted", "Confirmed", "Interview", "Offer", "Rejected"];
render.applications = async () => {
  const data = await api("/api/applications");
  const filter = sessionStorage.getItem("appFilter") || ""; sessionStorage.removeItem("appFilter");
  const cols = [["company", "Company"], ["title", "Role"], ["applied_at", "Applied"], ["resume_slot", "Resume"], ["score", "Match"], ["status", "Status"]];
  view.innerHTML = `<h1>Applications</h1><p class="subtitle">Every application the agent has sent${data.sheet_url ? `, mirrored to your <a href="${esc(data.sheet_url)}" target="_blank" rel="noopener">Google Sheet</a>` : ""}.</p>
    <div class="toolbar"><input type="search" id="appQ" placeholder="Filter by company or role" aria-label="Filter by company or role" value="${esc(filter)}" style="width:240px;max-width:100%">
      <select id="appS" aria-label="Filter by status"><option value="">All statuses</option>${STATUSES.map((s) => `<option>${s}</option>`).join("")}</select>
      <span class="right muted" id="appCount" aria-live="polite"></span></div>
    <div class="table-wrap"><table class="listview wide-table"><thead><tr>
      ${cols.map(([k, l]) => `<th data-k="${k}" tabindex="0" title="Sort by ${l.toLowerCase()}" aria-sort="${appSort.key === k ? (appSort.dir > 0 ? "ascending" : "descending") : "none"}">${l}<span class="arrow" aria-hidden="true">${appSort.key === k ? (appSort.dir > 0 ? "▲" : "▼") : ""}</span></th>`).join("")}
      <th>Resume sent</th></tr></thead><tbody id="appRows"></tbody></table></div>`;
  const draw = () => {
    const q = $("#appQ").value.toLowerCase(), st = $("#appS").value;
    const rows = data.items.filter((a) => (!q || `${a.company} ${a.title}`.toLowerCase().includes(q)) && (!st || a.status === st))
      .sort((a, b) => ((a[appSort.key] ?? "") > (b[appSort.key] ?? "") ? 1 : -1) * appSort.dir);
    $("#appCount").textContent = `${rows.length} item${rows.length === 1 ? "" : "s"}`;
    const filtered = data.items.length && !rows.length;
    $("#appRows").innerHTML = rows.length ? rows.map((a) => `<tr>
      <td><div class="co">${logo(a.company, 24)}<span class="co-name" title="${esc(a.company)}">${esc(a.company)}</span>${a.is_demo ? ' <span class="pill demo">Sample</span>' : ""}</div></td>
      <td style="max-width:280px"><div class="ellipsis"><a href="${esc(a.url)}" target="_blank" rel="noopener" title="${esc(a.title)}">${esc(a.title)}</a></div><div class="muted small ellipsis">${esc(a.location)}</div></td>
      <td class="nowrap" title="${esc(toDate(a.applied_at)?.toLocaleString())}">${fmtDate(a.applied_at)}</td>
      <td class="nowrap"><span class="pill slot">${esc(a.resume_slot)}</span> ${a.tailored ? `<span class="pill tailored" title="${esc(a.branch)}">Tailored</span>` : ""}</td>
      <td style="width:150px">${meter(a.score)}</td>
      <td><select data-app="${a.id}" class="stSel" aria-label="${esc(`Status for ${a.title} at ${a.company}`)}">${STATUSES.map((s) => `<option ${s === a.status ? "selected" : ""}>${s}</option>`).join("")}</select>
        ${a.status === "Interview" || a.status === "Offer" ? ` <span class="pill ${esc(a.status)}">New: ${esc(a.status)}</span>` : ""}
        <div class="small">${a.email_verified ? '<span class="ok-text" title="A confirmation email was found in Gmail">&#10003; Email confirmed</span>' : '<span class="muted" title="No confirmation email found in Gmail yet">No confirmation email</span>'}${a.email_link ? ` · <a href="${esc(a.email_link)}" target="_blank" rel="noopener" title="${esc(a.email_note)}">Email</a>` : ""}</div></td>
      <td class="nowrap">${a.doc_url ? `<a href="${esc(a.doc_url)}" target="_blank" rel="noopener">Google Doc</a>` : '<span class="muted">Original</span>'}</td></tr>`).join("")
      : `<tr><td colspan="7"><div class="empty">${filtered
        ? `<div class="big">No matching applications</div>Try a different filter. <button class="linkbtn" id="clearFilter">Clear filter</button>`
        : `<div class="big">No applications yet</div>Approve jobs in the queue and they'll show up here.`}</div></td></tr>`;
    $("#clearFilter")?.addEventListener("click", () => { $("#appQ").value = ""; $("#appS").value = ""; draw(); $("#appQ").focus(); });
    view.querySelectorAll(".stSel").forEach((s) => s.onchange = async () => {
      const item = data.items.find((a) => a.id == s.dataset.app), prev = item.status;
      s.disabled = true;
      try {
        const fd = new FormData(); fd.append("status", s.value); await post(`/api/applications/${s.dataset.app}/status`, fd);
        item.status = s.value; toast("Status updated.");
      } catch (e) { s.value = prev; toast(e.message, false); }
      finally { s.disabled = false; }
    });
  };
  view.querySelectorAll("th[data-k]").forEach((th) => {
    const sort = () => {
      appSort = { key: th.dataset.k, dir: appSort.key === th.dataset.k ? -appSort.dir : 1 };
      render.applications().then(() => view.querySelector(`th[data-k="${th.dataset.k}"]`)?.focus());
    };
    th.onclick = sort;
    th.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sort(); } };
  });
  $("#appQ").oninput = draw; $("#appS").onchange = draw; draw();
  if (data.sheet_url) { $("#sheetBtn").style.display = ""; $("#sheetBtn").onclick = () => window.open(data.sheet_url); }
};

// Make a <label class="drop"> work with drag-and-drop, click and the keyboard.
function wireDrop(drop, input, upload) {
  drop.tabIndex = 0; drop.setAttribute("role", "button");
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } };
  input.onchange = () => input.files[0] && upload(input.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]); };
}
const DOC_ICON = `<svg class="icon" style="width:14px;height:14px" aria-hidden="true"><use href="#i-doc"/></svg>`;

render.resumes = async () => {
  const d = await api("/api/resumes");
  const slot = (k) => {
    const r = d.slots[k];
    const master = !r ? "" : r.master_url
      ? `<div class="btn-row mt"><a class="btn small" href="${esc(r.master_url)}" target="_blank" rel="noopener">${DOC_ICON}Open Google Doc master</a>
           <button class="btn small" data-fmt="${k}">Format check</button></div>`
      : `<div class="btn-row mt"><button class="btn small primary" data-master="${k}">Create Google Doc master</button>
           <span class="muted small">Tailored copies are made from this. Needs the storage account.</span></div>`;
    return `<div class="panel"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-doc"/></svg><span class="ellipsis">Resume ${k}${r?.label ? ` — ${esc(r.label)}` : ""}</span></div><div class="panel-body">
      ${r ? `<div class="muted small"><span title="${esc(r.filename)}">${esc(r.filename)}</span> · ${r.words} words · uploaded ${ago(r.uploaded_at)} · <b>never modified</b></div><div class="preview" tabindex="0" aria-label="Resume ${k} text preview">${esc(r.preview)}</div>${master}`
           : `<div class="empty" style="padding:18px 10px"><div class="big">No resume yet</div>Upload Resume ${k} below.</div>`}
      <div class="mt"><label class="field-label" for="label${k}">Label</label><input type="text" id="label${k}" placeholder="e.g. Software / Data" value="${esc(r?.label || "")}" style="width:100%;margin-bottom:8px">
      <label class="drop" id="drop${k}"><input type="file" accept=".docx,.pdf" hidden id="file${k}">
        <b>${r ? "Replace" : "Upload"} Resume ${k}</b><br>Drop a PDF or Word file here, or click to browse</label></div>
    </div></div>`;
  };
  view.innerHTML = `<h1>Resumes &amp; Transcript</h1><p class="subtitle">Every job is matched against both base resumes. They are never edited: tailoring happens in one-off Google Doc copies, one per job branch.</p>
    <div class="slots">${slot("A")}${slot("B")}</div>
    <div id="fmtCheck"></div>
    <div id="transcriptPanel"><div class="panel mt"><div class="panel-head">Transcript</div><div class="panel-body"><div class="loading" style="padding:20px"><span class="busy"></span> Loading…</div></div></div></div>
    <h2>Tailored resumes by branch</h2>
    <p class="muted" style="margin-top:-4px">A tailored copy is made the first time a job in a new branch comes up, then reused for later jobs in the same branch.</p>
    ${d.tailored.length ? `<div class="table-wrap"><table class="listview mid-table"><thead><tr><th>Branch</th><th>Based on</th><th>First made for</th><th>Edits</th><th>Used</th><th>Files</th></tr></thead><tbody>
      ${d.tailored.map((t) => `<tr><td><span class="pill branch" title="${esc(t.branch)}">${esc(t.branch)}</span></td><td class="nowrap"><span class="pill slot">Resume ${esc(t.slot)}</span></td>
      <td>${esc(t.made_for)}</td><td>${t.edits.length}</td><td class="nowrap">${t.uses}×</td>
      <td class="nowrap">${t.doc_url ? `<a href="${esc(t.doc_url)}" target="_blank" rel="noopener">Google Doc</a>` : '<span class="muted">Doc pending</span>'}${t.has_pdf ? ` · <a href="/api/tailored/${t.id}/pdf" target="_blank" rel="noopener">PDF</a>` : ""}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty" style="border:1px dashed var(--line);border-radius:4px;padding:24px 10px"><div class="big">No tailored resumes yet</div>They'll appear here once the agent creates them.</div>`}`;
  view.querySelectorAll("[data-master]").forEach((b) => b.onclick = async () => {
    busy(b, true, "Creating…");
    try {
      const r = await post(`/api/resumes/${b.dataset.master}/master`); toast(r.message, r.ok);
      if (r.ok) { await render.resumes(); showFormatCheck(b.dataset.master); return; }
    } catch (e) { toast(e.message, false); }
    busy(b, false);
  });
  view.querySelectorAll("[data-fmt]").forEach((b) => b.onclick = () => showFormatCheck(b.dataset.fmt));
  renderTranscript().catch((e) => { const box = $("#transcriptPanel"); if (box) box.innerHTML = `<div class="panel mt"><div class="panel-head">Transcript</div><div class="panel-body"><div class="note" style="margin:0">Couldn't load the transcript: ${esc(e.message)}</div></div></div>`; });
  for (const k of ["A", "B"]) {
    const drop = $(`#drop${k}`), input = $(`#file${k}`);
    wireDrop(drop, input, async (file) => {
      const fd = new FormData(); fd.append("file", file); fd.append("label", $(`#label${k}`).value);
      drop.innerHTML = `<span class="busy"></span> Uploading ${esc(file.name)}…`; drop.setAttribute("aria-busy", "true");
      try { const r = await post(`/api/resumes/${k}`, fd); toast(r.message); } catch (e) { toast(e.message, false); }
      render.resumes();
    });
  }
};

async function renderTranscript() {
  const t = await api("/api/transcript");
  const box = $("#transcriptPanel");
  if (!box) return;
  box.innerHTML = `<div class="panel mt"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-list"/></svg>Transcript</div><div class="panel-body">
    ${t.uploaded ? `<div class="muted small">${esc(t.filename)}${t.pages ? ` · ${t.pages} page${t.pages > 1 ? "s" : ""}` : ""} · ${t.words} words read · uploaded ${ago(t.uploaded_at)}</div>
        <div class="preview" tabindex="0" aria-label="Transcript text preview">${esc(t.preview)}</div>
        <div class="btn-row mt"><a class="btn small" href="/api/transcript/file" target="_blank" rel="noopener">Open file</a><button class="btn small" id="trRemove">Remove</button></div>`
      : `<p class="muted" style="margin-top:0">No transcript uploaded yet.</p>`}
    <p class="muted small">The agent treats your transcript as a verified source: the coursework line on a tailored resume may name real courses from it. It is never edited.</p>
    <label class="drop" id="trDrop"><input type="file" accept=".pdf,.docx" hidden id="trFile">
      <b>${t.uploaded ? "Replace" : "Upload"} transcript</b><br>Drop a PDF here, or click to browse</label>
  </div></div>`;
  const drop = $("#trDrop"), input = $("#trFile");
  wireDrop(drop, input, async (file) => {
    const fd = new FormData(); fd.append("file", file);
    drop.innerHTML = `<span class="busy"></span> Uploading ${esc(file.name)}…`; drop.setAttribute("aria-busy", "true");
    try { const r = await post("/api/transcript", fd); toast(r.message); } catch (e) { toast(e.message, false); }
    renderTranscript();
  });
  $("#trRemove")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (await dialog({ title: "Remove the transcript?", body: "The agent will stop using its courses for tailoring.", ok: "Remove" })) {
      await withBusy(btn, "Removing…", () => api("/api/transcript", { method: "DELETE" }));
      renderTranscript();
    }
  });
}

render.facts = async () => {
  const facts = await api("/api/facts");
  const pending = facts.filter((f) => !f.confirmed), done = facts.filter((f) => f.confirmed);
  const fits = (f) => f.kind !== "project" || !f.fits ? "" : Object.entries(f.fits).map(([s, c]) =>
    `<span class="pill ${c.ok ? "fitok" : "warn"}" title="${esc(c.reason)}">Resume ${s}: ${c.ok ? "fits" : "won't fit"}</span>`).join(" ");
  const row = (f) => `<div class="fact" data-id="${f.id}">
      <div class="fact-main">
        <div class="pill-row"><span class="pill ${f.kind === "project" ? "tailored" : "slot"}">${f.kind === "project" ? "Project" : "Background"}</span>
          ${f.confirmed ? "" : '<span class="pill warn">Not used yet</span>'}
          ${fits(f)}
          <span class="muted small">${esc(f.source)} · updated ${ago(f.updated_at)}</span></div>
        ${f.kind === "project" ? `<div class="fact-title">${esc(f.title)}</div>` : ""}
        <div class="fact-text">${f.kind === "project" ? "• " : ""}${esc(f.text)}</div>
      </div>
      <div class="btn-row">
        ${f.confirmed ? "" : `<button class="btn small green" data-f="use">Use it</button>`}
        <button class="btn small" data-f="delete">Remove</button>
      </div></div>`;
  const all = [...pending, ...done];
  view.innerHTML = `<h1>Career Facts</h1>
    <p class="subtitle">Kept up to date automatically from the work you do with your AI assistant. Tailoring uses these, plus both resumes and your transcript, and never anything else.</p>
    <div class="panel"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-star"/></svg>What the agent knows about you${all.length ? `<span class="right muted small">${all.length} fact${all.length === 1 ? "" : "s"}</span>` : ""}</div><div class="panel-body" style="padding-top:2px;padding-bottom:2px">
      ${all.length ? all.map(row).join("") : '<div class="empty" style="padding:24px 10px"><div class="big">Nothing yet</div>Facts appear here as you work with your AI assistant.</div>'}</div></div>
    <div class="panel mt"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-info"/></svg>How this works</div><div class="panel-body muted">
      <ul class="bullets" style="margin:0">
        <li><b>Projects</b> can be added to a tailored resume as a short entry (title + one bullet), word for word, only when relevant and when the resume stays one page.</li>
        <li><b>Background</b> facts help tailoring reword existing bullets truthfully. They are never moved to a different employer or role.</li>
        <li>Everything in Resume A and Resume B and the courses on your transcript are always available too.</li>
        <li>Nothing to fill in here. If something is wrong or outdated, click <b>Remove</b>, or tell your AI assistant and it will update it.</li>
      </ul></div></div>`;
  view.querySelectorAll(".fact").forEach((el) => el.querySelectorAll("[data-f]").forEach((b) => b.onclick = async () => {
    const id = el.dataset.id, act = b.dataset.f;
    if (act === "delete") {
      if (!(await dialog({ title: "Remove this fact?", body: "Tailoring will stop using it.", ok: "Remove" }))) return;
      await withBusy(b, "Removing…", async () => { await api(`/api/facts/${id}`, { method: "DELETE" }); toast("Removed."); });
    } else {
      await withBusy(b, "Saving…", async () => {
        await api(`/api/facts/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmed: true }) });
        toast("Tailoring can use it now.");
      });
    }
    render.facts(); refreshChrome();
  }));
};

function showFormatCheck(k) {
  const t = Date.now();
  const fig = (name, label) => `<figure><img src="/api/resumes/${k}/preview/${name}.png?t=${t}" alt="${label}" loading="lazy"
      onerror="this.outerHTML='<div class=&quot;img-missing&quot;>Preview not available</div>'"><figcaption>${label}</figcaption></figure>`;
  $("#fmtCheck").innerHTML = `<div class="panel mt"><div class="panel-head">Format check: Resume ${k}<button class="linkbtn right" onclick="this.closest('.panel').remove()">Close</button></div>
    <div class="panel-body"><p class="muted" style="margin-top:0">Left: your original PDF. Right: the Google Doc master that tailored copies are made from. They should look the same; if anything is off, fix it once in the master Doc and every future copy inherits it.</p>
    <div class="compare">${fig("original", "Original PDF")}${fig("master", "Google Doc master")}</div></div></div>`;
  $("#fmtCheck").scrollIntoView({ behavior: "smooth" });
}

// ---------- Handshake sign-in status (Settings) ----------
function hsConnRow(h, checking = false) {
  const ok = h?.logged_in === true;
  const when = h?.checked_at ? ` · confirmed ${ago(h.checked_at)}` : "";
  let desc, btns;
  if (checking) {
    desc = `<span class="busy" aria-hidden="true"></span> Checking your Handshake sign-in…`;
    btns = "";
  } else if (h?.signin_open) {
    desc = "Signing in now in the Handshake tab. Click Done signing in there when you're finished.";
    btns = `<button class="btn small" id="hsOpenTab">Open Handshake tab</button>`;
  } else if (ok) {
    desc = `<span class="hs-confirmed">Signed in</span>${when}. The agent uses this session to search and apply. Your password is never stored.`;
    btns = `<button class="btn small" id="hsOpenTab">Open Handshake tab</button>`;
  } else {
    desc = (h?.logged_in === false ? `Not signed in${when}. ` : "") + "Sign in once from the dashboard's Handshake tab. Your password is never stored.";
    btns = `<button class="btn small primary" id="hsLogin">Sign in to Handshake</button>`;
  }
  const dot = checking ? "busy" : ok ? "on" : "off";
  return `<div class="conn"><span class="dot ${dot}" role="img" aria-label="${ok ? "Signed in" : "Not signed in"}"></span><div style="min-width:0"><div class="name">Handshake</div><div class="desc">${desc}</div></div><div class="btn-row">${btns}</div></div>`;
}
function wireHsConn() {
  $("#hsLogin")?.addEventListener("click", () => { live.autoSignIn = true; location.hash = "#/handshake"; });
  $("#hsOpenTab")?.addEventListener("click", () => { location.hash = "#/handshake"; });
}
async function confirmHandshake() {
  const box = $("#hsConn"); if (!box) return;
  box.innerHTML = hsConnRow(null, true);
  let r;
  try { r = await api("/api/handshake/status"); } catch { r = null; }
  if (!document.contains(box)) return;
  box.innerHTML = hsConnRow(r || { logged_in: null });
  wireHsConn();
  if (r?.checked && r.logged_in) notice("Signed in to Handshake", "Confirmed with the agent's browser just now. You're ready to apply.");
  else if (r?.checked && r.logged_in === false) notice("Not signed in to Handshake", "Click Sign in to Handshake to sign in from the dashboard.", false);
}
// A small slide-in notice in the corner of the window, like a system tray balloon.
function notice(title, text, ok = true) {
  let n = $("#notice");
  if (!n) { n = document.createElement("div"); n.id = "notice"; n.setAttribute("role", "status"); document.body.append(n); }
  n.className = "notice " + (ok ? "ok" : "warn");
  n.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#${ok ? "i-shield" : "i-info"}"/></svg><div><b>${esc(title)}</b><div>${esc(text)}</div></div><button class="notice-x" aria-label="Close">&#10005;</button>`;
  n.querySelector(".notice-x").onclick = () => n.classList.remove("show");
  requestAnimationFrame(() => n.classList.add("show"));
  clearTimeout(n._h); n._h = setTimeout(() => n.classList.remove("show"), 6000);
}

render.settings = async () => {
  const [s, c] = await Promise.all([api("/api/settings"), api("/api/connections").catch(() => null)]);
  const conn = (ok, name, desc, btns) => `<div class="conn"><span class="dot ${ok ? "on" : "off"}" role="img" aria-label="${ok ? "Connected" : "Not connected"}"></span><div style="min-width:0"><div class="name">${name}</div><div class="desc">${desc}</div></div><div class="btn-row">${btns}</div></div>`;
  view.innerHTML = `<h1>Settings</h1><p class="subtitle">Connect your accounts and tell the agent what to look for.</p>
    <div class="panel"><div class="panel-head">Connections</div><div class="panel-body" style="padding:0 12px">
      ${c ? [
        conn(c.anthropic, "Claude API", c.anthropic ? "API key found in .env" : "Add ANTHROPIC_API_KEY to the .env file, then restart.", ""),
        `<div id="hsConn">${hsConnRow(c.handshake)}</div>`,
        conn(c.storage.connected, "Storage Gmail (Sheet, Docs, Drive)", c.storage.connected ? `Connected as ${esc(c.storage.email || "?")}${c.storage.sheet_url ? ` · <a href="${esc(c.storage.sheet_url)}" target="_blank" rel="noopener">Open Sheet</a>` : ""}` : (c.google_client_file ? "Application log (Sheet) and tailored resumes (Google Docs)." : "Add credentials.json first (see SETUP.md)."),
          c.storage.connected ? `<button class="btn small" data-dis="storage">Disconnect</button>` : `<button class="btn small" data-con="storage" ${c.google_client_file ? "" : "disabled"}>Connect</button>`),
        conn(c.handshake_gmail.connected, "Handshake Gmail", c.handshake_gmail.connected ? `Connected as ${esc(c.handshake_gmail.email || "?")} · checks every 30 min, files confirmations under Job Agent/Submitted and interviews under Job Agent/Interviews` : "Reads application emails to update statuses and files them under Job Agent labels. Never sends or deletes mail.",
          c.handshake_gmail.connected ? `<button class="btn small" id="gmCheck">Check now</button><button class="btn small" data-dis="handshake_gmail">Disconnect</button>` : `<button class="btn small" data-con="handshake_gmail" ${c.google_client_file ? "" : "disabled"}>Connect</button>`),
      ].join("") : `<div class="empty" style="padding:20px 10px">Couldn't load connection status.</div>`}
    </div></div>

    <div class="panel mt"><div class="panel-head">Search &amp; matching</div><div class="panel-body"><div class="form">
      <label for="queries">Search terms</label><textarea id="queries" aria-describedby="queriesHelp">${esc(s.queries.join("\n"))}</textarea>
      <div class="help" id="queriesHelp">One Handshake search per line.</div>
      <label for="location">Location</label><input type="text" id="location" value="${esc(s.location)}" placeholder="Any">
      <span class="label">Remote</span><label class="check"><input type="checkbox" id="remote_ok" ${s.remote_ok ? "checked" : ""}> Include remote jobs</label>
      <label for="min_score">Minimum match</label><input type="number" id="min_score" min="0" max="100" value="${s.min_score}" style="width:90px" aria-describedby="minHelp">
      <div class="help" id="minHelp">Jobs below this score are skipped.</div>
      <label for="tailor_below">Tailor when below</label><input type="number" id="tailor_below" min="0" max="101" value="${s.tailor_below}" style="width:90px" aria-describedby="tailorHelp">
      <div class="help" id="tailorHelp">Jobs between the minimum and this score get a tailored resume.</div>
      <label for="max_new_per_run">New jobs per search</label><input type="number" id="max_new_per_run" min="1" max="100" value="${s.max_new_per_run}" style="width:90px">
      <label for="model">Claude model</label><input type="text" id="model" value="${esc(s.model)}" style="width:220px;max-width:100%">
    </div></div></div>

    <div class="panel mt"><div class="panel-head">Applying &amp; schedule</div><div class="panel-body"><div class="form">
      <span class="label">Dry run</span><label class="check"><input type="checkbox" id="dry_run" ${s.dry_run ? "checked" : ""} aria-describedby="dryHelp"> Attach the resume but stop before the final Submit</label>
      <div class="help" id="dryHelp">Keep this on for your first few applications to watch the agent work.</div>
      <label for="daily_cap">Daily limit</label><input type="number" id="daily_cap" min="1" max="100" value="${s.daily_cap}" style="width:90px">
      <span class="label">Automatic search</span><label class="check"><input type="checkbox" id="schedule_enabled" ${s.schedule_enabled ? "checked" : ""}> Search on a schedule while this PC is on</label>
      <label for="schedule_hours">Every (hours)</label><input type="number" id="schedule_hours" min="1" max="48" value="${s.schedule_hours}" style="width:90px">
    </div>
    <div class="btn-row mt"><button class="btn primary" id="saveS">Save</button><span class="muted small" id="savedMsg" aria-live="polite"></span>
      <span class="right"></span><button class="btn small" id="loadDemo">Show sample data</button></div>
    </div></div>`;

  $("#saveS").onclick = (e) => withBusy(e.currentTarget, "Saving…", async () => {
    const v = (id) => $("#" + id);
    const body = {
      queries: v("queries").value.split("\n").map((x) => x.trim()).filter(Boolean), location: v("location").value.trim(),
      remote_ok: v("remote_ok").checked, min_score: +v("min_score").value, tailor_below: +v("tailor_below").value,
      max_new_per_run: +v("max_new_per_run").value, model: v("model").value.trim(), dry_run: v("dry_run").checked,
      daily_cap: +v("daily_cap").value, schedule_enabled: v("schedule_enabled").checked, schedule_hours: +v("schedule_hours").value,
    };
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast("Settings saved."); $("#savedMsg").textContent = `Saved at ${new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
    refreshChrome();
  });
  $("#loadDemo").onclick = (e) => withBusy(e.currentTarget, "Loading…", async () => { await post("/api/demo/load"); toast("Sample data loaded."); refreshChrome(); });
  wireHsConn();
  // Confirm the sign-in on its own when the last check is old (or there's never been one).
  const hs = c?.handshake;
  const age = hs?.checked_at ? Date.now() - toDate(hs.checked_at) : Infinity;
  if (hs && !hs.signin_open && (hs.logged_in !== true || age > 10 * 60 * 1000)) confirmHandshake();
  $("#gmCheck")?.addEventListener("click", (e) => checkGmail(e.currentTarget));
  view.querySelectorAll("[data-con]").forEach((b) => b.onclick = async () => {
    busy(b, true, "Waiting for Google…"); toast("A Google sign-in page opened in your browser. Pick the right account there.");
    try { const r = await post(`/api/connect/${b.dataset.con}`); toast(r.ok ? "Connected." : r.message, r.ok); }
    catch (e) { toast(e.message, false); }
    render.settings();
  });
  view.querySelectorAll("[data-dis]").forEach((b) => b.onclick = async () => {
    if (await dialog({ title: "Disconnect this Google account?", body: "The agent will stop using it until you connect again.", ok: "Disconnect" })) {
      await withBusy(b, "Disconnecting…", () => post(`/api/disconnect/${b.dataset.dis}`));
      render.settings();
    }
  });
};

// ---------- Handshake tab: live view of the agent's browser ----------
// The agent's Chrome runs off-screen. This page shows it a few times a second and, when the agent
// isn't busy, passes your clicks, scrolling and typing through to it (for signing in, mostly).
const live = { st: null, loop: 0, queue: Promise.resolve(), text: "", textTimer: null, warned: 0, autoSignIn: false, done: null };

function liveSend(ev) {
  live.queue = live.queue.then(() => post("/api/live/input", JSON.stringify(ev))).catch((e) => {
    if (Date.now() - live.warned > 4000) { live.warned = Date.now(); toast(e.message, false); }
  });
  return live.queue;
}
function liveFlushText() { clearTimeout(live.textTimer); if (live.text) { const t = live.text; live.text = ""; liveSend({ kind: "text", text: t }); } }
const LIVE_KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "Home", "End", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "PageUp", "PageDown"]);

function liveStateLine(s) {
  if (!s.open) return `<span class="hs-dot off"></span>No Handshake browser is open. The agent opens one when it searches or applies.`;
  if (s.signin_open) return `<span class="hs-dot on"></span><b>Sign-in window</b> · click the page and sign in as usual, then click Done signing in.`;
  if (s.busy) return `<span class="hs-dot busy"></span><b>Agent at work</b> · ${esc(s.busy)}. You can watch; clicks are paused until it finishes.`;
  return `<span class="hs-dot on"></span><b>Agent's browser</b> · idle. You can click around if you need to.`;
}

function liveChrome(s) {
  live.st = s;
  const dot = $("#hsTopDot");
  if (dot) { dot.hidden = !(s.signin_open || s.busy); dot.className = "hs-top-dot" + (s.busy ? " busy" : ""); }
  if (!$("#hsLine")) return;
  $("#hsLine").innerHTML = liveStateLine(s);
  const url = s.open ? (s.url || "") : "";
  $("#hsUrl").textContent = url; $("#hsUrl").title = url;
  $("#hsTabs").innerHTML = (s.tabs || []).length > 1 ? s.tabs.map((t) =>
    `<button class="hs-tab${t.id === s.current ? " on" : ""}" data-tab="${esc(t.id)}" title="${esc(t.url)}">${esc((t.title || t.url).slice(0, 40))}</button>`).join("") : "";
  $("#hsSignIn").hidden = !!s.signin_open || s.logged_in === true;
  $("#hsBadge").hidden = !!s.signin_open || s.logged_in !== true;
  $("#hsDone").hidden = $("#hsPop").hidden = !s.signin_open;
  $("#hsSignIn").disabled = !!s.busy;
  $("#hsBack").disabled = $("#hsReload").disabled = !s.can_control;
  $("#hsScreen").classList.toggle("view-only", !s.can_control);
  $("#hsEmpty").hidden = !!s.open;
  $("#hsImg").hidden = !s.open;
}

async function liveRun(id) {
  let lastStatus = 0;
  while (live.loop === id && currentPage === "handshake") {
    if (document.hidden) { await new Promise((r) => setTimeout(r, 800)); continue; }
    try {
      if (Date.now() - lastStatus > 1500) { lastStatus = Date.now(); liveChrome(await api("/api/live/status")); }
      if (live.st?.open) {
        const res = await fetch("/api/live/frame", { cache: "no-store" });
        if (res.status === 200) {
          const url = URL.createObjectURL(await res.blob());
          const img = $("#hsImg");
          if (img && live.loop === id) { const old = img.src; img.src = url; if (old.startsWith("blob:")) URL.revokeObjectURL(old); }
          else URL.revokeObjectURL(url);
        } else if (res.status === 204) lastStatus = 0;  // the browser just closed: refresh the status line
      }
    } catch { /* server restarting: keep trying */ }
    await new Promise((r) => setTimeout(r, live.st?.open ? 300 : 1500));
  }
}

render.handshake = async () => {
  const s = await api("/api/live/status");
  view.innerHTML = `<h1>Handshake</h1>
    <p class="subtitle">A live view of the agent's own Chrome, which runs out of sight. Sign in here, or watch what it's doing.</p>
    <div class="panel hs-live">
      <div class="hs-bar">
        <button class="btn small" id="hsBack" title="Back">&#8592; Back</button>
        <button class="btn small" id="hsReload" title="Reload">Reload</button>
        <div class="hs-url" id="hsUrl"></div>
        <span class="hs-confirmed" id="hsBadge" hidden title="Confirmed with the agent's browser">Signed in</span>
        <button class="btn small primary" id="hsSignIn">Sign in to Handshake</button>
        <button class="btn small green" id="hsDone" hidden>Done signing in</button>
        <button class="btn small" id="hsPop" hidden title="Bring the sign-in window onto your screen (for a passkey or file prompt)">Pop out</button>
      </div>
      <div class="hs-tabs" id="hsTabs"></div>
      <div class="hs-screen" id="hsScreen" tabindex="0" aria-label="Live Handshake browser. Click to focus, then type.">
        <img id="hsImg" alt="Live view of the Handshake browser" draggable="false">
        <div class="hs-empty" id="hsEmpty"><div class="big">Nothing open right now</div>Click <b>Sign in to Handshake</b> to sign in, or start a search and watch it here.</div>
      </div>
      <div class="hs-foot" id="hsLine"></div>
    </div>
    <div class="hs-next" id="hsNext" hidden></div>
    <p class="hint">What you type goes straight to the Handshake page. The dashboard doesn't save or log it.</p>`;
  liveChrome(s);
  const showNext = (r) => {
    live.done = r;
    $("#hsNext").hidden = false;
    $("#hsNext").className = "hs-next " + (r.logged_in ? "ok" : "warn");
    $("#hsNext").innerHTML = r.logged_in
      ? `<b>Signed in to Handshake.</b> Pick jobs in the Approval Queue and click Start to apply.<button class="btn small green" id="hsGoQueue">Go to Approval Queue</button>`
      : `<b>Not signed in yet.</b> ${esc(r.message)}`;
    $("#hsGoQueue")?.addEventListener("click", () => { location.hash = "#/queue"; });
  };
  if (live.done) showNext(live.done);

  const signIn = (btn) => withBusy(btn, "Opening…", async () => {
    live.done = null; $("#hsNext").hidden = true;
    const r = await post("/api/handshake/login"); toast(r.message, r.ok);
    liveChrome(await api("/api/live/status")); $("#hsScreen").focus();
  });
  $("#hsSignIn").addEventListener("click", (e) => signIn(e.currentTarget));
  $("#hsDone").addEventListener("click", (e) => withBusy(e.currentTarget, "Checking sign-in…", async () => {
    const r = await post("/api/handshake/login/done"); showNext(r); refreshChrome();
    if (r.logged_in) notice("Signed in to Handshake", "Confirmed with the agent's browser. You're ready to apply."); else toast(r.message, false);
    liveChrome(await api("/api/live/status"));
  }));
  if (live.autoSignIn) { live.autoSignIn = false; if (!s.signin_open && !s.busy) signIn($("#hsSignIn")); }
  $("#hsPop").addEventListener("click", (e) => withBusy(e.currentTarget, "Opening…", async () => { const r = await post("/api/handshake/popout"); toast(r.message, r.ok); }));
  $("#hsBack").addEventListener("click", () => liveSend({ kind: "back" }));
  $("#hsReload").addEventListener("click", () => liveSend({ kind: "reload" }));
  $("#hsTabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) liveSend({ kind: "tab", id: b.dataset.tab }); });

  const screen = $("#hsScreen"), img = $("#hsImg");
  const at = (e) => { const r = img.getBoundingClientRect(); return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height }; };
  img.addEventListener("click", (e) => { screen.focus(); if (!live.st?.can_control) return; liveFlushText(); liveSend({ kind: "click", ...at(e) }); });
  img.addEventListener("wheel", (e) => {
    if (!live.st?.can_control) return;
    e.preventDefault();
    liveSend({ kind: "scroll", ...at(e), dy: e.deltaMode === 1 ? e.deltaY * 40 : e.deltaY });
  }, { passive: false });
  screen.addEventListener("keydown", (e) => {
    if (!live.st?.can_control || e.ctrlKey || e.metaKey || e.altKey) return;  // Ctrl+V reaches the paste handler
    if (e.key.length === 1) { e.preventDefault(); live.text += e.key; clearTimeout(live.textTimer); live.textTimer = setTimeout(liveFlushText, 60); }
    else if (LIVE_KEYS.has(e.key)) { e.preventDefault(); liveFlushText(); liveSend({ kind: "key", key: e.key, shift: e.shiftKey }); }
  });
  screen.addEventListener("paste", (e) => {
    if (!live.st?.can_control) return;
    e.preventDefault(); liveFlushText();
    liveSend({ kind: "text", text: e.clipboardData.getData("text") });
  });

  liveRun(++live.loop);
};

render.activity = async () => {
  const rows = await api("/api/log?limit=300");
  const html = `<h1>Activity Log</h1><p class="subtitle">Everything the agent has done, newest first. Refreshes automatically.</p>
    <div class="panel"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-pulse"/></svg>Recent events${rows.length ? `<span class="right muted small">${rows.length} shown</span>` : ""}</div>
    <div class="panel-body" style="padding:4px 12px">${feed(rows)}</div></div>`;
  if (view._activity !== html) { view.innerHTML = html; view._activity = html; } // avoid flicker on auto-refresh
};

render.github = async () => {
  const g = await api("/api/github");
  const days = new Set(g.commits.map((c) => (c.date || "").slice(0, 10))).size;
  const added = g.commits.reduce((n, c) => n + c.added, 0);
  const commitRow = (c) => `<li class="commit">
      <span class="lvl success" aria-hidden="true">✓</span>
      <time title="${esc(toDate(c.date)?.toLocaleString())}">${ago(c.date)}</time>
      <div class="commit-main">
        <div class="commit-subject">${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.subject)}</a>` : esc(c.subject)}</div>
        ${c.body ? `<div class="muted small commit-body">${esc(c.body)}</div>` : ""}
        <div class="muted small"><code>${esc(c.short)}</code> · ${c.files} file${c.files === 1 ? "" : "s"} · <span class="plus">+${c.added}</span> <span class="minus">−${c.removed}</span></div>
      </div></li>`;
  view.innerHTML = `<h1>GitHub</h1>
    <p class="subtitle">Your public record of building this agentic AI project: every update, commit by commit. Only the app's code is published; your resumes and personal data never are.</p>
    ${g.repo_url ? `<div class="panel"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-code"/></svg>Repository</div>
      <div class="panel-body"><div class="repo-row"><a class="repo-link" href="${esc(g.repo_url)}" target="_blank" rel="noopener">${esc(g.repo_url.replace(/^https:\/\//, ""))}</a>
        <div class="btn-row"><button class="btn small" id="copyRepo">Copy link</button><a class="btn small" href="${esc(g.repo_url)}" target="_blank" rel="noopener">Open on GitHub</a></div></div>
        <p class="muted small" style="margin:8px 0 0">Share this link with employers, or add it to your resume header or LinkedIn.</p></div></div>`
      : `<div class="note">${g.has_git ? "Not connected to GitHub yet. Your AI assistant will publish it once you've signed in to GitHub." : "This project isn't under version control yet."}</div>`}
    <div class="gadgets mt">
      <div class="gadget g-blue"><div class="label">Commits</div><div class="value">${g.total_commits}</div><div class="hint">total</div></div>
      <div class="gadget g-green"><div class="label">Days with updates</div><div class="value">${days}</div><div class="hint">in the last ${g.commits.length} commits</div></div>
      <div class="gadget g-teal"><div class="label">Lines added</div><div class="value">${added.toLocaleString()}</div><div class="hint">in the last ${g.commits.length} commits</div></div>
      <div class="gadget g-amber"><div class="label">Last update</div><div class="value" style="font-size:22px">${g.commits[0] ? ago(g.commits[0].date) : "—"}</div><div class="hint">${g.commits[0] ? esc(g.commits[0].short) : ""}</div></div>
    </div>
    <div class="panel mt"><div class="panel-head"><svg class="icon" aria-hidden="true"><use href="#i-pulse"/></svg>Recent updates</div>
      <div class="panel-body" style="padding:4px 12px">${g.commits.length ? `<ul class="feed commits">${g.commits.map(commitRow).join("")}</ul>`
        : `<div class="empty"><div class="big">No commits yet</div>Updates appear here as your AI assistant saves work.</div>`}</div></div>`;
  $("#copyRepo")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(g.repo_url); toast("Link copied."); } catch { toast("Couldn't copy; select the link instead.", false); }
  });
};

// ---------- movable window ----------
// Drag the title bar to move the dashboard window around the browser; drag its bottom-right corner
// to resize it. Double-click the title bar to put it back. Remembered in this browser only.
(function movableWindow() {
  const win = document.querySelector(".window"), bar = win?.querySelector(".titlebar");
  if (!win || !bar) return;
  const KEY = "jobAgentWindow";
  const wide = () => matchMedia("(min-width: 761px)").matches;
  let pos = { x: 0, y: 0, w: 0, h: 0 };
  try { pos = { ...pos, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch {}
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(pos)); } catch {} };
  const clamp = () => {  // keep the title bar reachable
    const r = win.getBoundingClientRect(), baseX = r.left - pos.x, baseY = r.top - pos.y;
    pos.x = Math.min(Math.max(pos.x, 80 - baseX - r.width), innerWidth - 80 - baseX);
    pos.y = Math.min(Math.max(pos.y, -baseY), innerHeight - 40 - baseY);
  };
  const apply = () => {
    if (!wide()) { win.style.transform = win.style.width = win.style.height = ""; win.classList.remove("moved"); return; }
    win.style.transform = pos.x || pos.y ? `translate(${pos.x}px, ${pos.y}px)` : "";
    win.style.width = pos.w ? pos.w + "px" : "";
    win.style.height = pos.h ? pos.h + "px" : "";
    win.classList.toggle("moved", !!(pos.x || pos.y || pos.w || pos.h));
  };
  apply();
  addEventListener("resize", apply);
  bar.addEventListener("pointerdown", (e) => {
    if (!wide() || e.button !== 0 || e.target.closest("button, a, input")) return;
    e.preventDefault();
    const sx = e.clientX - pos.x, sy = e.clientY - pos.y;
    bar.setPointerCapture(e.pointerId);
    win.classList.add("dragging");
    const move = (ev) => { pos.x = ev.clientX - sx; pos.y = ev.clientY - sy; clamp(); apply(); };
    const up = () => { bar.removeEventListener("pointermove", move); win.classList.remove("dragging"); save(); };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", up, { once: true });
    bar.addEventListener("pointercancel", up, { once: true });
  });
  bar.addEventListener("dblclick", (e) => {
    if (e.target.closest("button")) return;
    pos = { x: 0, y: 0, w: 0, h: 0 }; save(); apply();
  });
  // native corner-resize: remember the size the user drags it to
  let t;
  new ResizeObserver(() => {
    if (!wide() || !win.classList.contains("resizing")) return;
    clearTimeout(t); t = setTimeout(() => { pos.w = win.offsetWidth; pos.h = win.offsetHeight; save(); win.classList.toggle("moved", true); }, 150);
  }).observe(win);
  win.addEventListener("pointerdown", (e) => {  // only count size changes that start at the corner grip
    const r = win.getBoundingClientRect();
    if (e.clientX > r.right - 18 && e.clientY > r.bottom - 18) { win.classList.add("resizing"); addEventListener("pointerup", () => setTimeout(() => win.classList.remove("resizing"), 300), { once: true }); }
  });
})();

// ---------- router ----------
let routeSeq = 0, lastPage = null;
async function route() {
  const seq = ++routeSeq;
  const page = (location.hash.match(/#\/(\w+)/) || [])[1];
  currentPage = PAGES[page] ? page : "overview";
  setCrumbs(currentPage);
  document.title = `${PAGES[currentPage]} – Job Agent`;
  view._activity = null;
  view.setAttribute("aria-busy", "true");
  // show a spinner only if the page takes a moment, so quick loads don't flicker
  const slow = setTimeout(() => { if (seq === routeSeq) view.innerHTML = `<div class="loading"><span class="busy lg" aria-hidden="true"></span>Loading ${esc(PAGES[currentPage])}…</div>`; }, 200);
  try { await render[currentPage](); }
  catch (e) {
    if (seq === routeSeq) view.innerHTML = `<div class="empty"><div class="big">Something went wrong</div>${esc(e.message)}<div class="btn-row" style="justify-content:center;margin-top:12px"><button class="btn" id="retryBtn">Try again</button></div></div>`;
    $("#retryBtn")?.addEventListener("click", route);
  } finally { clearTimeout(slow); if (seq === routeSeq) view.setAttribute("aria-busy", "false"); }
  if (seq === routeSeq && lastPage !== currentPage) { view.scrollTop = 0; lastPage = currentPage; }
  if (currentPage !== "overview") refreshChrome();
}
window.addEventListener("hashchange", route);
route();
clearInterval(pollTimer);
pollTimer = setInterval(() => {
  refreshChrome();
  if (currentPage !== "handshake") api("/api/live/status").then((s) => { live.st = s; liveChrome(s); }).catch(() => {});
  if (currentPage === "activity") render.activity().catch(() => {});
}, 6000);
