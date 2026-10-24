/* ==========================================================================
   Station display - read-only view of the line for a screen next to it.
   Same live stream as the dashboard (/api/stream); no settings, no login.
   ========================================================================== */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const params = new URLSearchParams(location.search);
const LIVE = params.get("live") === "1";
const CONTROLS = params.get("controls") === "1";
const ESCAPED = new Set(["missed", "disabled", "locked"]);

const S = { status: null, results: [], alarms: null, lastSeq: -1, canControl: false, liveOn: false };

/* ------------------------------------------------------------------ stream */
function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => $("#offline").classList.add("hidden");
  es.onerror = () => { $("#offline").classList.remove("hidden"); S.liveOn = false; };
  es.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === "init") {
      S.results = m.results || [];
      S.canControl = m.can_control;
      $("#controls").classList.toggle("hidden", !(CONTROLS && S.canControl));
      document.body.classList.toggle("with-controls", CONTROLS && S.canControl);
      loadAlarms();
      onStatus(m.status);
      renderLatest();
      renderStrip();
    } else if (m.type === "status") onStatus(m.status);
    else if (m.type === "result") {
      S.results.push(m.result);
      if (S.results.length > 400) S.results.shift();
      renderLatest(); renderStrip();
    } else if (m.type === "reject") {
      const r = S.results.find((x) => x.id === m.id);
      if (r) { r.reject = m.status; renderLatest(); renderStrip(); }
    } else if (m.type === "alarms") loadAlarms();
  };
}

async function loadAlarms() {
  try { S.alarms = await (await fetch("/api/alarms")).json(); } catch { return; }
  if (S.status) renderSide(S.status);
}

/* ------------------------------------------------------------------ status */
function onStatus(st) {
  S.status = st;
  renderBanner(st);
  renderKpis(st.stats);
  renderCan(st);
  renderSide(st);
  renderChips(st);
  renderControls(st);
}

function activeAlarm(st) {
  // the alarm behind what the banner shows: the first fault, else the defect warning
  const a = S.alarms && S.alarms.active;
  if (!a || !a.length) return null;
  if (st.estop) return a.find((x) => x.key === "estop") || null;
  if (st.faults.length) return a.find((x) => x.msg === st.faults[0]) || a.find((x) => x.severity === "fault") || null;
  if (st.warning) return a.find((x) => x.key === "defect-warning") || null;
  return null;
}

function renderBanner(st) {
  let cls = "neutral", title = "STOPPED", sub = "Ready — press Start";
  const al = activeAlarm(st);
  if (st.estop) { cls = "estop"; title = "EMERGENCY STOP"; sub = "Make the line safe, then press Reset"; }
  else if (st.faults.length) {
    cls = "fault";
    title = al ? al.title.toUpperCase() : "FAULT";
    sub = st.faults[0] + (st.faults.length > 1 ? `   (+${st.faults.length - 1} more)` : "");
  } else if (st.warning) { cls = "warning"; title = "DEFECT WARNING"; sub = `${st.warning_reason} — check the cans upstream, then Acknowledge`; }
  else if (st.manual) { cls = "neutral"; title = "MANUAL OVERRIDE"; sub = "Outputs controlled by hand"; }
  else if (st.running) { cls = "running"; title = "RUNNING"; sub = `Inspecting · ${st.settings.inspector}${st.sim ? " · simulation" : ""}`; }
  else if (st.standby) { cls = "neutral"; title = "STANDBY"; sub = "Line stopped — camera resting to stay cool"; }
  else { cls = "ready"; }
  const b = $("#banner");
  b.className = `banner ${cls}`;
  $("#bTitle").textContent = title;
  $("#bSub").textContent = sub;
  $("#bRecipe").textContent = st.recipe ? st.recipe.active : "—";
  $("#bLotBox").classList.toggle("hidden", !st.batch);
  if (st.batch) $("#bLot").textContent = st.batch.lot;
  $("#bClock").textContent = st.time.slice(0, 5);
  document.title = `${title} · Line Display`;
}

function renderKpis(s) {
  $("#kN").textContent = s.n.toLocaleString();
  $("#kNsub").textContent = `since ${s.started}`;
  $("#kFail").textContent = s.failed.toLocaleString();
  const fs = $("#kFailSub");
  fs.textContent = s.escaped ? `⚠ ${s.escaped} not rejected` : s.n ? `${(s.failed / s.n * 100).toFixed(1)}% of cans` : " ";
  fs.style.color = s.escaped ? "var(--orange)" : "";
  const y = s.yield, ky = $("#kYield");
  ky.textContent = y == null ? "—" : `${y.toFixed(1)}%`;
  const color = y == null ? "" : y >= 95 ? "var(--green)" : y >= 85 ? "var(--amber)" : "var(--red)";
  ky.style.color = color;
  $("#kYieldBar").style.width = `${y || 0}%`;
  $("#kYieldBar").style.background = color || "var(--green)";
  $("#kRate").textContent = s.rate;
}

/* ------------------------------------------------------------- last can */
function renderCan(st) {
  const cam = st.camera || {};
  const view = $("#canView"), img = $("#canImg");
  if (LIVE && cam.available && cam.ok) {
    if (!S.liveOn) { img.src = `/video/live?t=${Date.now()}`; S.liveOn = true; }
    view.classList.add("has-img");
    $("#canTitle").textContent = "Live camera";
    return;
  }
  if (cam.last && cam.last_seq !== S.lastSeq) {
    S.lastSeq = cam.last_seq;
    img.src = `/video/last.jpg?seq=${cam.last_seq}`;
    view.classList.add("has-img");
  }
}

function renderLatest() {
  const r = S.results[S.results.length - 1];
  const panel = $(".can-panel"), tag = $("#canTag");
  if (!r) { tag.textContent = ""; tag.className = "tag"; panel.classList.remove("pass", "fail"); return; }
  const escaped = !r.pass && ESCAPED.has(r.reject);
  tag.className = `tag ${r.pass ? "pass" : escaped ? "escaped" : "fail"}`;
  tag.textContent = r.pass ? "PASS" : escaped ? "NOT REJECTED" : "DEFECT";
  panel.classList.toggle("pass", r.pass);
  panel.classList.toggle("fail", !r.pass);
  $("#canFoot").innerHTML = `<span><b>Can #${r.id}</b></span><span class="muted">${esc(r.t)}</span>` +
    (r.pass ? "" : `<span><b>${esc(r.defect || "Defect")}</b></span>`) +
    `<span class="muted">${Math.round(r.conf * 100)}% sure</span>` +
    (r.pass ? "" : `<span class="muted">${r.reject === "fired" ? "blown off" : r.reject === "pending" ? "air pending" : esc(r.reject)}</span>`);
  $("#canEmpty").classList.add("hidden");
  // no camera image (e.g. simulated inspector): show the result itself, big
  const bw = $("#bigWord");
  bw.textContent = r.pass ? "PASS" : escaped ? "NOT REJECTED" : (r.defect || "DEFECT").toUpperCase();
  bw.className = r.pass ? "pass" : escaped ? "escaped" : "fail";
  $("#bigSub").textContent = `Can #${r.id} · ${r.t}`;
}

/* -------------------------------------------------------------- side panel */
function renderSide(st) {
  const panel = $("#sidePanel"), body = $("#sideBody");
  const al = activeAlarm(st);
  let html;
  if (al) {
    panel.className = `panel side-panel alarm ${al.severity}`;
    $("#sideTitle").textContent = "What to check";
    const more = S.alarms.active.length - 1;
    html = `<div class="al-title">${esc(al.title)}</div><div class="al-msg">${esc(al.msg)}</div>
      <ol class="al-steps">${al.steps.slice(0, 3).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>` +
      (more > 0 ? `<div class="al-more">+ ${more} more alarm${more > 1 ? "s" : ""} — see the dashboard</div>` : "");
  } else {
    panel.className = "panel side-panel";
    $("#sideTitle").textContent = "Defects this session";
    const d = st.stats.defects;
    if (!d.length) html = `<div class="side-empty"><b>No defects</b>${st.stats.n ? `${st.stats.n.toLocaleString()} cans inspected` : "Waiting for cans"}</div>`;
    else {
      const peak = d[0][1];
      html = d.slice(0, 5).map(([name, n]) => `<div class="def"><span>${esc(name)}</span><b>${n}</b>
        <div class="bar"><i style="width:${Math.max(4, n / peak * 100)}%"></i></div></div>`).join("");
    }
  }
  if (body.dataset.html !== html) { body.innerHTML = html; body.dataset.html = html; }
}

/* ----------------------------------------------------------------- footer */
function renderStrip() {
  const el = $("#strip");
  const unit = Math.min(window.innerHeight / 100, window.innerWidth * 0.0056);
  const n = Math.max(10, Math.floor(el.clientWidth / (unit * 2.1)));
  const shown = S.results.slice(-n);
  el.innerHTML = shown.map((r) => {
    const c = r.pass ? "pass" : r.reject === "pending" ? "pending" : ESCAPED.has(r.reject) ? "escaped" : "fail";
    return `<i class="${c}"></i>`;
  }).join("");
}

function renderChips(st) {
  const cam = st.camera || {};
  const chips = [
    [st.sim ? "warn" : "ok", st.sim ? "Simulation" : "Hardware"],
    [st.sensor_mode && !st.faults.some((f) => f.startsWith("Trigger")) ? "ok" : "bad", "Sensor"],
    [!cam.available ? "" : cam.ok ? "ok" : "bad", cam.ok ? `Camera ${Math.round(cam.fps)} fps` : "Camera"],
  ];
  if (st.cpu_temp != null) chips.push([st.thermal === "ok" ? "ok" : st.thermal === "warm" ? "warn" : "bad", `Pi ${Math.round(st.cpu_temp)} °C`]);
  const html = chips.map(([c, t]) => `<span class="chip"><i class="${c}"></i>${esc(t)}</span>`).join("");
  if ($("#chips").innerHTML !== html) $("#chips").innerHTML = html;
}

/* --------------------------------------------------------------- controls */
function renderControls(st) {
  if (!(CONTROLS && S.canControl)) return;
  const q = (c) => document.querySelector(`#controls [data-cmd="${c}"]`);
  q("start").disabled = st.running || st.estop;
  q("stop").disabled = !st.running;
  q("ack").disabled = !st.warning;
}
document.querySelectorAll("#controls [data-cmd]").forEach((b) => b.addEventListener("click", async () => {
  try {
    const r = await fetch(`/api/cmd/${b.dataset.cmd}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) { b.textContent = "✕"; setTimeout(() => (b.textContent = b.dataset.label), 1200); }
  } catch {}
}));
document.querySelectorAll("#controls [data-cmd]").forEach((b) => (b.dataset.label = b.textContent));

/* -------------------------------------------------- screen niceties */
let idleTimer;
function wake() {
  document.body.classList.remove("idle");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => document.body.classList.add("idle"), 3000);   // hide the mouse pointer
}
["mousemove", "touchstart", "keydown"].forEach((e) => document.addEventListener(e, wake, { passive: true }));
document.addEventListener("keydown", (e) => {
  if (e.key === "f" || e.key === "F") document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => {});
});
async function keepAwake() {                     // ask the browser not to blank the screen
  try { if ("wakeLock" in navigator) await navigator.wakeLock.request("screen"); } catch {}
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") keepAwake(); });
$("#canImg").addEventListener("error", () => { S.liveOn = false; });
window.addEventListener("resize", renderStrip);

wake();
keepAwake();
connect();
