/* ==========================================================================
   Can Inspection Station - dashboard client
   Talks to the station over Server-Sent Events (/api/stream) and small
   POST calls (/api/cmd/*, /api/settings). No build step, no libraries.
   ========================================================================== */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  canControl: false,
  status: null,
  results: [],          // session results, oldest first
  byId: new Map(),
  inspectors: [],
  sources: [],
  pins: null,
  editing: {},          // setting key -> time of last local edit (don't overwrite while typing)
  lastSeqShown: -1,
  liveOn: false,
  logUnseen: false,
  role: "operator",     // viewer | operator | engineer
  name: "",
  idleLeft: null,
  defaultPin: false,
  defectTypes: [],
  audit: [],
  page: "line",
  gige: null,
  alarms: null,
  alarmsVersion: -1,
  histDays: 30,
  hist: null,
  diag: null,
  rev: { status: "todo", result: "fail", date: "", items: [], total: 0, offset: 0, idx: -1, view: "ann", stats: null },
};
const MAX_ROWS = 300;
const REJECT_LABEL = { "-": "", pending: "pending…", fired: "fired", missed: "MISSED", disabled: "reject off", locked: "LOCKED" };
const ESCAPED = new Set(["missed", "disabled", "locked"]);
const RANK = { viewer: 0, operator: 1, engineer: 2 };
const can = (role) => RANK[S.role] >= RANK[role];
const RECIPE_KEYS = ["camera_delay_ms", "reject_delay_ms", "exposure_us", "gain_db", "vision_sensitivity",
  "vision_min_area", "pulses", "pulse_on_ms", "pulse_off_ms"];

/* ------------------------------------------------------------------ server */
function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => $("#connBanner").classList.add("hidden");
  es.onerror = () => { $("#connBanner").classList.remove("hidden"); stopLive(); };
  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handle(msg);
  };
}

function handle(msg) {
  switch (msg.type) {
    case "init":
      S.canControl = msg.can_control;
      S.role = msg.role;
      S.name = msg.name;
      S.defaultPin = msg.default_pin;
      S.defectTypes = msg.defect_types || [];
      S.audit = msg.audit || [];
      renderAudit();
      S.inspectors = msg.inspectors;
      S.sources = msg.sources;
      S.pins = msg.pins;
      S.results = msg.results;
      S.byId = new Map(S.results.map((r) => [r.id, r]));
      $("#log").innerHTML = "";
      msg.logs.forEach((l) => addLog(l, true));
      buildSettings();
      applyPermissions();
      renderTable(true);
      renderTimeline();
      drawTrend();
      onStatus(msg.status);
      loadAlarms();
      break;
    case "status": onStatus(msg.status); break;
    case "result": addResult(msg.result); break;
    case "reject": updateReject(msg.id, msg.status); break;
    case "log": addLog(msg.entry); break;
    case "audit": S.audit.push(msg.entry); if (S.audit.length > 300) S.audit.shift(); renderAudit(); break;
    case "alarms": loadAlarms(); break;
    case "sensor": break;
    case "cleared":
      S.results = []; S.byId.clear();
      renderTable(true); renderTimeline(); drawTrend();
      break;
  }
}

async function post(url, body) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.ok === false) {
      toast(data.error || `Request failed (${r.status})`, "error");
      if (data.login) openLogin();
      return null;
    }
    if (data.message) toast(data.message, "ok");
    return data;
  } catch {
    toast("Station not reachable", "error");
    return null;
  }
}
const cmd = (name, body) => post(`/api/cmd/${name}`, body);
function setSetting(key, value) {
  S.editing[key] = Date.now();
  return post("/api/settings", { [key]: value });
}

const GIGE_NOTE = '<div class="hint">The <b>Genie Nano</b> is a network (GigE Vision) camera, not a webcam, so it never shows up here. ' +
  'Use the <b>Genie Nano</b> source (on the Pi), or see README › Camera › Testing on a laptop.</div>';
/* ---------------------------------------------- GigE camera finder */
async function findGige(btn) {
  const lists = $$("[data-gige-list]"), btns = $$("[data-gige-find]");
  btns.forEach((b) => { b.disabled = true; b.dataset.label = b.dataset.label || b.textContent; b.textContent = "Searching the network…"; });
  lists.forEach((l) => (l.innerHTML = ""));
  const r = await post("/api/gige/find");
  btns.forEach((b) => { b.disabled = false; b.textContent = b.dataset.label; });
  if (!r) return;
  S.gige = r;
  renderGige();
}

function renderGige() {
  const r = S.gige;
  if (!r) return;
  const eng = can("engineer");
  const ifaces = r.interfaces.map((i) => `${esc(i.ip)}${i.name ? ` <span class="muted">(${esc(i.name)})</span>` : ""}`).join(", ") || "none";
  let html;
  if (!r.cameras.length) {
    html = `<div class="gige-none"><b>No camera answered.</b><ol>
      <li><b>Power:</b> the camera's LED must be on. A laptop's Ethernet port gives <b>no power</b> — use a PoE injector (802.3af) or a 10–36 V supply on the camera's 10-pin connector.</li>
      <li><b>LED slow-flashing blue</b> = no network link: check the cable and adapter.</li>
      <li><b>Just plugged in?</b> Wait about a minute: with no router, the computer and camera each pick a 169.254.x.x address, which takes a while.</li>
      ${r.os.startsWith("win") ? "<li><b>Windows Firewall</b> may block the answer: allow Python on <b>Public</b> networks (a cable straight to the camera counts as public), then search again.</li>" : ""}
      <li>Turn off VPN software on that Ethernet adapter.</li></ol>
      <div class="muted small">This computer's adapters: ${ifaces}</div></div>`;
  } else {
    html = r.cameras.map((c) => {
      const okNet = !!c.reachable_via, sug = c.suggested_ip;
      return `<div class="gige-cam ${okNet ? "same-net" : "other-net"}">
        <div class="gige-top"><b>${esc(c.manufacturer)} ${esc(c.model)}</b><span class="muted small">S/N ${esc(c.serial)}</span></div>
        <dl class="facts"><dt>IP address</dt><dd class="mono">${esc(c.ip)}</dd><dt>Subnet mask</dt><dd class="mono">${esc(c.mask)}</dd>
          <dt>Got its IP</dt><dd>${esc(c.ip_mode)}</dd><dt>MAC</dt><dd class="mono">${esc(c.mac)}</dd></dl>
        ${okNet ? `<div class="gige-msg ok">✓ Same network as this computer (${esc(c.reachable_via.ip)}).</div>
            ${c.current ? `<div class="gige-msg ok">✓ This is the IP in Settings.</div>` : `<button class="btn btn-sm btn-primary" data-gige-use="${esc(c.ip)}" ${eng ? "" : "disabled"}>Use ${esc(c.ip)}</button>`}`
          : `<div class="gige-msg warn">Found, but on a <b>different network</b> than this computer, so programs can't talk to it yet.</div>
            ${sug ? `<button class="btn btn-sm btn-primary" data-gige-force='${esc(JSON.stringify({ mac: c.mac, ip: sug.ip, mask: sug.mask }))}' ${eng ? "" : "disabled"}>Give it ${esc(sug.ip)} (temporary)</button>
            <div class="hint">Puts the camera on this computer's ${esc(sug.adapter || sug.via)} network until it is power-cycled.</div>` : ""}`}
      </div>`;
    }).join("") + `<div class="muted small">This computer's adapters: ${ifaces}</div>`;
  }
  $$("[data-gige-list]").forEach((l) => (l.innerHTML = html));
}

async function findWebcams() {
  const btn = $("#webcamFind"), list = $("#webcamList");
  btn.disabled = true; btn.textContent = "Searching…";
  list.innerHTML = '<div class="hint">Checking each camera — this takes a few seconds.</div>';
  const r = await post("/api/webcams");
  btn.disabled = false; btn.textContent = "Find cameras";
  if (!r) { list.innerHTML = ""; return; }
  if (!r.cameras.length) { list.innerHTML = '<div class="hint">No cameras found. Close other apps using the camera (Zoom, Teams, browser tabs) and try again.</div>' + GIGE_NOTE; return; }
  list.innerHTML = "";
  r.cameras.forEach((c) => {
    const b = document.createElement("button");
    b.className = "cam-opt" + (c.index === r.current && r.active ? " on" : "");
    b.dataset.i = c.index;
    b.innerHTML = (c.thumb ? `<img src="${c.thumb}" alt="">` : '<div class="cam-noimg">no picture</div>') +
      `<span>Camera ${c.index}${c.width ? ` · ${c.width}×${c.height}` : ""}</span>`;
    b.addEventListener("click", () => setSetting("webcam_index", c.index));
    list.appendChild(b);
  });
  list.insertAdjacentHTML("beforeend", GIGE_NOTE);
}

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; }, 3200);
  setTimeout(() => el.remove(), 3600);
}

/* ------------------------------------------------------------------ status */
function onStatus(st) {
  S.status = st;
  renderHeader(st);
  renderControls(st);
  renderTower(st);
  renderCamera(st);
  renderKpis(st.stats);
  renderDefects(st.stats);
  renderProduction(st);
  syncSettings(st);
  renderRecipeDrawer(st);
  renderConveyor(st.conveyor);
}

function renderHeader(st) {
  const pill = $("#statePill");
  let cls = "neutral", text = "Stopped · ready";
  if (st.estop) { cls = "estop"; text = "Emergency stop"; }
  else if (st.faults.length) { cls = "fault"; text = `Fault · ${st.faults[0]}` + (st.faults.length > 1 ? `  (+${st.faults.length - 1})` : ""); }
  else if (st.warning) { cls = "warning"; text = `Warning · ${st.warning_reason}`; }
  else if (st.running) { cls = "running"; text = "Running"; }
  else if (st.manual) { cls = "manual"; text = "Manual override"; }
  pill.className = `state-pill ${cls}`;
  $("#stateText").textContent = text;
  pill.title = text;

  setChip("#chipGpio", st.sim ? "Simulation" : "Hardware", st.sim ? "warn" : "ok");
  setChip("#chipSensor", st.sensor_mode, st.sensor ? "info" : "ok");
  const cam = st.camera;
  const srcLabel = st.cam_source === "gige" ? st.cam_ip : (S.sources.find((s) => s.key === st.cam_source) || {}).label || st.cam_source;
  if (!cam.available) setChip("#chipCamera", "not installed", "");
  else setChip("#chipCamera", cam.ok ? `${srcLabel} · ${Math.round(cam.fps)} fps` : srcLabel, cam.ok ? "ok" : "bad");

  // temperatures
  const temps = [];
  if (st.cpu_temp != null) temps.push(`Pi ${Math.round(st.cpu_temp)}°`);
  if (st.cam_temp != null) temps.push(`Cam ${Math.round(st.cam_temp)}°`);
  $("#chipTemp").classList.toggle("hidden", !temps.length);
  if (temps.length) {
    const hot = st.thermal === "hot" || st.thermal === "critical" || (st.power && st.power.undervoltage_now);
    setChip("#chipTemp", temps.join(" · "), hot ? "bad" : st.thermal === "warm" ? "warn" : "ok");
  }

  $("#clock").textContent = st.time;
  const up = Math.floor(st.uptime);
  $("#dateLine").textContent = `${st.date} · up ${Math.floor(up / 3600)}:${String(Math.floor(up % 3600 / 60)).padStart(2, "0")}:${String(up % 60).padStart(2, "0")}`;
}

function setChip(sel, text, state) {
  const c = $(sel);
  $(".v", c).textContent = text;
  $(".dot", c).className = `dot ${state}`;
}

function renderControls(st) {
  const op = can("operator"), eng = can("engineer");
  $("#btnStart").disabled = !op || st.running || st.estop;
  $("#btnStop").disabled = !st.running;
  $("#btnReset").disabled = !op;
  $("#btnAck").disabled = !op || !st.warning;
  $("#btnSim").disabled = !op || !st.running;
  $("#btnBurst").disabled = !eng || st.running || st.estop;
  const ov = $("#overrideToggle");
  ov.checked = st.manual;
  ov.disabled = !eng || st.running || st.estop;
  $("#overrideHint").textContent = st.manual ? "manual override" : "";
}

/* ------------------------------------------------------------- stack light */
const LAMPS = [["red", "#ef4444", "#3a1517"], ["yellow", "#f5a524", "#3a2a0c"], ["green", "#22c55e", "#0f3320"]];
function buildTower() {
  const svg = $("#tower");
  let html = `<defs>
    <filter id="glow" x="-80%" y="-80%" width="260%" height="260%">
      <feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <linearGradient id="shade" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset=".35" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".25"/></linearGradient>
  </defs>
  <rect x="24" y="4" width="36" height="12" rx="5" fill="#232d3b" stroke="none"/>`;
  LAMPS.forEach(([key], i) => {
    const y = 18 + i * 66;
    html += `<g class="lamp" data-key="${key}">
      <rect class="lens" x="16" y="${y}" width="52" height="60" rx="11" stroke="none"/>
      <rect x="16" y="${y}" width="52" height="60" rx="11" fill="url(#shade)" stroke="none" pointer-events="none"/>
      <text class="lamp-txt" x="42" y="${y + 34}" text-anchor="middle" font-size="10" font-weight="700" fill="#0b0f15" stroke="none"></text>
    </g>`;
  });
  html += `<rect x="37" y="216" width="10" height="12" fill="#232d3b" stroke="none"/><rect x="18" y="228" width="48" height="7" rx="3" fill="#232d3b" stroke="none"/>`;
  svg.innerHTML = html;
  $$(".lamp", svg).forEach((g) => g.addEventListener("click", () => {
    if (S.status && S.status.manual && S.canControl) cmd("lamp", { key: g.dataset.key });
  }));
}

function renderTower(st) {
  const svg = $("#tower");
  svg.classList.toggle("clickable", st.manual && S.canControl);
  LAMPS.forEach(([key, on, off]) => {
    const g = $(`.lamp[data-key="${key}"]`, svg);
    const lit = !!st.lights[key];
    const lens = $(".lens", g);
    lens.setAttribute("fill", lit ? on : off);
    lens.setAttribute("filter", lit ? "url(#glow)" : "");
    $(".lamp-txt", g).textContent = st.manual ? (lit ? "ON" : "OFF") : "";
    $(".lamp-txt", g).setAttribute("fill", lit ? "#0b0f15" : "#8b97a8");
    $(`.legend-row[data-key="${key}"]`).classList.toggle("on", lit);
  });

  // reasons
  let items;
  if (st.faults.length) items = st.faults.slice(0, 4).map((f) => ["bad", f]);
  else if (st.warning) items = [["warn", `${st.warning_reason} — check upstream, then Acknowledge`]];
  else items = [["ok", st.running ? "All systems OK — inspecting" : "All systems OK — press Start"]];
  const tip = st.faults.length || st.warning ? `<span class="why">What to check ›</span>` : "";
  const html = items.map(([c, t], i) => `<li class="${c}${tip ? " link" : ""}"><span class="dot ${c}"></span><span>${esc(t)}</span>${i === 0 ? tip : ""}</li>`).join("");
  const ul = $("#reasons");
  if (ul.innerHTML !== html) ul.innerHTML = html;

  const v = $("#valve");
  v.classList.toggle("open", st.valve);
  v.classList.toggle("locked", !st.valve && st.locked);
  $("#valveState").textContent = st.valve ? "Air on" : st.locked ? "Locked out" : "Closed";
  if (S.pins) $("#valvePins").textContent = `GPIO ${S.pins.reject} · relay 4`;
}

/* ------------------------------------------------------------------ camera */
function startLive() {
  if (S.liveOn) return;
  S.liveOn = true;
  $("#liveImg").src = `/video/live?t=${Date.now()}`;
}
function stopLive() {
  S.liveOn = false;
  $("#liveImg").removeAttribute("src");
}

function renderCamera(st) {
  const cam = st.camera;
  const msg = $("#vpMsg");
  const srcLabel = (S.sources.find((s) => s.key === st.cam_source) || {}).label || st.cam_source;
  $("#camSource").textContent = cam.available ? srcLabel : "unavailable";
  if (!cam.available) {
    msg.classList.remove("hidden");
    $("#vpMsgText").innerHTML = "Camera features need OpenCV and NumPy.<br><code>pip install opencv-python numpy</code>";
    stopLive();
    return;
  }
  $("#btnWake").classList.toggle("hidden", !(cam.ok && st.standby));
  if (S.page !== "line") { stopLive(); return; }       // no video while on other pages (less CPU / heat)
  if (cam.ok && st.standby) {
    msg.classList.remove("hidden");
    $("#vpMsgText").textContent = "Camera in standby — the line has been stopped, so the camera runs slowly to stay cool.";
  } else if (cam.ok) {
    msg.classList.add("hidden");
    startLive();
  } else {
    msg.classList.remove("hidden");
    $("#vpMsgText").textContent = cam.status || "Camera not connected";
    stopLive();
  }
  $$("#viewSeg button").forEach((b) => b.classList.toggle("on", b.dataset.v === cam.view));
  const badge = $("#vpBadge");
  badge.classList.toggle("paused", cam.paused);
  badge.classList.toggle("hidden", !cam.ok);
  const heat = st.thermal === "hot" || st.thermal === "critical" ? " · REDUCED (HOT)" : st.thermal === "warm" ? " · SLOWED (WARM)" : "";
  $("#vpBadgeText").textContent = cam.paused ? "PAUSED" : `LIVE · ${Math.round(cam.fps)} FPS${heat}`;
  $("#btnPause").textContent = cam.paused ? "Resume" : "Pause";
  const res = cam.resolution ? `${cam.resolution[0]}×${cam.resolution[1]}` : "";
  const br = cam.brightness;
  const brTxt = br ? `brightness ${Math.round(br[0] / 2.55)}%${br[0] < 50 ? " — TOO DARK" : br[1] > 5 ? " — BLOWN OUT" : ""}` : "";
  $("#vpInfo").textContent = cam.ok ? [cam.info, res, brTxt].filter(Boolean).join("  ·  ") : "";
  $("#vpInfo").classList.toggle("warn-text", !!br && (br[0] < 50 || br[1] > 5));
  const bh = $("#brightHint");
  if (bh) bh.innerHTML = !br ? "no camera image" : `${Math.round(br[0] / 2.55)}% · ` +
    (br[0] < 50 ? '<span class="bad">too dark — raise exposure, open the lens aperture, add light</span>'
      : br[1] > 5 ? '<span class="warn">blown out — lower exposure</span>' : '<span class="ok">OK</span>');
  const ab = $("#btnAutoExp");
  if (ab) { ab.disabled = !can("engineer") || !cam.ok || cam.auto_exposure; ab.textContent = cam.auto_exposure ? "Adjusting…" : "Auto-set exposure"; }
  $("#vpInfo").classList.toggle("hidden", !cam.ok);

  // reference
  const ref = $("#refState");
  ref.textContent = cam.reference ? `Taught ${cam.reference}` : "Not taught — local contrast";
  ref.classList.toggle("ok", !!cam.reference);

  // last inspected can
  const last = cam.last;
  if (last && cam.last_seq !== S.lastSeqShown) {
    S.lastSeqShown = cam.last_seq;
    $("#lastImg").src = `/video/last.jpg?seq=${cam.last_seq}`;
    $("#lastEmpty").classList.add("hidden");
    $("#lastResult").innerHTML = `<span class="tag ${last.pass ? "pass" : "fail"}">${last.pass ? "PASS" : "DEFECT"}</span><span class="last-id">Can #${last.id}</span>`;
    $("#lastSummary").textContent = last.summary;
    $("#lastFacts").innerHTML = `
      <dt>Confidence</dt><dd>${Math.round(last.conf * 100)}%</dd>
      <dt>Processing</dt><dd>${last.ms.toFixed(0)} ms · ${esc(last.method)}</dd>
      <dt>Time</dt><dd>${esc(last.t)}</dd>
      <dt>Saved</dt><dd>${last.img ? "yes" : "no"}</dd>` +
      Object.entries(last.measures || {}).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("#lastThumb").dataset.img = last.img || "";
  }
}

/* -------------------------------------------------------------------- KPIs */
function renderKpis(s) {
  $("#kN").textContent = s.n.toLocaleString();
  $("#kNsub").textContent = `since ${s.started}`;
  $("#kPass").textContent = s.passed.toLocaleString();
  $("#kPassSub").textContent = s.n ? `${(s.passed / s.n * 100).toFixed(1)}% of cans` : " ";
  $("#kFail").textContent = s.failed.toLocaleString();
  const fs = $("#kFailSub");
  if (s.escaped) { fs.textContent = `⚠ ${s.escaped} not rejected`; fs.classList.add("warn"); }
  else { fs.textContent = s.failed ? `${s.fired} blown off` : " "; fs.classList.remove("warn"); }
  const y = s.yield;
  const ky = $("#kYield");
  ky.textContent = y == null ? "—" : `${y.toFixed(1)}%`;
  const color = y == null ? "" : y >= 95 ? "var(--green)" : y >= 85 ? "var(--amber)" : "var(--red)";
  ky.style.color = color;
  const bar = $("#kYieldBar");
  bar.style.width = `${y || 0}%`;
  bar.style.background = color || "var(--cyan)";
  $("#kRate").textContent = s.rate;
  $("#kMs").textContent = s.avg_ms == null ? "—" : Math.round(s.avg_ms);
  $("#tableCount").textContent = s.n;
}

function renderDefects(s) {
  const el = $("#defects");
  if (!s.defects.length) {
    if (!el.dataset.empty) { el.innerHTML = `<div class="empty" style="inset:0">No defects recorded this session.</div>`; el.dataset.empty = "1"; }
    return;
  }
  delete el.dataset.empty;
  const peak = s.defects[0][1];
  const html = s.defects.map(([name, n]) =>
    `<div class="def-row"><span>${esc(name)}</span><div class="def-bar"><span style="width:${Math.max(3, n / peak * 100)}%"></span></div>
     <span class="def-num"><b>${n}</b> · ${(n / s.n * 100).toFixed(1)}%</span></div>`).join("");
  if (el.innerHTML !== html) el.innerHTML = html;
}

/* ----------------------------------------------------------------- results */
function rowHtml(r) {
  const escaped = !r.pass && ESCAPED.has(r.reject);
  const tag = r.pass ? `<span class="tag pass">PASS</span>` : `<span class="tag ${escaped ? "escaped" : "fail"}">DEFECT</span>`;
  const rl = REJECT_LABEL[r.reject] ?? r.reject;
  const rcls = escaped ? "bad" : r.reject === "fired" ? "fired" : "";
  const img = r.img ? `<svg class="img-ic" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-8 8"/></svg>` : "";
  return `<td class="id">#${r.id}</td><td>${esc(r.t)}</td><td>${tag}</td><td>${esc(r.defect || "—")}</td>
    <td class="num">${Math.round(r.conf * 100)}%</td><td class="num">${Math.round(r.ms)} ms</td>
    <td><span class="reject-lbl ${rcls}">${esc(rl)}</span></td><td>${img}</td>`;
}

function makeRow(r, fresh) {
  const tr = document.createElement("tr");
  tr.dataset.id = r.id;
  tr.innerHTML = rowHtml(r);
  if (r.img) tr.classList.add("has-img");
  if (fresh) tr.classList.add("fresh");
  return tr;
}

function renderTable() {
  const tb = $("#rows");
  tb.innerHTML = "";
  const frag = document.createDocumentFragment();
  S.results.slice(-MAX_ROWS).reverse().forEach((r) => frag.appendChild(makeRow(r, false)));
  tb.appendChild(frag);
  $("#tableEmpty").classList.toggle("hidden", S.results.length > 0);
}

function addResult(r) {
  S.results.push(r);
  S.byId.set(r.id, r);
  if (S.results.length > 5000) S.byId.delete(S.results.shift().id);
  const tb = $("#rows");
  tb.insertBefore(makeRow(r, true), tb.firstChild);
  while (tb.children.length > MAX_ROWS) tb.lastChild.remove();
  $("#tableEmpty").classList.add("hidden");
  renderTimeline();
  scheduleTrend();
}

function updateReject(id, status) {
  const r = S.byId.get(id);
  if (!r) return;
  r.reject = status;
  const tr = $(`#rows tr[data-id="${id}"]`);
  if (tr) tr.innerHTML = rowHtml(r);
  renderTimeline();
}

function renderTimeline() {
  const el = $("#timeline");
  if (!el) return;                       // removed from the light UI
  const n = Math.max(1, Math.floor((el.clientWidth + 3) / 14));
  const shown = S.results.slice(-n);
  let html = "";
  for (let i = 0; i < n - shown.length; i++) html += "<i></i>";
  for (const r of shown) {
    const c = r.pass ? "pass" : r.reject === "pending" ? "pending" : ESCAPED.has(r.reject) ? "escaped" : "fail";
    html += `<i class="${c}" title="#${r.id} ${r.pass ? "pass" : esc(r.defect)}"></i>`;
  }
  el.innerHTML = html;
}

/* ------------------------------------------------------------------ chart */
let trendQueued = false;
function scheduleTrend() {
  if (trendQueued) return;
  trendQueued = true;
  requestAnimationFrame(() => { trendQueued = false; drawTrend(); });
}

function drawTrend() {
  const cv = $("#trend");
  if (!cv) return;                       // removed from the light UI
  const box = cv.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  if (box.width < 10 || box.height < 10) return;
  cv.width = Math.round(box.width * dpr);
  cv.height = Math.round(box.height * dpr);
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = box.width, H = box.height;
  g.clearRect(0, 0, W, H);
  g.font = "11px InterLocal, system-ui, sans-serif";

  const MIN = 30, BUCKET = 10;                       // 30 buckets x 10 s = last 5 minutes
  const nowB = Math.floor(Date.now() / 1000 / BUCKET);
  const total = new Array(MIN).fill(0), bad = new Array(MIN).fill(0);
  for (const r of S.results) {
    const i = MIN - 1 - (nowB - Math.floor(r.ts / BUCKET));
    if (i >= 0 && i < MIN) { total[i]++; if (!r.pass) bad[i]++; }
  }
  const padL = 34, padR = 38, padT = 8, padB = 22;
  const cw = W - padL - padR, ch = H - padT - padB;
  const maxN = Math.max(4, ...total);
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500].find((s) => s * 4 >= maxN) || Math.ceil(maxN / 4);
  const niceMax = step * 4;

  // grid + axes
  g.strokeStyle = "rgba(148,163,184,.10)";
  g.fillStyle = "#5a6577";
  g.lineWidth = 1;
  for (let k = 0; k <= 4; k++) {
    const y = padT + ch - (ch * k) / 4 + 0.5;
    g.beginPath(); g.moveTo(padL, y); g.lineTo(W - padR, y); g.stroke();
    g.textAlign = "right"; g.fillText(String(Math.round(niceMax * k / 4)), padL - 8, y + 4);
    g.textAlign = "left"; g.fillText(`${k * 25}%`, W - padR + 8, y + 4);
  }
  g.textAlign = "center";
  for (const [back, label] of [[29, "-5 min"], [18, "-3 min"], [6, "-1 min"], [0, "now"]]) {
    const x = padL + (MIN - 1 - back + 0.5) * (cw / MIN);
    g.fillText(label, Math.min(Math.max(x, padL + 16), W - padR - 10), H - 6);
  }

  if (!S.results.length) {
    g.fillStyle = "#8b97a8"; g.font = "13px InterLocal, system-ui, sans-serif";
    g.fillText("Waiting for cans…", padL + cw / 2, padT + ch / 2);
    return;
  }

  // bars
  const bw = cw / MIN;
  const rr = (x, y, w, h, r) => { g.beginPath(); g.roundRect ? g.roundRect(x, y, w, h, [r, r, 0, 0]) : g.rect(x, y, w, h); g.fill(); };
  for (let i = 0; i < MIN; i++) {
    if (!total[i]) continue;
    const x = padL + i * bw + bw * 0.18, w = bw * 0.64;
    const h = (total[i] / niceMax) * ch, hb = (bad[i] / niceMax) * ch;
    g.fillStyle = "rgba(59,130,246,.55)"; rr(x, padT + ch - h, w, h, 3);
    if (bad[i]) { g.fillStyle = "rgba(239,68,68,.9)"; g.fillRect(x, padT + ch - hb, w, hb); }
  }
  // yield line
  g.strokeStyle = "#22c55e"; g.lineWidth = 2; g.lineJoin = "round";
  g.beginPath();
  let started = false;
  const pts = [];
  for (let i = 0; i < MIN; i++) {
    if (!total[i]) continue;
    const y = padT + ch - ((total[i] - bad[i]) / total[i]) * ch;
    const x = padL + (i + 0.5) * bw;
    pts.push([x, y]);
    started ? g.lineTo(x, y) : g.moveTo(x, y);
    started = true;
  }
  g.stroke();
  g.fillStyle = "#22c55e";
  for (const [x, y] of pts) { g.beginPath(); g.arc(x, y, 2.6, 0, Math.PI * 2); g.fill(); }
}

/* -------------------------------------------------------------------- log */
function addLog(l, initial = false) {
  const box = $("#log");
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const row = document.createElement("div");
  row.className = `log-row ${l.level}`;
  row.innerHTML = `<span class="t">${esc(l.t)}</span><span class="lv">${esc(l.level)}</span><span class="m">${esc(l.msg)}</span>`;
  box.appendChild(row);
  while (box.children.length > 400) box.firstChild.remove();
  if (near || initial) box.scrollTop = box.scrollHeight;
  if (!initial && (l.level === "ERROR" || l.level === "ALARM") && !$('.tab[data-tab="log"]').classList.contains("active")) {
    $("#logAlert").classList.remove("hidden");
  }
}

/* --------------------------------------------------------------- settings */
function buildSettings() {
  // inspector cards
  $("#inspectorCards").innerHTML = S.inspectors.map((i) =>
    `<button class="radio-card" data-insp="${esc(i.name)}"><b>${esc(i.name)}</b><span>${esc(i.description)}</span></button>`).join("");
  $$("#inspectorCards .radio-card").forEach((b) => b.addEventListener("click", () => setSetting("inspector", b.dataset.insp)));
  // camera sources
  $("#sourceSeg").innerHTML = S.sources.map((s) => `<button data-v="${s.key}">${esc(s.label)}</button>`).join("");
  $$("#sourceSeg button").forEach((b) => b.addEventListener("click", () => setSetting("camera_backend", b.dataset.v)));
}

function initSettingWidgets() {
  // steppers
  $$(".stepper[data-setting]").forEach((el) => {
    const key = el.dataset.setting, min = +el.dataset.min, max = +el.dataset.max, step = +el.dataset.step;
    el.innerHTML = `<button type="button" aria-label="decrease">−</button><input inputmode="numeric"><button type="button" aria-label="increase">+</button><span class="unit">${esc(el.dataset.unit)}</span>`;
    const input = $("input", el);
    const commit = (v) => {
      v = Math.min(max, Math.max(min, Math.round(v)));
      input.value = v;
      setSetting(key, v);
    };
    const [dec, inc] = $$("button", el);
    dec.addEventListener("click", () => commit((+input.value || 0) - step));
    inc.addEventListener("click", () => commit((+input.value || 0) + step));
    input.addEventListener("change", () => commit(+input.value || min));
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
  });
  // mark settings that belong to the recipe
  $$(".stepper[data-setting]").forEach((el) => {
    const label = el.closest(".field") && $("label", el.closest(".field"));
    if (label && RECIPE_KEYS.includes(el.dataset.setting)) label.insertAdjacentHTML("beforeend", '<span class="rcp" title="Saved in the recipe">R</span>');
  });
  // switches
  $$('input[type="checkbox"][data-setting]').forEach((cb) =>
    cb.addEventListener("change", () => setSetting(cb.dataset.setting, cb.checked)));
  // segmented settings
  $$(".seg[data-setting]").forEach((el) => {
    el.innerHTML = el.dataset.options.split(",").map((o) => { const [v, l] = o.split(":"); return `<button data-v="${v}">${l}</button>`; }).join("");
    $$("button", el).forEach((b) => b.addEventListener("click", () => setSetting(el.dataset.setting, b.dataset.v)));
  });
  // camera IP / folder
  $("#ipApply").addEventListener("click", () => setSetting("camera_ip", $("#ipInput").value.trim()));
  $("#ipInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#ipApply").click(); });
  $("#ipInput").addEventListener("input", () => (S.editing.camera_ip = Date.now()));
  $("#webcamFind").addEventListener("click", findWebcams);
  $$("[data-gige-find]").forEach((b) => b.addEventListener("click", () => findGige(b)));
  document.addEventListener("click", async (e) => {
    const use = e.target.closest("[data-gige-use]");
    if (use) {
      if (await setSetting("camera_ip", use.dataset.gigeUse)) {
        if (S.status && S.status.settings.camera_backend !== "gige") await setSetting("camera_backend", "gige");
        toast(`Camera IP set to ${use.dataset.gigeUse}`, "ok");
        findGige();
      }
    }
    const force = e.target.closest("[data-gige-force]");
    if (force) {
      const d = JSON.parse(force.dataset.gigeForce);
      force.disabled = true; force.textContent = "Setting IP…";
      if (await post("/api/gige/forceip", d)) { await setSetting("camera_ip", d.ip); setTimeout(findGige, 600); }
      else force.disabled = false;
    }
  });
  $("#yoloApply").addEventListener("click", () => setSetting("yolo_model", $("#yoloInput").value.trim()));
  $("#hailoApply").addEventListener("click", () => setSetting("hailo_model", $("#hailoInput").value.trim()));
  $("#hailoInput").addEventListener("input", () => (S.editing.hailo_model = Date.now()));
  $("#yoloInput").addEventListener("input", () => (S.editing.yolo_model = Date.now()));
  $("#folderApply").addEventListener("click", () => setSetting("image_folder", $("#folderInput").value.trim()));
  $("#folderInput").addEventListener("input", () => (S.editing.image_folder = Date.now()));
}

function fresh(key) { return Date.now() - (S.editing[key] || 0) < 2500; }

function syncSettings(st) {
  const s = st.settings;
  $$(".stepper[data-setting]").forEach((el) => {
    const input = $("input", el);
    if (document.activeElement !== input && !fresh(el.dataset.setting)) input.value = s[el.dataset.setting];
  });
  $$('input[type="checkbox"][data-setting]').forEach((cb) => { if (!fresh(cb.dataset.setting)) cb.checked = !!s[cb.dataset.setting]; });
  $$(".seg[data-setting]").forEach((el) => $$("button", el).forEach((b) => b.classList.toggle("on", b.dataset.v === s[el.dataset.setting])));
  $$("#inspectorCards .radio-card").forEach((b) => b.classList.toggle("on", b.dataset.insp === s.inspector));
  $$("#sourceSeg button").forEach((b) => b.classList.toggle("on", b.dataset.v === s.camera_backend));
  const ip = $("#ipInput");
  if (document.activeElement !== ip && !fresh("camera_ip")) ip.value = s.camera_ip;
  const fi = $("#folderInput");
  if (document.activeElement !== fi && !fresh("image_folder")) fi.value = s.image_folder;
  $("#folderField").classList.toggle("hidden", s.camera_backend !== "folder");
  $("#webcamField").classList.toggle("hidden", s.camera_backend !== "webcam");
  $("#webcamCur").textContent = `Using camera ${s.webcam_index}`;
  $$("#webcamList .cam-opt").forEach((b) => b.classList.toggle("on", +b.dataset.i === s.webcam_index));
  $("#ipField").classList.toggle("hidden", s.camera_backend !== "gige");
  const online = st.cam_online;
  $("#ipHint").textContent = !st.cam_ping ? "" : online === true ? `Reachable · ${Math.round(st.cam_ms)} ms` : online === false ? "Not reachable — check cable, power and the Pi's IP" : "Checking…";
  const burst = s.pulses * (s.pulse_on_ms + s.pulse_off_ms);
  $("#burstHint").textContent = `Burst length ${burst} ms — cans must be at least ${burst} ms apart to be rejected individually.`;
  $("#warnWindow").textContent = s.warn_window;
  const eh = $("#engineHint"), cam = st.camera || {};
  const eng = s.vision_engine;
  eh.classList.toggle("hidden", eng === "builtin");
  eh.classList.toggle("bad", !!cam.engine_error);
  $("#yoloFields").classList.toggle("hidden", eng !== "yolo" && eng !== "hailo");
  $("#hailoFields").classList.toggle("hidden", eng !== "hailo");
  const hi = $("#hailoInput");
  if (document.activeElement !== hi && !fresh("hailo_model")) hi.value = s.hailo_model;
  $("#yoloInput").closest(".field").classList.toggle("hidden", eng === "hailo");
  const yb = $('.seg[data-setting="vision_engine"] button[data-v="yolo"]');
  if (yb) yb.classList.toggle("hidden", !st.yolo_available);
  ["vision_sensitivity", "vision_min_area"].forEach((k) =>
    $(`.stepper[data-setting="${k}"]`).closest(".field").classList.toggle("hidden", eng !== "builtin"));
  const yi = $("#yoloInput");
  if (document.activeElement !== yi && !fresh("yolo_model")) yi.value = s.yolo_model;
  eh.textContent = cam.engine_error ? `⚠ ${cam.engine_error} — cans are being rejected until it is fixed.`
    : eng === "team" ? "Using detect() from team_vision.py. Sensitivity and min size don't apply; edits to the file load automatically."
    : cam.yolo_info ? `Loaded ${cam.yolo_info}` + (cam.yolo_ms != null ? ` · last inference ${Math.round(cam.yolo_ms)} ms` : "") +
        (s.active_cooler ? "" : " · no cooler: live view shows the raw camera, boxes appear on inspected cans")
    : "Model loads on the first can (or open the live view). Sensitivity and min size don't apply.";
  renderHealth(st);
}

function renderHealth(st) {
  const box = $("#healthBox");
  const rows = [];
  const lvl = { ok: ["ok", "normal"], warm: ["warn", "warm - live view slowed"], hot: ["bad", "hot - load reduced"], critical: ["bad", "critical - inspection stopped"] }[st.thermal] || ["", ""];
  if (st.cpu_temp != null) rows.push(["Raspberry Pi", `<span class="${lvl[0]}">${Math.round(st.cpu_temp)} °C · ${lvl[1]}</span>`]);
  if (st.cam_temp != null) rows.push(["Camera", `<span class="${st.cam_temp >= 65 ? "bad" : st.cam_temp >= 60 ? "warn" : "ok"}">${Math.round(st.cam_temp)} °C</span>`]);
  const p = st.power || {};
  if (st.is_pi) rows.push(["Power", p.undervoltage_now ? `<span class="bad">under-voltage now</span>` : p.undervoltage_seen ? `<span class="warn">under-voltage since boot</span>` : `<span class="ok">OK</span>`]);
  if (st.is_pi && p.throttled_seen) rows.push(["CPU throttling", `<span class="warn">has happened since boot</span>`]);
  rows.push(["Camera mode", st.standby ? "standby" : st.running ? "inspecting" : "idle"]);
  rows.push(["Edition", st.yolo_available ? "Full (with YOLO)" : "Lite (no YOLO)"]);
  let html = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  if (!st.is_pi) html += `<dd class="full">Not running on a Raspberry Pi - temperature monitoring is off.</dd>`;
  if (box.innerHTML !== html) box.innerHTML = html;
}

function applyPermissions() {
  const op = can("operator"), eng = can("engineer");
  $("#roBanner").classList.toggle("hidden", S.canControl);
  $("#pinBanner").classList.toggle("hidden", !(eng && S.defaultPin));
  $$("#drawer input, #drawer select, #drawer button.radio-card, #drawer .stepper button, #drawer .seg button, " +
     "#ipApply, #folderApply, #recipeSave, #recipeCreate, #recipeDelete, #pinSave").forEach((el) => (el.disabled = !eng));
  $("#drawerLock").classList.toggle("hidden", eng || !S.canControl);
  $$("#viewSeg button, #btnPause, #btnWake").forEach((el) => (el.disabled = !op));
  $$('.camera-card [data-cmd="snapshot"], .camera-card [data-cmd="auto_exposure"], #btnArea, .ref-box [data-cmd], #btnClear').forEach((el) => (el.disabled = !eng));
  if (!eng && AREA.on) areaMode(false);
  $$("#pageDiag .stepper button, #pageDiag .stepper input, #pageDiag [data-cmd=cleanup]").forEach((el) => (el.disabled = !eng));
  if (S.diag) renderDiag();
  if (S.gige) renderGige();
  $("#revExport").classList.toggle("disabled", !eng);
  $("#revRoNote").classList.toggle("hidden", eng);
  renderUser();
  if (S.status) { renderControls(S.status); renderProduction(S.status); renderConveyor(S.status.conveyor); }
  if (S.rev.idx >= 0) renderReviewer();
}

/* ------------------------------------------------------------ user / login */
function renderUser() {
  const b = $("#btnUser");
  b.className = `user-btn ${S.role}`;
  $("#userRole").textContent = { viewer: "View only", operator: "Operator", engineer: "Engineer" }[S.role];
  $("#userName").textContent = S.role === "engineer"
    ? `${S.name}${S.idleLeft != null ? ` · ${Math.max(1, Math.ceil(S.idleLeft / 60))} min` : ""}`
    : S.role === "operator" ? "log in" : "";
}

async function refreshMe() {
  try {
    const d = await (await fetch("/api/me")).json();
    const was = S.role;
    S.role = d.role; S.name = d.name; S.idleLeft = d.idle_left; S.defaultPin = d.default_pin; S.canControl = d.can_control;
    if (was === "engineer" && d.role !== "engineer") toast("Engineer session ended", "");
    applyPermissions();
  } catch { /* offline: the stream banner already says so */ }
}

function openLogin() {
  if (!S.canControl) { toast("View-only device", "error"); return; }
  $("#loginErr").textContent = "";
  $("#loginPin").value = "";
  try { $("#loginName").value = localStorage.getItem("engName") || ""; } catch {}
  openDialog("#loginModal");
  setTimeout(() => ($("#loginName").value ? $("#loginPin") : $("#loginName")).focus(), 60);
}

function openDialog(sel) { $(sel).classList.add("open"); }
function closeDialog(el) { el.closest(".modal").classList.remove("open"); if (el.closest("#reviewer")) S.rev.idx = -1; }

/* ------------------------------------------------------- recipes & batches */
function renderProduction(st) {
  if (!st.recipe) return;
  const rc = st.recipe, op = can("operator"), eng = can("engineer");
  const sel = $("#recipeSelect");
  const sig = rc.list.map((r) => r.name + (r.has_reference ? "*" : "")).join("|");
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = rc.list.map((r) => `<option value="${esc(r.name)}">${esc(r.name)}${r.has_reference ? "" : " · no reference"}</option>`).join("");
    sel.dataset.sig = sig;
  }
  if (document.activeElement !== sel) sel.value = rc.active;
  const lock = st.running ? "Stop the line to change the recipe" : st.batch ? "End the batch to change the recipe" : "";
  sel.disabled = !op || !!lock;
  sel.title = lock;
  const mod = $("#recipeMod");
  mod.classList.toggle("hidden", !rc.modified.length);
  mod.textContent = rc.modified.length ? `Changed from recipe: ${rc.modified.join(", ")}${eng ? " — save it in Settings" : ""}` : "";

  const box = $("#batchBox");
  const b = st.batch;
  box.classList.toggle("active", !!b);
  const html = !b
    ? `<div class="batch-none"><span>No batch running</span><button class="btn btn-sm btn-go" data-batch="start" ${op ? "" : "disabled"}>Start batch</button></div>`
    : `<div class="batch-top"><span class="batch-live"></span><span class="lot">Lot ${esc(b.lot)}</span></div>
       <div class="batch-meta">${esc(b.operator)} · since ${esc(b.start.slice(11, 16))} · ${b.n.toLocaleString()} cans · yield ${b.yield == null ? "—" : b.yield.toFixed(1) + "%"}</div>
       <div class="btn-row"><button class="btn btn-sm btn-quiet" data-batch="report" data-id="${esc(b.id)}">Live report</button>
       <button class="btn btn-sm btn-quiet danger-text" data-batch="end" data-lot="${esc(b.lot)}" ${op ? "" : "disabled"}>End batch</button></div>`;
  if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
}

function renderRecipeDrawer(st) {
  if (!st.recipe) return;
  const rc = st.recipe;
  const active = rc.list.find((r) => r.name === rc.active) || {};
  $("#recipeName").textContent = rc.active;
  $("#recipeState").textContent = (rc.modified.length ? `Changed: ${rc.modified.join(", ")}` : "Matches the saved recipe") +
    (active.has_reference ? " · reference taught" : " · no good-can reference yet");
  $("#recipeSave").disabled = !can("engineer") || !rc.modified.length;
  const del = $("#recipeDelSel");
  const others = rc.list.filter((r) => r.name !== rc.active).map((r) => r.name);
  const sig = others.join("|");
  if (del.dataset.sig !== sig) {
    del.innerHTML = others.length ? others.map((n) => `<option>${esc(n)}</option>`).join("") : `<option value="">(no other recipes)</option>`;
    del.dataset.sig = sig;
  }
  $("#recipeDelete").disabled = !can("engineer") || !others.length;
}

async function openReport(id, title) {
  $("#reportFrame").src = `/report/${encodeURIComponent(id)}?embed=1`;
  $("#reportCsv").href = `/api/batch/${encodeURIComponent(id)}.csv`;
  $("#reportTitle").textContent = title || "Batch report";
  openDialog("#reportModal");
}

/* ------------------------------------------------------------------ pages */
let reportsTimer = null, diagTimer = null;
function setPage(page) {
  S.page = page;
  $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.page === page));
  $("#pageLine").classList.toggle("hidden", page !== "line");
  $("#pageReview").classList.toggle("hidden", page !== "review");
  $("#pageReports").classList.toggle("hidden", page !== "reports");
  $("#pageHistory").classList.toggle("hidden", page !== "history");
  $("#pageDiag").classList.toggle("hidden", page !== "diag");
  $("#pageMotors").classList.toggle("hidden", page !== "motors");
  if (page === "motors" && S.status) renderConveyor(S.status.conveyor);
  clearInterval(diagTimer);
  if (page === "history") loadHistory();
  if (page === "diag") { loadDiag(); diagTimer = setInterval(loadDiag, 1000); }
  if (page === "line") { renderTimeline(); scheduleTrend(); if (S.status) renderCamera(S.status); }
  else stopLive();
  if (page === "review") loadReview(true);
  clearInterval(reportsTimer);
  if (page === "reports") { loadBatches(); reportsTimer = setInterval(loadBatches, 10000); }
  try { history.replaceState(null, "", page === "line" ? "/" : `#${page}`); } catch {}
}

async function loadBatches() {
  let d;
  try { d = await (await fetch("/api/batches")).json(); } catch { return; }
  const fmtDur = (s) => { s = Math.round(s); return s >= 3600 ? `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m` : `${Math.floor(s / 60)}m`; };
  $("#batchRows").innerHTML = d.batches.map((b) => `<tr>
    <td><span class="lot">${esc(b.lot)}</span>${b.active ? '<span class="live">● LIVE</span>' : ""}</td>
    <td>${esc(b.recipe)}</td><td>${esc(b.operator)}</td><td>${esc(b.start.slice(0, 10))} ${esc(b.start.slice(11, 16))}</td>
    <td>${fmtDur(b.duration_s)}</td><td class="num">${b.n.toLocaleString()}</td><td class="num">${b.failed.toLocaleString()}</td>
    <td class="num"${b.escaped ? ' style="color:var(--orange);font-weight:700"' : ""}>${b.escaped}</td>
    <td class="num">${b.yield == null ? "—" : b.yield.toFixed(1) + "%"}</td>
    <td><div class="act"><button class="btn btn-sm btn-quiet" data-report="${esc(b.id)}" data-lot="${esc(b.lot)}">Report</button>
      <a class="btn btn-sm btn-quiet" href="/api/batch/${esc(b.id)}.csv">CSV</a></div></td></tr>`).join("");
  $("#batchEmpty").classList.toggle("hidden", d.batches.length > 0);
}

/* ----------------------------------------------------------------- review */
async function loadReview(reset) {
  const r = S.rev;
  if (reset) { r.offset = 0; r.items = []; }
  const q = new URLSearchParams({ status: r.status, result: r.result, date: r.date, offset: r.offset, limit: 60 });
  let d;
  try { d = await (await fetch(`/api/review?${q}`)).json(); } catch { return; }
  r.total = d.total;
  r.items = reset ? d.items : r.items.concat(d.items);
  r.offset = r.items.length;
  r.stats = d.stats;
  const ds = $("#revDate");
  const sig = d.dates.join("|");
  if (ds.dataset.sig !== sig) {
    ds.innerHTML = `<option value="">All days</option>` + d.dates.map((x) => `<option>${x}</option>`).join("");
    ds.dataset.sig = sig;
    ds.value = r.date;
  }
  renderReviewStats();
  renderReviewGrid();
}

function renderReviewStats() {
  const s = S.rev.stats;
  if (!s) return;
  const fr = s.false_reject_rate;
  $("#revStats").innerHTML = `<span>Reviewed<b>${s.reviewed}</b> / ${s.images}</span>
    <span class="${fr != null && fr > 10 ? "bad" : ""}">False-reject rate<b>${fr == null ? "—" : fr.toFixed(1) + "%"}</b></span>
    <span class="${s.missed ? "bad" : ""}">Missed defects<b>${s.missed}</b></span>`;
  const nc = $("#navReviewCount");
  nc.textContent = s.todo;
  nc.classList.toggle("hidden", !s.todo);
}

function verdictText(it) {
  if (!it.verdict) return "";
  if (it.result === "fail") return it.verdict === "correct" ? "Real defect" : "False reject";
  return it.verdict === "correct" ? "Good can" : "Missed defect";
}

function renderReviewGrid() {
  const r = S.rev;
  $("#reviewGrid").innerHTML = r.items.map((it, i) => `<button class="rcard" data-i="${i}">
    <div class="thumb"><img loading="lazy" src="/api/thumb?path=${encodeURIComponent(it.path)}" alt="">
      <span class="tag ${it.result === "fail" ? "fail" : "pass"}">${it.result === "fail" ? "REJECTED" : "PASSED"}</span>
      ${it.verdict ? `<span class="verdict ${it.verdict}">${verdictText(it)}</span>` : ""}</div>
    <div class="meta"><span><b>#${it.id}</b> · ${esc(it.time)}</span><span>${esc(it.label || it.detected || "—")}</span></div></button>`).join("");
  $("#reviewMore").classList.toggle("hidden", r.items.length >= r.total);
  const empty = $("#reviewEmpty");
  empty.classList.toggle("hidden", r.items.length > 0);
  empty.innerHTML = r.stats && r.stats.images === 0
    ? "No saved images yet.<br>Set <b>Settings › Detection › Save images</b> to Fails or All, select the Camera inspector and run the line."
    : "Nothing here with these filters.";
}

function renderReviewer() {
  const r = S.rev, it = r.items[r.idx];
  if (!it) { $("#reviewer").classList.remove("open"); r.idx = -1; return; }
  const eng = can("engineer");
  $("#rvImg").src = `/api/image?path=${encodeURIComponent(it.path)}${r.view === "raw" ? "&raw=1" : ""}`;
  $$("#rvView button").forEach((b) => b.classList.toggle("on", b.dataset.v === r.view));
  $("#rvTitle").textContent = `Can #${it.id}`;
  $("#rvFacts").innerHTML = `<dt>Date</dt><dd>${esc(it.date)}</dd><dt>Time</dt><dd>${esc(it.time)}</dd>
    <dt>Station decision</dt><dd>${it.result === "fail" ? "Rejected" : "Passed"}</dd>
    <dt>Detected</dt><dd>${esc(it.detected || "—")}</dd><dt>Position</dt><dd>${r.idx + 1} of ${r.total}</dd>`;
  const fail = it.result === "fail";
  $("#rvYes").textContent = fail ? "✓  Real defect — station was right" : "✓  Good can — station was right";
  $("#rvNo").textContent = fail ? "✕  False reject — it's a good can" : "✕  Missed defect — it's defective";
  const sel = $("#rvType");
  if (sel.dataset.n !== String(S.defectTypes.length)) {
    sel.innerHTML = S.defectTypes.map((t) => `<option>${esc(t)}</option>`).join("");
    sel.dataset.n = String(S.defectTypes.length);
  }
  const guess = S.defectTypes.find((t) => t.toLowerCase() === (it.label || it.detected || "").toLowerCase()) || (fail ? "Other" : "Dent");
  sel.value = guess;
  // the type belongs to "real defect" (rejected cans) or "missed defect" (passed cans)
  const wrap = $("#rvTypeWrap");
  if (fail) $("#rvYes").after(wrap); else $("#rvNo").after(wrap);
  $("#rvYes").classList.toggle("chosen", it.verdict === "correct");
  $("#rvNo").classList.toggle("chosen", it.verdict === "wrong");
  ["#rvYes", "#rvNo", "#rvClear", "#rvType"].forEach((s) => ($(s).disabled = !eng));
  $("#rvClear").classList.toggle("hidden", !it.verdict);
  $("#rvCurrent").textContent = it.verdict ? `Labelled "${verdictText(it)}"${it.label ? ` (${it.label})` : ""} by ${it.by}` : "Not labelled yet";
  $("#rvRo").textContent = eng ? "" : "Log in as engineer to label.";
  $("#rvPrev").disabled = r.idx <= 0;
  $("#rvNext").disabled = r.idx >= r.items.length - 1;
}

async function labelCurrent(verdict) {
  const r = S.rev, it = r.items[r.idx];
  if (!it || !can("engineer")) return;
  const fail = it.result === "fail";
  const typed = (verdict === "correct" && fail) || (verdict === "wrong" && !fail);
  const body = { path: it.path, verdict, defect_type: typed ? $("#rvType").value : "" };
  const d = await post("/api/review/label", body);
  if (!d) return;
  it.verdict = verdict; it.label = body.defect_type || ""; it.by = d.label.by;
  renderReviewGrid();
  if (verdict && r.idx < r.items.length - 1) r.idx++;
  renderReviewer();
  refreshReviewStats();
}

async function refreshReviewStats() {
  try {
    const d = await (await fetch(`/api/review?status=all&result=all&limit=1`)).json();
    S.rev.stats = d.stats;
    renderReviewStats();
  } catch {}
}

/* ----------------------------------------------------------------- alarms */
const fmtDur = (s) => {
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(s / 3600)} h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")} min`;
};
let alarmsLoading = false;
async function loadAlarms() {
  if (alarmsLoading) return;
  alarmsLoading = true;
  try { S.alarms = (await (await fetch("/api/alarms")).json()); renderAlarms(); } catch {}
  alarmsLoading = false;
}

function renderAlarms() {
  const a = S.alarms;
  if (!a) return;
  const steps = (x) => `<ol class="al-steps">${x.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>`;
  const time = (iso) => `${iso.slice(11, 16)}<span class="muted"> ${iso.slice(5, 10)}</span>`;
  let html = "";
  if (a.active.length) {
    html += a.active.map((x) => `<div class="al-active ${x.severity}">
      <div class="al-top"><span class="al-sev">${x.severity === "warning" ? "WARNING" : "FAULT"}</span><b>${esc(x.title)}</b>
        <span class="spacer"></span><span class="muted small">since ${esc(x.start.slice(11, 19))} · ${fmtDur(x.duration_s)}</span></div>
      <div class="al-msg">${esc(x.msg)}</div>
      <div class="al-check">What to check</div>${steps(x)}</div>`).join("");
  } else {
    html += `<div class="al-ok"><span class="dot ok"></span>No active alarms</div>`;
  }
  if (a.today.length) html += `<div class="al-today"><span class="muted small">Today:</span> ${a.today.map(([t, n]) => `<span class="pill">${esc(t)} <b>${n}</b></span>`).join("")}</div>`;
  if (a.history.length) {
    html += `<div class="al-hist"><div class="al-hist-head"><span>Started</span><span>Alarm</span><span>Lasted</span><span>Cleared</span></div>` +
      a.history.map((x) => `<div class="al-hist-row ${x.severity}"><span>${time(x.start)}</span>
        <span><b>${esc(x.title)}</b><small>${esc(x.msg)}</small></span><span>${fmtDur(x.duration_s)}</span><span class="muted">${esc(x.cleared)}</span>
        <div class="al-more">${steps(x)}</div></div>`).join("") + `</div>`;
  } else if (!a.active.length) {
    html += `<div class="muted small al-none">Alarms are recorded here with how long they lasted and what to check.</div>`;
  }
  const el = $("#alarms");
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
  const n = a.active.length;
  $("#alarmCount").textContent = n;
  $("#alarmCount").classList.toggle("hidden", !n);
}

/* ---------------------------------------------------------------- history */
async function loadHistory() {
  try { S.hist = await (await fetch(`/api/history?days=${S.histDays}`)).json(); } catch { return; }
  renderHistory();
}

function renderHistory() {
  const h = S.hist;
  if (!h) return;
  const t = h.total, y = h.yield;
  const yc = y == null ? "" : y >= 95 ? "ok" : y >= 85 ? "warn" : "bad";
  $("#histKpis").innerHTML = `
    <div><span>Cans inspected</span><b>${(t.n || 0).toLocaleString()}</b><small>${h.active_days} day${h.active_days === 1 ? "" : "s"} with production</small></div>
    <div><span>Yield</span><b class="${yc}">${y == null ? "—" : y.toFixed(1) + "%"}</b><small>${h.best ? `best ${h.best.yield.toFixed(1)}% · ${h.best.date.slice(5)}` : "&nbsp;"}</small></div>
    <div><span>Rejected</span><b>${(t.failed || 0).toLocaleString()}</b><small>${t.n ? (t.failed / t.n * 100).toFixed(1) + "% of cans" : "&nbsp;"}</small></div>
    <div><span>Not rejected</span><b class="${t.escaped ? "bad" : ""}">${(t.escaped || 0).toLocaleString()}</b><small>defects that got through</small></div>
    <div><span>Worst day</span><b>${h.worst ? h.worst.yield.toFixed(1) + "%" : "—"}</b><small>${h.worst ? h.worst.date : "&nbsp;"}</small></div>`;
  $("#histDefRange").textContent = `${h.from.slice(5)} – ${h.to.slice(5)}`;
  const dEl = $("#histDefects");
  if (!h.defects.length) dEl.innerHTML = `<div class="muted small" style="padding:8px 2px">No defects in this period.</div>`;
  else {
    const peak = h.defects[0][1];
    dEl.innerHTML = h.defects.map(([name, n]) => `<div class="def-row"><span>${esc(name)}</span><div class="def-bar"><span style="width:${Math.max(3, n / peak * 100)}%"></span></div>
      <span class="def-num"><b>${n.toLocaleString()}</b> · ${(n / t.failed * 100).toFixed(0)}%</span></div>`).join("");
  }
  const days = h.days.filter((d) => d.n).reverse();
  $("#histRows").innerHTML = days.map((d) => `<tr>
    <td><b>${esc(d.date)}</b> <span class="muted small">${new Date(d.date + "T12:00").toLocaleDateString(undefined, { weekday: "short" })}</span></td>
    <td class="num">${d.n.toLocaleString()}</td><td class="num">${d.failed.toLocaleString()}</td>
    <td class="num"${d.escaped ? ' style="color:var(--orange);font-weight:700"' : ""}>${d.escaped}</td>
    <td class="num">${d.yield.toFixed(1)}%</td><td>${d.defects.length ? esc(d.defects[0][0]) + ` <span class="muted">(${d.defects[0][1]})</span>` : "—"}</td>
    <td><a class="btn btn-sm btn-quiet" href="/api/day/${d.date}.csv">CSV</a></td></tr>`).join("");
  $("#histEmpty").classList.toggle("hidden", days.length > 0);
  drawHistChart();
}

function drawHistChart() {
  const h = S.hist, cv = $("#histChart");
  if (!h || !cv) return;
  const box = cv.parentElement.getBoundingClientRect();
  if (box.width < 10 || box.height < 10) return;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(box.width * dpr); cv.height = Math.round(box.height * dpr);
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = box.width, H = box.height;
  g.clearRect(0, 0, W, H);
  g.font = "11px InterLocal, system-ui, sans-serif";
  const days = h.days, N = days.length;
  const padL = 46, padR = 40, padT = 10, padB = 24, cw = W - padL - padR, ch = H - padT - padB;
  const maxN = Math.max(4, ...days.map((d) => d.n));
  const mag = Math.pow(10, Math.floor(Math.log10(maxN / 4)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s * 4 >= maxN) || maxN / 4;
  const top = step * 4;
  g.strokeStyle = "rgba(148,163,184,.10)"; g.fillStyle = "#5a6577"; g.lineWidth = 1;
  for (let k = 0; k <= 4; k++) {
    const y = padT + ch - (ch * k) / 4 + 0.5;
    g.beginPath(); g.moveTo(padL, y); g.lineTo(W - padR, y); g.stroke();
    g.textAlign = "right"; g.fillText(Math.round(top * k / 4).toLocaleString(), padL - 8, y + 4);
    g.textAlign = "left"; g.fillText(`${k * 25}%`, W - padR + 8, y + 4);
  }
  const bw = cw / N;
  g.textAlign = "center";
  const every = Math.ceil(N / Math.max(1, Math.floor(cw / 46)));
  days.forEach((d, i) => {
    if ((N - 1 - i) % every === 0) g.fillText(d.date.slice(5).replace("-", "/"), padL + (i + 0.5) * bw, H - 7);
  });
  if (!h.total.n) {
    g.fillStyle = "#8b97a8"; g.font = "13px InterLocal, system-ui, sans-serif";
    g.fillText("No inspections in this period yet", padL + cw / 2, padT + ch / 2);
    return;
  }
  days.forEach((d, i) => {
    if (!d.n) return;
    const x = padL + i * bw + bw * 0.16, w = Math.max(2, bw * 0.68);
    const hp = (d.passed / top) * ch, hf = (d.failed / top) * ch;
    g.fillStyle = "rgba(59,130,246,.6)"; g.fillRect(x, padT + ch - hp - hf, w, hp);
    g.fillStyle = "rgba(239,68,68,.9)"; g.fillRect(x, padT + ch - hf, w, hf);
  });
  g.strokeStyle = "#22c55e"; g.lineWidth = 2; g.lineJoin = "round"; g.beginPath();
  let started = false;
  const pts = [];
  days.forEach((d, i) => {
    if (!d.n) return;
    const x = padL + (i + 0.5) * bw, y = padT + ch - (d.yield / 100) * ch;
    pts.push([x, y]); started ? g.lineTo(x, y) : g.moveTo(x, y); started = true;
  });
  g.stroke();
  g.fillStyle = "#22c55e";
  pts.forEach(([x, y]) => { g.beginPath(); g.arc(x, y, 2.8, 0, Math.PI * 2); g.fill(); });
}

/* ------------------------------------------------------------ diagnostics */
async function loadDiag() {
  try { S.diag = await (await fetch("/api/diag")).json(); } catch { return; }
  renderDiag();
}

function renderDiag() {
  const d = S.diag, st = S.status;
  if (!d) return;
  const b = d.belt, eng = can("engineer");
  // sensor
  $("#dgSensorMode").textContent = d.sim ? "simulated" : d.sensor_mode;
  const sEl = $("#dgSensor");
  sEl.classList.toggle("on", !!b.active);
  sEl.classList.toggle("bad", !!d.sensor_error);
  $("#dgSensorText").textContent = d.sensor_error ? `Error: ${d.sensor_error}` : b.active ? "BLOCKED — can in the beam" : "CLEAR";
  $("#dgSensorFacts").innerHTML = `<dt>Triggers</dt><dd>${b.count.toLocaleString()}</dd>
    <dt>Last blocked for</dt><dd>${b.last_block_ms == null ? "—" : Math.round(b.last_block_ms) + " ms"}</dd>
    <dt>Last can</dt><dd>${b.last_age_s == null ? "—" : b.last_age_s < 60 ? Math.round(b.last_age_s) + " s ago" : fmtDur(b.last_age_s) + " ago"}</dd>
    <dt>Wiring</dt><dd>GPIO 25 · header pin 22</dd>`;
  // speed
  const t = d.timing;
  if (b.speed_mm_s) {
    $("#dgSpeed").innerHTML = `${(b.speed_mm_s / 1000).toFixed(2)}<small> m/s</small>`;
    $("#dgSpeedSub").textContent = `${(b.speed_mm_s * 60 / 1000).toFixed(1)} m/min · ${b.n} cans`;
    $("#dgSpread").innerHTML = `±${(b.spread_pct / 2).toFixed(0)}<small>%</small>`;
    $("#dgSpread").className = `speed-v ${b.spread_pct > 30 ? "bad" : b.spread_pct > 15 ? "warn" : ""}`;
  } else {
    $("#dgSpeed").textContent = "—"; $("#dgSpeedSub").textContent = "pass a few cans"; $("#dgSpread").textContent = "—";
  }
  $("#dgGap").innerHTML = b.min_gap_ms == null ? "—" : `${Math.round(b.min_gap_ms)}<small> ms</small>`;
  const row = (label, key, now, rec, need) => {
    const recTxt = rec == null ? `<span class="muted">${need}</span>` : `${rec} ms`;
    const diff = rec != null && Math.abs(rec - now) > Math.max(5, now * 0.05);
    const btn = rec != null && diff ? `<button class="btn btn-sm btn-primary" data-apply="${key}" data-v="${rec}" ${eng ? "" : "disabled"}>Apply</button>`
      : rec != null ? `<span class="ok small">✓ matches</span>` : "";
    return `<tr><td>${label}</td><td class="num">${now} ms</td><td class="num">${recTxt}</td><td>${btn}</td></tr>`;
  };
  const html = row("Capture delay", "camera_delay_ms", t.camera_delay_ms, b.rec_camera_delay_ms,
      !b.speed_mm_s ? "needs belt speed" : "enter sensor → camera") +
    row("Reject delay", "reject_delay_ms", t.reject_delay_ms, b.rec_reject_delay_ms,
      !b.speed_mm_s ? "needs belt speed" : "enter sensor → nozzle");
  if ($("#dgTiming").dataset.html !== html) { $("#dgTiming").innerHTML = html; $("#dgTiming").dataset.html = html; }
  // checks
  const checks = [];
  const budget = t.reject_delay_ms - t.camera_delay_ms;
  if (t.avg_inspect_ms != null) {
    const okT = t.avg_inspect_ms < budget * 0.7;
    checks.push([okT ? "ok" : t.avg_inspect_ms < budget ? "warn" : "bad",
      `Inspection takes ${Math.round(t.avg_inspect_ms)} ms; the air needs the decision within ${budget} ms of capture` +
      (okT ? "" : " — too tight: increase reject delay / move the nozzle further, or make inspection faster")]);
  } else checks.push(["", `Time for inspection: ${budget} ms (reject delay − capture delay). Run some cans to compare.`]);
  if (b.min_gap_ms != null) {
    const okG = b.min_gap_ms > t.burst_ms;
    checks.push([okG ? "ok" : "bad", `Closest cans ${Math.round(b.min_gap_ms)} ms apart, air burst ${t.burst_ms} ms` +
      (okG ? "" : " — the burst is longer than the gap: neighbouring cans can be blown off too. Shorten the burst.")]);
  }
  if (b.speed_mm_s && b.spread_pct > 15)
    checks.push([b.spread_pct > 30 ? "bad" : "warn", "Belt speed varies a lot between cans — the air may miss. Check the conveyor drive / tension."]);
  if (b.speed_mm_s) checks.push(["", `How speed is measured: can diameter (${t.can_diameter_mm} mm) ÷ time the can blocks the sensor. Recommended delays put the can centre under the camera and centre the air burst on the can.`]);
  $("#dgChecks").innerHTML = checks.map(([c, x]) => `<li class="${c}"><span class="dot ${c}"></span><span>${esc(x)}</span></li>`).join("");
  // outputs
  const stopped = st && !st.running && !st.estop;
  $("#dgSelftest").disabled = !eng || !stopped || d.selftest;
  $("#dgSelftest").firstChild.textContent = d.selftest ? "Self-test running…" : "Run self-test";
  const ov = $("#dgOverride");
  ov.checked = !!(st && st.manual);
  ov.disabled = !eng || !stopped;
  $$("#dgLamps .lamp-btn").forEach((x) => {
    x.disabled = !eng || !(st && st.manual);
    x.classList.toggle("lit", !!(st && st.lights[x.dataset.key]));
  });
  $('#pageDiag [data-cmd="test_burst"]').disabled = !eng || !stopped;
  // camera
  const c = d.camera;
  $("#dgCamera").innerHTML = `<dt>Source</dt><dd>${esc(c.source)}</dd>
    <dt>Status</dt><dd class="${c.ok ? "ok" : "bad"}">${esc(c.ok ? "running" : c.status)}</dd>
    <dt>Frame rate</dt><dd>${c.fps} fps${c.resolution ? ` · ${c.resolution[0]}×${c.resolution[1]}` : ""}</dd>
    ${c.ping ? `<dt>IP</dt><dd>${esc(c.ip)} · ${c.online === true ? `<span class="ok">reachable ${Math.round(c.ms)} ms</span>` : c.online === false ? '<span class="bad">not reachable</span>' : "checking…"}</dd>` : ""}
    ${d.cam_temp != null ? `<dt>Temperature</dt><dd>${Math.round(d.cam_temp)} °C</dd>` : ""}
    <dt>Details</dt><dd>${esc(c.info || "—")}</dd>`;
  // system
  if (d.disk_total_mb) {
    const used = 1 - d.disk_free_mb / d.disk_total_mb;
    $("#dgDiskText").textContent = `${(d.disk_free_mb / 1024).toFixed(1)} GB free of ${(d.disk_total_mb / 1024).toFixed(0)} GB · images ${d.images_mb >= 1024 ? (d.images_mb / 1024).toFixed(1) + " GB" : Math.round(d.images_mb) + " MB"}`;
    const bar = $("#dgDiskBar");
    bar.style.width = `${used * 100}%`;
    bar.style.background = d.disk_free_mb < 1024 ? "var(--red)" : used > 0.8 ? "var(--amber)" : "var(--cyan)";
  }
  const p = d.power || {};
  $("#dgSystem").innerHTML = `<dt>Computer</dt><dd>${esc(d.model)}</dd>
    <dt>Temperature</dt><dd>${d.cpu_temp == null ? "—" : `<span class="${d.thermal === "ok" ? "ok" : d.thermal === "warm" ? "warn" : "bad"}">${Math.round(d.cpu_temp)} °C · ${d.thermal}</span>`}</dd>
    ${d.is_pi ? `<dt>Power</dt><dd>${p.undervoltage_now ? '<span class="bad">under-voltage now</span>' : p.undervoltage_seen ? '<span class="warn">under-voltage since boot</span>' : '<span class="ok">OK</span>'}</dd>` : ""}
    <dt>Edition</dt><dd>${d.edition === "full" ? "Full (with YOLO)" : "Lite (no YOLO)"} · v${esc(d.version)}</dd>
    <dt>Running for</dt><dd>${fmtDur(d.uptime)}</dd>`;
}

/* ------------------------------------------------------------------ audit */
function renderAudit() {
  const rows = S.audit.slice(-200).reverse().map((e) => `<tr><td>${esc(e.t.slice(11, 19))}<br><span class="muted small">${esc(e.t.slice(0, 10))}</span></td>
    <td class="who"><b>${esc(e.who)}</b><span class="role ${esc(e.role)}">${esc(e.role)}</span></td>
    <td>${esc(e.action)}</td><td>${esc(e.detail)}</td><td class="muted">${esc(e.source)}</td></tr>`).join("");
  $("#auditRows").innerHTML = rows || `<tr><td colspan="5" class="muted">No actions recorded yet.</td></tr>`;
}

/* --------------------------------------------------------- inspection area */
const AREA = { on: false, start: null, rect: null, prevView: null };

function imgContentBox() {
  const el = $("#liveImg"), b = el.getBoundingClientRect();
  const nw = el.naturalWidth || 4, nh = el.naturalHeight || 3;
  const sc = Math.min(b.width / nw, b.height / nh);
  const w = nw * sc, h = nh * sc;
  return { left: b.left + (b.width - w) / 2, top: b.top + (b.height - h) / 2, width: w, height: h };
}

function showAreaBox() {
  const box = $("#areaBox"), r = AREA.rect;
  box.classList.toggle("hidden", !r);
  if (!r) return;
  const c = imgContentBox(), vp = $("#viewport").getBoundingClientRect();
  box.style.left = `${c.left - vp.left + r[0] * c.width}px`;
  box.style.top = `${c.top - vp.top + r[1] * c.height}px`;
  box.style.width = `${(r[2] - r[0]) * c.width}px`;
  box.style.height = `${(r[3] - r[1]) * c.height}px`;
}

function areaMode(on) {
  AREA.on = on;
  $("#areaDraw").classList.toggle("hidden", !on);
  const st = S.status;
  if (on) {
    AREA.prevView = st && st.camera ? st.camera.view : "detect";
    if (AREA.prevView !== "raw") setSetting("live_view", "raw");     // draw on the plain, full picture
    const a = st && st.settings.inspect_area;
    AREA.rect = a ? [...a] : null;
    setTimeout(showAreaBox, 150);
  } else if (AREA.prevView && AREA.prevView !== "raw") {
    setSetting("live_view", AREA.prevView);
  }
}

function wireArea() {
  $("#btnArea").addEventListener("click", () => areaMode(!AREA.on));
  const pad = $("#areaDraw");
  const frac = (e) => {
    const c = imgContentBox();
    return [Math.min(1, Math.max(0, (e.clientX - c.left) / c.width)), Math.min(1, Math.max(0, (e.clientY - c.top) / c.height))];
  };
  pad.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".area-bar")) return;
    pad.setPointerCapture(e.pointerId);
    AREA.start = frac(e);
    AREA.rect = [...AREA.start, ...AREA.start];
    showAreaBox();
  });
  pad.addEventListener("pointermove", (e) => {
    if (!AREA.start) return;
    const p = frac(e);
    AREA.rect = [Math.min(AREA.start[0], p[0]), Math.min(AREA.start[1], p[1]), Math.max(AREA.start[0], p[0]), Math.max(AREA.start[1], p[1])];
    showAreaBox();
  });
  pad.addEventListener("pointerup", () => { AREA.start = null; });
  $("#areaSave").addEventListener("click", async () => {
    const r = AREA.rect;
    if (!r || r[2] - r[0] < 0.05 || r[3] - r[1] < 0.05) return toast("Drag a bigger box first", "error");
    if (await setSetting("inspect_area", r.map((v) => +v.toFixed(4)))) { toast("Inspection area saved", "ok"); areaMode(false); }
  });
  $("#areaFull").addEventListener("click", async () => {
    if (await setSetting("inspect_area", null)) { toast("Inspecting the whole view", "ok"); areaMode(false); }
  });
  $("#areaCancel").addEventListener("click", () => areaMode(false));
  window.addEventListener("resize", () => { if (AREA.on) showAreaBox(); });
}

/* -------------------------------------------------------------- UI wiring */
function openModal(path, title) {
  $("#modalImg").src = `/api/image?path=${encodeURIComponent(path)}`;
  $("#modalRaw").href = `/api/image?path=${encodeURIComponent(path)}&raw=1`;
  $("#modalTitle").textContent = title;
  $("#modal").classList.add("open");
}

function wire() {
  $$("[data-cmd]").forEach((b) => b.addEventListener("click", () => cmd(b.dataset.cmd)));
  $("#overrideToggle").addEventListener("change", (e) => cmd("override", { on: e.target.checked }));
  $$("#viewSeg button").forEach((b) => b.addEventListener("click", () => setSetting("live_view", b.dataset.v)));

  $$(".tab").forEach((t) => t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === t.dataset.tab));
    if (t.dataset.tab === "log") { $("#logAlert").classList.add("hidden"); const l = $("#log"); l.scrollTop = l.scrollHeight; }
  }));
  $("#btnClear").addEventListener("click", () => {
    if (confirm("Clear this session's results and statistics?\n(Files saved on the station are kept.)")) cmd("clear_session");
  });
  $("#rows").addEventListener("click", (e) => {
    const tr = e.target.closest("tr");
    const r = tr && S.byId.get(+tr.dataset.id);
    if (r && r.img) openModal(r.img, `Can #${r.id} — ${r.pass ? "pass" : r.defect}`);
  });
  $("#lastThumb").addEventListener("click", () => {
    const img = $("#lastThumb").dataset.img;
    if (img) openModal(img, "Last inspected can");
    else if ($("#lastImg").src) { $("#modalImg").src = $("#lastImg").src; $("#modalRaw").removeAttribute("href"); $("#modalTitle").textContent = "Last inspected can (not saved)"; $("#modal").classList.add("open"); }
  });
  $("#modal").addEventListener("click", (e) => { if (e.target.id === "modal" || e.target.closest("#modalClose")) $("#modal").classList.remove("open"); });

  const drawer = (open) => { $("#drawer").classList.toggle("open", open); $("#scrim").classList.toggle("open", open); };
  $("#btnSettings").addEventListener("click", () => drawer(true));
  $("#btnCloseDrawer").addEventListener("click", () => drawer(false));
  $("#scrim").addEventListener("click", () => drawer(false));
  $("#btnFullscreen").addEventListener("click", toggleFullscreen);
  wireConveyor();

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); cmd("estop"); return; }      // always: safety first
    const typing = e.target.matches("input, textarea, select");
    if (typing) return;
    if (S.rev.idx >= 0 && $("#reviewer").classList.contains("open")) {
      if (e.key === "ArrowLeft" && S.rev.idx > 0) { S.rev.idx--; renderReviewer(); }
      else if (e.key === "ArrowRight" && S.rev.idx < S.rev.items.length - 1) { S.rev.idx++; renderReviewer(); }
      else if (e.key === "1") labelCurrent("correct");
      else if (e.key === "2") labelCurrent("wrong");
      return;
    }
    if (e.key === " " && S.status && S.status.running && S.canControl) { e.preventDefault(); cmd("simulate"); }
    if (e.key === "f" || e.key === "F") toggleFullscreen();
    if (e.key === "c" || e.key === "C") toggleConveyor();
  });

  // ---- navigation, login
  $$("#nav button").forEach((b) => b.addEventListener("click", () => setPage(b.dataset.page)));
  $("#btnUser").addEventListener("click", async () => {
    if (S.role === "engineer") {
      if (confirm(`Log out ${S.name}?`)) { await post("/api/logout"); refreshMe(); }
    } else openLogin();
  });
  $("#drawerLogin").addEventListener("click", openLogin);
  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#loginName").value.trim(), pin = $("#loginPin").value;
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, pin }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { $("#loginErr").textContent = d.error || "Login failed"; $("#loginPin").value = ""; $("#loginPin").focus(); return; }
    try { localStorage.setItem("engName", name); } catch {}
    $("#loginModal").classList.remove("open");
    toast(`Logged in as engineer (${name})`, "ok");
    await refreshMe();
    if (d.default_pin) toast("Please change the default engineer PIN (Settings › Security)", "error");
  });
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => closeDialog(b)));
  $$(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m && m.id !== "modal") closeDialog(m.firstElementChild); }));

  // ---- recipes
  $("#recipeSelect").addEventListener("change", async (e) => {
    const name = e.target.value;
    if (!(await post("/api/recipes/select", { name }))) e.target.value = S.status.recipe.active;
    else toast(`Recipe: ${name}`, "ok");
  });
  $("#recipeSave").addEventListener("click", () => post("/api/recipes/save"));
  $("#recipeCreate").addEventListener("click", async () => {
    const name = $("#recipeNew").value.trim();
    if (!name) return toast("Enter a name for the new recipe", "error");
    if (await post("/api/recipes/create", { name })) $("#recipeNew").value = "";
  });
  $("#recipeDelete").addEventListener("click", () => {
    const name = $("#recipeDelSel").value;
    if (name && confirm(`Delete recipe "${name}"? This cannot be undone.`)) post("/api/recipes/delete", { name });
  });
  $("#pinSave").addEventListener("click", async () => {
    if (await post("/api/pin", { pin: $("#pinNew").value.trim() })) { $("#pinNew").value = ""; refreshMe(); }
  });

  // ---- batches & reports
  $("#batchBox").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-batch]");
    if (!b || b.disabled) return;
    if (b.dataset.batch === "start") {
      $("#batchErr").textContent = "";
      $("#batchLot").value = ""; $("#batchNote").value = "";
      try { $("#batchOp").value = localStorage.getItem("batchOp") || ""; } catch {}
      $("#batchRecipe").textContent = S.status ? S.status.recipe.active : "—";
      openDialog("#batchModal");
      setTimeout(() => $("#batchLot").focus(), 60);
    } else if (b.dataset.batch === "end") {
      if (!confirm(`End batch ${b.dataset.lot}? Its report will be finalised.`)) return;
      const d = await post("/api/batch/end");
      if (d) openReport(d.id, `Batch report — Lot ${b.dataset.lot}`);
    } else if (b.dataset.batch === "report") openReport(b.dataset.id, "Live batch report");
  });
  $("#batchForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { lot: $("#batchLot").value, operator: $("#batchOp").value, note: $("#batchNote").value };
    const r = await fetch("/api/batch/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) { $("#batchErr").textContent = d.error || "Could not start the batch"; return; }
    try { localStorage.setItem("batchOp", body.operator); } catch {}
    $("#batchModal").classList.remove("open");
    toast(`Batch ${body.lot.trim()} started`, "ok");
  });
  $("#batchRows").addEventListener("click", (e) => {
    const b = e.target.closest("[data-report]");
    if (b) openReport(b.dataset.report, `Batch report — Lot ${b.dataset.lot}`);
  });
  $("#reportPrint").addEventListener("click", () => { try { $("#reportFrame").contentWindow.print(); } catch {} });

  // ---- review
  const segPick = (id, key) => $$(`#${id} button`).forEach((b) => b.addEventListener("click", () => {
    $$(`#${id} button`).forEach((x) => x.classList.toggle("on", x === b));
    S.rev[key] = b.dataset.v;
    loadReview(true);
  }));
  segPick("revStatus", "status");
  segPick("revResult", "result");
  $("#revDate").addEventListener("change", (e) => { S.rev.date = e.target.value; loadReview(true); });
  $("#reviewMore").addEventListener("click", () => loadReview(false));
  $("#reviewGrid").addEventListener("click", (e) => {
    const c = e.target.closest(".rcard");
    if (!c) return;
    S.rev.idx = +c.dataset.i;
    renderReviewer();
    openDialog("#reviewer");
  });
  $("#rvPrev").addEventListener("click", () => { S.rev.idx--; renderReviewer(); });
  $("#rvNext").addEventListener("click", () => { S.rev.idx++; renderReviewer(); });
  $("#rvYes").addEventListener("click", () => labelCurrent("correct"));
  $("#rvNo").addEventListener("click", () => labelCurrent("wrong"));
  $("#rvClear").addEventListener("click", () => labelCurrent(null));
  $$("#rvView button").forEach((b) => b.addEventListener("click", () => { S.rev.view = b.dataset.v; renderReviewer(); }));

  // ---- alarms
  $("#reasons").addEventListener("click", () => {
    if (!(S.status && (S.status.faults.length || S.status.warning))) return;
    $('.tab[data-tab="alarms"]').click();
    $(".tabs-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  $("#alarms").addEventListener("click", (e) => {
    const h = e.target.closest(".al-hist-row");
    if (h) h.classList.toggle("open");
  });
  // ---- history
  $$("#histRange button").forEach((b) => b.addEventListener("click", () => {
    $$("#histRange button").forEach((x) => x.classList.toggle("on", x === b));
    S.histDays = +b.dataset.v;
    loadHistory();
  }));
  // ---- diagnostics
  $("#dgSelftest").addEventListener("click", () => cmd("selftest"));
  $("#dgOverride").addEventListener("change", (e) => cmd("override", { on: e.target.checked }));
  $$("#dgLamps .lamp-btn").forEach((b) => b.addEventListener("click", () => cmd("lamp", { key: b.dataset.key })));
  $("#dgTiming").addEventListener("click", (e) => {
    const b = e.target.closest("[data-apply]");
    if (b) setSetting(b.dataset.apply, +b.dataset.v).then((r) => r && toast("Applied", "ok"));
  });
  window.addEventListener("resize", () => { if (S.page === "history") drawHistChart(); });

  setInterval(refreshMe, 20000);
  const hashPage = location.hash.slice(1);
  if (["review", "reports", "history", "diag", "motors"].includes(hashPage)) setPage(hashPage);

  wireArea();
  $("#liveImg").addEventListener("error", () => { S.liveOn = false; });   // retried on next status


}

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
}

buildTower();
initSettingWidgets();
wire();
connect();


/* ------------------------------------------------------------ motors page */
const CONV = { editing: {} };
const convPost = (action, body) => post(`/api/conveyor/${action}`, body);
const cSpeed = (pct) => 53 + Math.floor(pct * 30 / 100 + 0.5);              // same as main.c

function toggleConveyor() { setPage(S.page === "motors" ? "line" : "motors"); }

function wireConveyor() {
  $("#btnConveyor").addEventListener("click", toggleConveyor);
  const slider = (id, key, label, fmt) => {
    const el = $(id);
    let t = null;
    el.addEventListener("input", () => {
      $(label).textContent = fmt(+el.value);
      CONV.editing[key] = Date.now();
      clearTimeout(t);
      t = setTimeout(() => setSetting(key, +el.value), 150);
    });
  };
  slider("#convSpeed", "conveyor_speed_pct", "#convSpeedVal", (v) => `${v}%`);
  slider("#convSteps", "conveyor_move_steps", "#convStepsVal", (v) => `${v} steps`);
  slider("#mountSteps", "mount_move_steps", "#mountStepsVal", (v) => `${v} steps`);

  // pressing the active button again stops it - like the motor program
  $$("[data-belt]").forEach((b) => b.addEventListener("click", () => {
    if (b.classList.contains("active")) { convPost("stop"); return; }
    const k = b.dataset.belt, steps = +$("#convSteps").value;
    if (k === "auto") convPost("auto", { steps });
    else convPost("move", { dir: k === "fwd" ? 1 : -1, steps });
  }));
  $("#convRun").addEventListener("click", () => convPost($("#convRun").classList.contains("active") ? "stop" : "run", { dir: 1 }));
  $("#convStopAll").addEventListener("click", () => convPost("stop_all"));
  $$("[data-mount]").forEach((b) => b.addEventListener("click", () => {
    if (b.classList.contains("active")) { convPost("mount_stop"); return; }
    convPost("mount_move", { dir: +b.dataset.mount, steps: +$("#mountSteps").value });
  }));
  $("#mountRef").addEventListener("click", () => convPost("mount_reference"));
  $("#convWithLine").addEventListener("change", (e) => setSetting("conveyor_with_line", e.target.checked));
  $("#mountFitted").addEventListener("change", (e) => setSetting("mount_motor", e.target.checked));
  $("#motorPrecise").addEventListener("change", (e) => setSetting("motor_timing", e.target.checked ? "precise" : "team"));
  $$("[data-lamp]").forEach((b) => b.addEventListener("click", () => cmd("lamp", { key: b.dataset.lamp })));
  $("#mcValve").addEventListener("click", () => cmd("test_burst"));
  $("#mcLightsAuto").addEventListener("click", () => cmd("lights_auto"));
}

function renderConveyor(c) {
  if (!c) return;
  const st = S.status || {}, op = can("operator"), off = !op || st.estop;
  $("#convDot").classList.toggle("on", c.running || c.mount_running);
  if (S.page !== "motors") return;                 // nothing else to draw while hidden

  const err = $("#convErr");
  err.classList.toggle("hidden", !c.error);
  if (c.error) err.textContent = `Motors not available - simulated. ${c.error}`;
  $("#convDriver").textContent = `Driver: ${c.driver_name}`;
  const sync = (id, key, label, val, fmt) => {
    const el = $(id);
    if (Date.now() - (CONV.editing[key] || 0) > 1500) el.value = val;
    $(label).textContent = fmt(+el.value);
    el.disabled = !op;
  };
  sync("#convSpeed", "conveyor_speed_pct", "#convSpeedVal", c.speed_pct, (v) => `${v}%`);
  sync("#convSteps", "conveyor_move_steps", "#convStepsVal", c.move_steps, (v) => `${v} steps`);
  sync("#mountSteps", "mount_move_steps", "#mountStepsVal", c.mount_move_steps, (v) => `${v} steps`);

  const steps = +$("#convSteps").value;
  const act = { fwd: c.running && c.mode === "move" && c.dir > 0, rev: c.running && c.mode === "move" && c.dir < 0,
                auto: c.running && c.mode === "auto" };
  $$("[data-belt]").forEach((b) => { const a = act[b.dataset.belt]; b.classList.toggle("active", a); b.disabled = a ? !S.canControl : off; });
  const prog = (a) => `Steps ${a ? c.steps_done : 0} / ${a ? c.steps_requested : steps}`;
  $("#convFwdSteps").textContent = prog(act.fwd);
  $("#convRevSteps").textContent = prog(act.rev);
  $("#convAutoSteps").textContent = prog(act.auto);
  const runOn = c.running && c.mode === "continuous";
  $("#convRun").classList.toggle("active", runOn);
  $("#convRun").disabled = runOn ? !S.canControl : off;
  $("#convStopAll").disabled = !S.canControl;
  $("#convState").textContent = "Conveyor: " + (st.estop ? "E-stop" : !c.running ? "Stopped" :
    c.mode === "auto" ? (c.dir > 0 ? "Auto forward →" : "Auto reverse ←") :
    c.mode === "continuous" ? "Running →" : (c.dir > 0 ? "Forward →" : "Reverse ←"));

  // camera height (only when fitted)
  $$(".mount-only").forEach((el) => el.classList.toggle("hidden", !c.mount_fitted));
  const mf = $("#mountFitted"); mf.checked = c.mount_fitted; mf.disabled = !can("engineer");
  const wl = $("#convWithLine"); wl.checked = c.with_line; wl.disabled = !can("engineer");
  const mp = $("#motorPrecise"); mp.checked = (st.settings || {}).motor_timing !== "team";
  mp.disabled = !can("operator") || c.running;
  if (c.mount_fitted) {
    const msteps = +$("#mountSteps").value;
    $$("[data-mount]").forEach((b) => {
      const a = c.mount_running && +b.dataset.mount === c.mount_dir;
      b.classList.toggle("active", a); b.disabled = a ? !S.canControl : off;
    });
    const up = c.mount_running && c.mount_dir > 0, dn = c.mount_running && c.mount_dir < 0;
    $("#mUpTxt").textContent = `Steps ${up ? c.mount_steps_done : 0} / ${up ? c.mount_requested : msteps}`;
    $("#mDownTxt").textContent = `Steps ${dn ? c.mount_steps_done : 0} / ${dn ? c.mount_requested : msteps}`;
    const pre = $("#mountPresets");
    if (!pre.children.length) Object.entries(c.presets).forEach(([name, pos]) => {
      const b = document.createElement("button");
      b.className = "mc-flat"; b.textContent = name.replace(" ", ""); b.dataset.pos = pos;
      b.addEventListener("click", () => convPost("mount_goto", { position: pos }));
      pre.appendChild(b);
    });
    const ref = c.mount_position !== null && c.mount_position !== undefined;
    $$("#mountPresets button").forEach((b) => { b.disabled = off || !ref; b.classList.toggle("on", ref && !c.mount_running && c.mount_position === +b.dataset.pos); });
    $("#mountPos").textContent = ref ? `12oz reference saved · Camera at ${c.mount_position} steps` : "Calibrate 12oz height before using presets";
    $("#mountRef").disabled = off || c.mount_running;
    $("#mountState").textContent = "Camera: " + (!c.mount_running ? "Stopped" : c.mount_dir > 0 ? "Up ↑" : "Down ↓");
  }

  // sensor + lights (same column as the motor program)
  const sen = $("#mcSensor");
  sen.textContent = st.sensor ? "● Can detected" : "○ No can";
  sen.classList.toggle("on", !!st.sensor);
  const lights = st.lights || {};
  $$("[data-lamp]").forEach((b) => {
    const on = !!lights[b.dataset.lamp];
    b.classList.toggle("on", on);
    b.querySelector("small").textContent = on ? "ON" : "OFF";
    b.disabled = !op || st.running || st.estop;
  });
  $("#mcValve").classList.toggle("on", !!st.valve);
  $("#mcValve").disabled = !op || st.running || st.estop;
  $("#mcLightsAuto").disabled = !op || !st.manual;
  $("#mcLightsAuto").textContent = st.manual ? "Lights: back to automatic" : "Lights: automatic";
}
