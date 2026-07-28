/* ATLAS // observatory — client. Session/Telegram auth, reactive backdrop, live telemetry. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, c, h) => { const n = document.createElement(t); if (c) n.className = c; if (h != null) n.innerHTML = h; return n; };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const num = (v, d = 0) => (v == null || isNaN(v)) ? d : v;

/* ------------------------------------------------------------------ auth */
const TG = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData
  ? window.Telegram.WebApp : null;
const SKEY = "atlas.session";
let sessionTok = "";
let installUrl = "";

function readFragmentToken() {
  const m = /[#&]s=([A-Za-z0-9._-]+)/.exec(location.hash || "");
  if (m) {
    try { localStorage.setItem(SKEY, m[1]); } catch (_) {}
    history.replaceState(null, "", location.pathname + location.search);
    return m[1];
  }
  return "";
}
function authHeaders() {
  const h = {};
  if (TG && TG.initData) h["X-Telegram-Init-Data"] = TG.initData;
  else if (sessionTok) h["Authorization"] = "Bearer " + sessionTok;
  return h;
}
async function api(path) {
  const r = await fetch(path, { headers: authHeaders(), credentials: "same-origin", cache: "no-store" });
  if (r.status === 403 || r.status === 401) throw { forbidden: true };
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}
async function ensureSession() {
  // In Telegram we can mint a durable token so a standalone install works later.
  try {
    const j = await api("/api/session");
    if (j && j.token) {
      sessionTok = j.token;
      try { localStorage.setItem(SKEY, sessionTok); } catch (_) {}
      installUrl = location.origin + "/#s=" + sessionTok;
    }
  } catch (_) {}
}

/* ------------------------------------------------------------------ boot */
const bootLog = (t) => { const b = $("#bootlog"); if (b) b.textContent = (b.textContent + t + "\n").split("\n").slice(-6).join("\n"); };

async function boot() {
  initBackdrop();  // фон живёт с самого старта — и на гейте тоже
  sessionTok = readFragmentToken() || (localStorage.getItem(SKEY) || "");
  bootLog("> link " + (TG ? "telegram" : sessionTok ? "device-session" : "none"));
  if (TG) { try { TG.ready(); TG.expand(); TG.disableVerticalSwipes && TG.disableVerticalSwipes(); } catch (_) {} }
  bootLog("> probing owner authority…");
  let ok = false;
  try { await api("/api/overview"); ok = true; } catch (e) { ok = false; }
  if (ok && TG) { await ensureSession(); }
  else if (ok && sessionTok) { installUrl = location.origin + "/#s=" + sessionTok; }
  bootLog(ok ? "> authority: OWNER ✓" : "> authority: DENIED");
  setTimeout(() => {
    $("#boot").classList.add("gone");
    if (ok) startApp(); else showGate();
  }, 620);
}

function extractToken(text) {
  text = (text || "").trim();
  const m = /[#?&/]s=([A-Za-z0-9._-]+)/.exec(text);
  if (m) return m[1];
  if (/^\d+\.\d+\.[A-Za-z0-9_-]+\.[a-f0-9]{16,}$/.test(text)) return text;
  return "";
}
async function linkFromText(text) {
  const err = $("#gateErr");
  const tok = extractToken(text);
  if (!tok) { err.hidden = false; err.style.color = ""; err.textContent = "Не нашёл ключ в ссылке — скопируй её целиком."; return; }
  sessionTok = tok;
  try { localStorage.setItem(SKEY, tok); } catch (_) {}
  err.hidden = false; err.style.color = "var(--mut)"; err.textContent = "Проверяю ключ…";
  try { await api("/api/overview"); } catch (e) { err.style.color = ""; err.textContent = "Ключ отклонён или истёк — запроси свежую ссылку."; return; }
  $("#gate").hidden = true; startApp();
}
function showGate() {
  if (authed) return;  // не роняем рабочий авторизованный апп на транзиентном сбое
  $("#boot").hidden = true; $("#app").hidden = true;
  $("#gate").hidden = false;
  const inp = $("#gateLink");
  $("#gateLinkBtn").onclick = () => linkFromText(inp.value);
  inp.onkeydown = (e) => { if (e.key === "Enter") linkFromText(inp.value); };
  $("#gatePaste").onclick = async () => {
    try { const txt = await navigator.clipboard.readText(); inp.value = txt; linkFromText(txt); }
    catch (_) { const err = $("#gateErr"); err.hidden = false; err.style.color = ""; err.textContent = "Буфер недоступен — вставь ссылку вручную."; }
  };
}

/* ------------------------------------------------------------------ reactive backdrop (WebGL + canvas fallback) */
let bgLoad = 0.2, bgTarget = 0.2, bgStarted = false;
function initBackdrop() {
  if (bgStarted) return; bgStarted = true;
  const cv = $("#bg");
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const resize = () => { cv.width = innerWidth * dpr; cv.height = innerHeight * dpr; };
  resize(); addEventListener("resize", resize);
  const gl = cv.getContext("webgl", { antialias: false, alpha: true, powerPreference: "high-performance" });
  if (!gl) return canvasBackdrop(cv, resize);
  const vs = `attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}`;
  const fs = `precision highp float;uniform vec2 R;uniform float T;uniform float L;
  float hash(vec2 p){return fract(sin(dot(p,vec2(41.3,289.1)))*43758.5453);}
  float noise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.-2.*f);
    float a=hash(i),b=hash(i+vec2(1,0)),c=hash(i+vec2(0,1)),d=hash(i+vec2(1,1));
    return mix(mix(a,b,f.x),mix(c,d,f.x),f.y);}
  void main(){
    vec2 uv=(gl_FragCoord.xy-0.5*R)/R.y;
    float t=T*(0.06+L*0.5);
    // receding perspective grid
    vec2 g=uv; g.y+=0.55; float persp=1.0/max(0.06,abs(g.y)+0.02);
    vec2 gp=vec2(uv.x*persp, (g.y>0.?1.:-1.)*(persp - t*3.0));
    float gx=abs(fract(gp.x*0.5)-0.5), gy=abs(fract(gp.y*0.25)-0.5);
    float grid=smoothstep(0.5,0.46,max(1.-gx*8.,1.-gy*8.));
    grid*=smoothstep(1.4,0.1,abs(g.y))*0.5;
    // flow field
    float n=noise(uv*2.5+vec2(t*1.2,t*0.6))*0.5+noise(uv*5.0-vec2(t,0.))*0.25;
    float flow=smoothstep(0.55,0.9,n)*(0.10+L*0.35);
    // scan bar
    float scan=smoothstep(0.02,0.,abs(fract(uv.y*0.5-T*0.05)-0.5)-0.48)*(0.05+L*0.15);
    vec3 cool=vec3(0.12,0.42,0.7), hot=vec3(1.0,0.62,0.18);
    vec3 col=mix(cool,hot,clamp(L*1.3,0.,1.));
    vec3 o=col*(grid*0.9+flow+scan);
    o+=vec3(0.02,0.03,0.05);
    // vignette
    o*=smoothstep(1.5,0.2,length(uv*vec2(0.8,1.1)));
    gl_FragColor=vec4(o,1.0);
  }`;
  const sh = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const prog = gl.createProgram();
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(prog); gl.useProgram(prog);
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p"); gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const uR = gl.getUniformLocation(prog, "R"), uT = gl.getUniformLocation(prog, "T"), uL = gl.getUniformLocation(prog, "L");
  const t0 = performance.now();
  (function loop() {
    bgLoad += (bgTarget - bgLoad) * 0.05;
    const W = Math.floor(innerWidth * dpr), H = Math.floor(innerHeight * dpr);
    if (W && H && (cv.width !== W || cv.height !== H)) { cv.width = W; cv.height = H; }
    gl.viewport(0, 0, cv.width, cv.height);
    gl.uniform2f(uR, cv.width, cv.height);
    gl.uniform1f(uT, (performance.now() - t0) / 1000);
    gl.uniform1f(uL, clamp(bgLoad, 0, 1));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    requestAnimationFrame(loop);
  })();
}
function canvasBackdrop(cv, resize) {
  const ctx = cv.getContext("2d");
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  (function loop(t) {
    bgLoad += (bgTarget - bgLoad) * 0.05;
    const W = Math.floor(innerWidth * dpr), H = Math.floor(innerHeight * dpr);
    if (W && H && (cv.width !== W || cv.height !== H)) { cv.width = W; cv.height = H; }
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.strokeStyle = `rgba(${80 + bgLoad * 175},${140 - bgLoad * 60},${200 - bgLoad * 120},0.06)`;
    ctx.lineWidth = 1;
    const step = 46, off = (t * (0.02 + bgLoad * 0.12)) % step;
    for (let y = -off; y < cv.height; y += step) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cv.width, y); ctx.stroke(); }
    for (let x = 0; x < cv.width; x += step) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, cv.height); ctx.stroke(); }
    requestAnimationFrame(loop);
  })(0);
}

/* ------------------------------------------------------------------ sparkline */
function spark(canvas, data, color, opts = {}) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = canvas.clientWidth || 200, h = canvas.clientHeight || 46;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
  const d = (data || []).slice(-60); if (d.length < 2) return;
  const max = opts.max != null ? opts.max : Math.max(1, ...d) * 1.15, min = 0;
  const X = i => i / (d.length - 1) * w, Y = v => h - (clamp(v, min, max) - min) / (max - min) * (h - 3) - 1;
  ctx.beginPath(); ctx.moveTo(0, h);
  d.forEach((v, i) => ctx.lineTo(X(i), Y(v))); ctx.lineTo(w, h); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + "44"); grad.addColorStop(1, color + "00");
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); d.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
  ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.stroke();
  const lv = d[d.length - 1];
  ctx.beginPath(); ctx.arc(X(d.length - 1), Y(lv), 2, 0, 7); ctx.fillStyle = color; ctx.fill();
}

/* ------------------------------------------------------------------ formatting */
const kb = v => { v = num(v); const u = ["B", "K", "M", "G", "T"]; let i = 0; while (v >= 1024 && i < 4) { v /= 1024; i++; } return v.toFixed(v < 10 && i ? 1 : 0) + u[i]; };
const rate = v => kb(v) + "/s";
const upt = s => { s = num(s); const d = s / 86400 | 0, h = (s % 86400) / 3600 | 0, m = (s % 3600) / 60 | 0; return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; };
const sev = (v, w, c) => v >= c ? "crit" : v >= w ? "warn" : "";
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));

/* ------------------------------------------------------------------ app */
let state = { live: null, overview: null };
let authed = false;
const secEls = {}; $$("#stage > section").forEach(s => secEls[s.dataset.sec] = s);
let current = "vitals";
const loaded = {};

function startApp() {
  authed = true;
  $("#gate").hidden = true; $("#app").hidden = false;
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  initBackdrop();
  $$("#rail a").forEach(a => a.onclick = () => nav(a.dataset.sec));
  clock();
  pollLive();
  loadOverview();
  render("vitals");
  ensureSession();                                   // продлить свой ключ на каждый вход
  setInterval(pollLive, 2000);
  setInterval(loadOverview, 8000);
  setInterval(ensureSession, 6 * 3600 * 1000);       // и раз в 6ч, чтобы не протух
}
function nav(sec) {
  if (sec === current) return;
  current = sec;
  $$("#rail a").forEach(a => a.classList.toggle("on", a.dataset.sec === sec));
  $$("#stage > section").forEach(s => s.classList.toggle("on", s.dataset.sec === sec));
  if (TG && TG.HapticFeedback) try { TG.HapticFeedback.selectionChanged(); } catch (_) {}
  render(sec);
}
function render(sec) {
  const map = { vitals: renderVitals, containers: loadContainers, topology: loadTopology,
    storage: () => loadGeneric("storage", "/api/disks", "STORAGE"),
    ports: () => loadGeneric("ports", "/api/ports", "PORTS"),
    proc: loadProc, stack: () => loadGeneric("stack", "/api/stack", "STACK / VIRTUALIZATION"),
    access: () => loadGeneric("access", "/api/access", "ACCESS / SECURITY") };
  (map[sec] || (() => {}))();
}

/* clock + topbar */
function clock() {
  const t = $("#tbClock");
  setInterval(() => { const d = new Date(); t.textContent = d.toTimeString().slice(0, 8); }, 1000);
  $("#tbSess").textContent = TG ? "◇ TG" : "◆ PWA";
  $("#tbSess").className = "tb-sess" + (TG ? " tg" : "");
}

/* live poll */
async function pollLive() {
  let j; try { j = await api("/api/live"); } catch (e) { if (e.forbidden) return showGate(); return; }
  state.live = j;
  const cpu = num(j.cpu && j.cpu.total), mem = num(j.mem && j.mem.used_pct), ld = num(j.load && j.load.m1);
  const cores = (j.cpu && j.cpu.cores || []).length || 1;
  // всегда живой (пол 0.18), резко разгорается под нагрузкой CPU/load
  bgTarget = clamp(0.18 + cpu / 100 * 0.85 + (ld / cores) * 0.4, 0.18, 1);
  setMini("miniCpu", cpu.toFixed(0) + "%", sev(cpu, 70, 90));
  setMini("miniMem", mem.toFixed(0) + "%", sev(mem, 75, 92));
  setMini("miniLoad", ld.toFixed(2), sev(ld / cores, 0.9, 1.5));
  if (current === "vitals") updateVitals();
  updateTicker();
}
function setMini(id, v, cls) { const e = $("#" + id); e.className = "mini" + (cls ? " " + cls : ""); e.querySelector("em").textContent = v; }

/* overview */
async function loadOverview() { try { state.overview = await api("/api/overview"); if (current === "vitals") updateVitals(); } catch (e) {} }

/* ---------------- VITALS ---------------- */
function renderVitals() {
  const s = secEls.vitals;
  s.innerHTML = `
    <div class="sec-title">01 · vitals</div>
    <div class="hero">
      <div class="vital" id="vCpu"><div class="vl">processor</div><div class="vv" id="vCpuV">—</div><div class="vsub" id="vCpuS"></div><canvas class="spark" id="skCpu"></canvas></div>
      <div class="vital" id="vMem"><div class="vl">memory</div><div class="vv" id="vMemV">—</div><div class="vsub" id="vMemS"></div><canvas class="spark" id="skMem"></canvas></div>
      <div class="vital" id="vLoad"><div class="vl">load · 1m</div><div class="vv" id="vLoadV">—</div><div class="vsub" id="vLoadS"></div><canvas class="spark" id="skLoad"></canvas></div>
    </div>
    <div class="grid g2" style="margin-top:12px">
      <div class="panel k"><div class="p-head"><div class="p-title">throughput</div><div class="p-tag">host net-ns</div></div>
        <div class="grid g2">
          <div><div class="vl" style="font-family:var(--mono);font-size:10px;color:var(--dim)">NET RX / TX</div><div style="font-family:var(--mono);font-size:18px;color:var(--cyan)" id="tNet">—</div><canvas class="spark" id="skNet" style="position:relative;height:38px;margin-top:6px"></canvas></div>
          <div><div class="vl" style="font-family:var(--mono);font-size:10px;color:var(--dim)">DISK R / W</div><div style="font-family:var(--mono);font-size:18px;color:var(--violet)" id="tDisk">—</div><canvas class="spark" id="skDisk" style="position:relative;height:38px;margin-top:6px"></canvas></div>
        </div>
      </div>
      <div class="panel k"><div class="p-head"><div class="p-title">host</div><div class="p-tag" id="vVirt">—</div></div>
        <dl class="kv" id="vHost"></dl>
      </div>
    </div>
    <div class="grid g4" style="margin-top:12px">
      <div class="panel k"><div class="p-title">containers</div><div class="vv" style="font-size:28px" id="cntCont">—</div><div class="vsub" id="cntContS"></div></div>
      <div class="panel k"><div class="p-title">processes</div><div class="vv" style="font-size:28px" id="cntProc">—</div><div class="vsub" id="cntProcS"></div></div>
      <div class="panel k"><div class="p-title">ports open</div><div class="vv" style="font-size:28px" id="cntPort">—</div><div class="vsub">listening</div></div>
      <div class="panel k"><div class="p-title">sessions</div><div class="vv" style="font-size:28px" id="cntSess">—</div><div class="vsub">users online</div></div>
    </div>
    <div class="panel k" style="margin-top:12px"><div class="p-head"><div class="p-title">praxis · heartbeat</div><div class="p-tag">agent</div></div><dl class="kv" id="vPraxis"></dl></div>`;
  updateVitals();
}
function updateVitals() {
  const j = state.live, o = state.overview; if (!secEls.vitals.querySelector("#vCpuV")) return;
  if (j) {
    const cpu = num(j.cpu && j.cpu.total), mem = num(j.mem && j.mem.used_pct), cores = (j.cpu && j.cpu.cores || []).length || 1, ld = num(j.load && j.load.m1);
    setV("vCpu", "vCpuV", cpu.toFixed(0), "%", `${cores} cores · ${(j.procs && j.procs.running) || 0} running`, sev(cpu, 70, 90));
    const mg = j.mem || {};
    setV("vMem", "vMemV", mem.toFixed(0), "%", `${kb(mg.used || 0)} / ${kb(mg.total || 0)}`, sev(mem, 75, 92));
    setV("vLoad", "vLoadV", ld.toFixed(2), "", `5m ${num(j.load && j.load.m5).toFixed(2)} · 15m ${num(j.load && j.load.m15).toFixed(2)}`, sev(ld / cores, 0.9, 1.5));
    const H = j.hist || {};
    spark($("#skCpu"), H.cpu, "#ffb454", { max: 100 });
    spark($("#skMem"), H.mem, "#37b6f2", { max: 100 });
    spark($("#skLoad"), H.load, "#39d98a", { max: Math.max(cores, ...(H.load || [1])) });
    spark($("#skNet"), (H.net_rx || []).map((v, i) => v + num((H.net_tx || [])[i])), "#37b6f2");
    spark($("#skDisk"), (H.disk_r || []).map((v, i) => v + num((H.disk_w || [])[i])), "#b98bff");
    $("#tNet").textContent = `${rate(j.net && j.net.rx)} ↓ ${rate(j.net && j.net.tx)} ↑`;
    $("#tDisk").textContent = `${rate(j.disk && j.disk.read)} · ${rate(j.disk && j.disk.write)}`;
    $("#cntCont").textContent = (j.docker && j.docker.running) || 0;
    $("#cntCont").parentNode.querySelector("#cntContS").textContent = `${(j.docker && j.docker.total) || 0} total`;
    $("#cntProc").textContent = (j.procs && j.procs.total) || 0;
    $("#cntProcS").textContent = `${(j.procs && j.procs.running) || 0} running`;
  }
  if (o) {
    $("#tbHost").textContent = (o.host && o.host.hostname || "host") + " · " + (o.host && o.host.os || "");
    $("#vVirt").textContent = (o.virt && (o.virt.type || o.virt.hypervisor)) || "bare";
    const h = o.host || {};
    $("#vHost").innerHTML = kvRows([
      ["hostname", h.hostname, "hi"], ["os", h.os], ["kernel", h.kernel],
      ["uptime", upt(h.uptime), "am"], ["users", o.users], ["sessions", o.sessions, "ok"]
    ]);
    $("#cntPort").textContent = num(o.ports);
    $("#cntSess").textContent = num(o.sessions);
    const p = o.praxis || {};
    $("#vPraxis").innerHTML = kvRows([["head", p.head, "am"], ["subject", p.subject, "hi"], ["ts", p.ts]]);
  }
}
function setV(box, val, v, unit, sub, cls) {
  const b = $("#" + box); b.className = "vital" + (cls ? " " + cls : "");
  b.querySelector("#" + val).innerHTML = esc(v) + (unit ? `<small>${unit}</small>` : "");
  const ss = b.querySelector(".vsub"); if (ss) ss.textContent = sub || "";
}
function kvRows(rows) { return rows.filter(r => r[1] != null && r[1] !== "").map(([k, v, c]) => `<dt>${esc(k)}</dt><dd class="${c || ""}">${esc(v)}</dd>`).join(""); }

/* ---------------- CONTAINERS ---------------- */
async function loadContainers() {
  const s = secEls.containers;
  s.innerHTML = `<div class="sec-title">02 · containers</div><div class="cgrid" id="ccg"><div class="empty">scanning…</div></div>`;
  let j; try { j = await api("/api/containers"); } catch (e) { if (e.forbidden) return showGate(); s.querySelector("#ccg").innerHTML = `<div class="empty">docker: ${esc(e.message || "недоступен")}</div>`; return; }
  const g = s.querySelector("#ccg"); g.innerHTML = "";
  if (j.stale) g.insertAdjacentHTML("beforebegin", `<div class="stale">STALE</div>`);
  (j.items || []).forEach(c => {
    const st = c.stats || {}, up = c.state === "running";
    const cpu = num(st.cpu_pct), mem = num(st.mem_pct);
    const card = el("div", `cc iso-${c.level || "normal"} ${up ? "up" : "stopped"}`);
    const ports = (c.ports || []).filter(p => p.public).slice(0, 4)
      .map(p => `<span class="chip ${p.ip && p.ip.startsWith("0.") ? "pub" : "port"}">${p.public}:${p.private}</span>`).join("");
    card.innerHTML = `
      <div class="cn">${esc(c.name)}<i class="dot"></i></div>
      <div class="ci">${esc(c.image)}</div>
      <div class="cbars">
        <span>CPU</span><div class="bar"><i style="width:${clamp(cpu, 0, 100)}%;background:var(--amber)"></i></div><b>${up ? cpu.toFixed(1) + "%" : "—"}</b>
        <span>MEM</span><div class="bar"><i style="width:${clamp(mem, 0, 100)}%"></i></div><b>${up ? mem.toFixed(0) + "%" : "—"}</b>
      </div>
      <div class="cmt"><span class="chip">${esc(c.level || "normal")}</span>${ports}</div>`;
    card.onclick = () => openContainer(c.id, c.name);
    g.appendChild(card);
  });
  if (!(j.items || []).length) g.innerHTML = `<div class="empty">нет контейнеров</div>`;
}
async function openContainer(id, name) {
  let m = $("#modal"); if (!m) { m = el("div"); m.id = "modal"; document.body.appendChild(m); }
  m.className = "on"; m.innerHTML = `<div class="modal-card"><span class="mx">✕</span><div class="p-title">${esc(name)}</div><div class="empty">inspect…</div></div>`;
  m.querySelector(".mx").onclick = () => m.classList.remove("on");
  m.onclick = e => { if (e.target === m) m.classList.remove("on"); };
  try {
    const [d, logs] = await Promise.all([api("/api/container/" + id), api("/api/container/" + id + "/logs").catch(() => ({}))]);
    const rows = Object.entries(d).filter(([k, v]) => typeof v !== "object").map(([k, v]) => `<dt>${esc(k)}</dt><dd class="hi">${esc(v)}</dd>`).join("");
    const mounts = (d.mounts || []).map(mt => `<span class="chip ${mt.rw ? "pub" : ""}">${esc(mt.destination || mt.dst || "")}${mt.rw ? " rw" : " ro"}</span>`).join(" ");
    m.querySelector(".modal-card").innerHTML = `<span class="mx">✕</span><div class="p-title" style="margin-bottom:12px">${esc(name)}</div>
      <dl class="kv">${rows}</dl>${mounts ? `<div class="cmt" style="margin-top:12px">${mounts}</div>` : ""}
      ${logs.text ? `<div class="p-title" style="margin-top:14px">logs</div><pre>${esc(logs.text)}</pre>` : ""}`;
    m.querySelector(".mx").onclick = () => m.classList.remove("on");
  } catch (e) { m.querySelector(".modal-card").innerHTML = `<span class="mx" onclick="this.closest('#modal').classList.remove('on')">✕</span><div class="empty">${esc(e.message || "нет данных")}</div>`; }
}

/* ---------------- TOPOLOGY (3D) ---------------- */
let g3d = null;
async function loadTopology() {
  const s = secEls.topology;
  if (!loaded.topo) {
    s.innerHTML = `<div class="sec-title">03 · topology</div>
      <div class="topo-wrap"><div id="g3d"></div>
      <div class="topo-legend">
        <span><i style="background:#ffb454"></i>host</span>
        <span><i style="background:#37b6f2"></i>container</span>
        <span><i style="background:#39d98a"></i>network</span>
        <span><i style="background:#b98bff"></i>port</span>
      </div></div>`;
    loaded.topo = true;
  }
  let data; try { data = await api("/api/netmap"); } catch (e) { if (e.forbidden) return showGate(); return; }
  const nodes = (data.nodes || []).map(n => ({ ...n, col: n.id === "host" ? "#ffb454" : n.id.startsWith("c:") ? "#37b6f2" : n.id.startsWith("n:") ? "#39d98a" : "#b98bff" }));
  const links = (data.links || []).map(l => ({ source: l.a, target: l.b }));
  await ensureForceGraph();
  const box = $("#g3d");
  if (!window.ForceGraph3D) { box.innerHTML = `<div class="empty">3D-движок недоступен</div>`; return; }
  if (!g3d) {
    g3d = ForceGraph3D()(box)
      .backgroundColor("#05070b")
      .showNavInfo(false)
      .nodeColor(n => n.col)
      .nodeVal(n => n.id === "host" ? 8 : n.id.startsWith("c:") ? 4 : 2)
      .nodeLabel(n => n.label || n.id)
      .linkColor(() => "rgba(120,150,190,0.25)")
      .linkDirectionalParticles(2).linkDirectionalParticleSpeed(0.006).linkDirectionalParticleWidth(1.2)
      .width(box.clientWidth).height(box.clientHeight);
  }
  g3d.graphData({ nodes, links });
  setTimeout(() => { try { g3d.zoomToFit(600, 40); } catch (_) {} }, 400);
  addEventListener("resize", () => { if (g3d && current === "topology") g3d.width($("#g3d").clientWidth).height($("#g3d").clientHeight); });
}
function ensureForceGraph() {
  return new Promise(res => {
    if (window.ForceGraph3D) return res();
    const s = el("script"); s.src = "/vendor/3d-force-graph.min.js";
    s.onload = res; s.onerror = res; document.head.appendChild(s);
  });
}

/* ---------------- generic sections (storage/ports/stack/access) ---------------- */
async function loadGeneric(sec, path, title) {
  const s = secEls[sec];
  s.innerHTML = `<div class="sec-title">${title}</div><div id="gc"><div class="empty">loading…</div></div>`;
  let j; try { j = await api(path); } catch (e) { if (e.forbidden) return showGate(); s.querySelector("#gc").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  s.querySelector("#gc").innerHTML = renderAny(j);
}
function renderAny(j) {
  if (Array.isArray(j)) return renderTable(j);
  if (j && typeof j === "object") {
    const parts = [];
    for (const [k, v] of Object.entries(j)) {
      if (k === "stale") continue;
      if (Array.isArray(v) && v.length && typeof v[0] === "object") parts.push(`<div class="panel k" style="margin-bottom:12px"><div class="p-title">${esc(k)}</div>${renderTable(v)}</div>`);
      else if (Array.isArray(v)) parts.push(`<div class="panel k" style="margin-bottom:12px"><div class="p-title">${esc(k)}</div><div class="cmt">${v.map(x => `<span class="chip">${esc(typeof x === "object" ? JSON.stringify(x) : x)}</span>`).join("")}</div></div>`);
      else if (v && typeof v === "object") parts.push(`<div class="panel k" style="margin-bottom:12px"><div class="p-title">${esc(k)}</div><dl class="kv">${Object.entries(v).map(([a, b]) => `<dt>${esc(a)}</dt><dd class="hi">${esc(typeof b === "object" ? JSON.stringify(b) : b)}</dd>`).join("")}</dl></div>`);
      else parts.push(`<div class="panel k" style="margin-bottom:12px;display:flex;justify-content:space-between"><span class="p-title">${esc(k)}</span><b style="font-family:var(--mono);color:var(--hi)">${esc(v)}</b></div>`);
    }
    return parts.join("");
  }
  return `<div class="empty">${esc(String(j))}</div>`;
}
const BYTEISH = /(bytes|_b$|size|total|used|free|avail|rss|mem|read|write|rx|tx)/i;
function cell(k, v) {
  if (v == null) return "";
  if (typeof v === "object") return JSON.stringify(v);
  if (typeof v === "number" && BYTEISH.test(k) && Math.abs(v) >= 100000) return kb(v);
  return v;
}
function renderTable(arr) {
  if (!arr.length) return `<div class="empty">пусто</div>`;
  const keys = [...new Set(arr.flatMap(o => Object.keys(o || {})))].filter(k => typeof (arr[0] || {})[k] !== "object").slice(0, 6);
  const cols = keys.length || 1;
  const head = `<div class="row" style="grid-template-columns:repeat(${cols},1fr);color:var(--dim)">${keys.map(k => `<span>${esc(k)}</span>`).join("")}</div>`;
  const body = arr.slice(0, 120).map(o => `<div class="row" style="grid-template-columns:repeat(${cols},1fr)">${keys.map(k => `<span class="${typeof o[k] === "number" ? "rd" : "rn"}">${esc(cell(k, o[k]))}</span>`).join("")}</div>`).join("");
  return `<div class="rows">${head}${body}</div>`;
}

/* ---------------- PROCESSES ---------------- */
async function loadProc() {
  const s = secEls.proc;
  s.innerHTML = `<div class="sec-title">06 · process table</div><div class="grid g2" id="pc"><div class="empty">loading…</div></div>`;
  let j; try { j = await api("/api/processes"); } catch (e) { if (e.forbidden) return showGate(); s.querySelector("#pc").innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const topList = (arr, label, unit, color) => `<div class="panel k"><div class="p-head"><div class="p-title">${label}</div><div class="p-tag">${(j.total) || 0} total</div></div><div class="rows">${(arr || []).slice(0, 12).map(p => {
    const v = num(p.cpu != null ? p.cpu : p.mem); const w = clamp(unit === "%" ? v : v / (num((arr[0] || {})[p.cpu != null ? "cpu" : "mem"]) || 1) * 100, 0, 100);
    return `<div class="row" style="grid-template-columns:1fr auto"><span class="rn">${esc(p.name || p.comm || p.pid)}<div class="bar" style="margin-top:5px"><i style="width:${w}%;background:${color}"></i></div></span><b class="rd" style="color:${color}">${v.toFixed(1)}${unit}</b></div>`;
  }).join("")}</div></div>`;
  s.querySelector("#pc").innerHTML = topList(j.top_cpu, "top · cpu", "%", "#ffb454") + topList(j.top_mem, "top · memory", "%", "#37b6f2");
}

/* ---------------- ticker ---------------- */
function updateTicker() {
  const j = state.live, o = state.overview; if (!j) return;
  const bits = [
    `HOST <u>${o && o.host ? o.host.hostname : "—"}</u>`,
    `CPU <u>${num(j.cpu && j.cpu.total).toFixed(1)}%</u>`,
    `MEM <u>${num(j.mem && j.mem.used_pct).toFixed(1)}%</u>`,
    `LOAD <u>${num(j.load && j.load.m1).toFixed(2)}</u>`,
    `NET <u>${rate(j.net && j.net.rx)}↓ ${rate(j.net && j.net.tx)}↑</u>`,
    `DISK <u>${rate(j.disk && j.disk.read)} ${rate(j.disk && j.disk.write)}</u>`,
    `CONTAINERS <u>${(j.docker && j.docker.running) || 0}/${(j.docker && j.docker.total) || 0}</u>`,
    `PROC <u>${(j.procs && j.procs.total) || 0}</u>`,
    o && o.praxis ? `PRAXIS <u>${esc(o.praxis.head || "")}</u>` : "",
    `UPTIME <u>${o && o.host ? upt(o.host.uptime) : "—"}</u>`
  ].filter(Boolean);
  const line = bits.map(b => `<b>${b.replace(/<u>/g, "").replace(/<\/u>/g, "").split(" ")[0]}</b> ${b.replace(/^\S+\s/, "")}`).join(" &nbsp;·&nbsp; ");
  const track = $("#tkTrack"); track.innerHTML = line + " &nbsp;·&nbsp; " + line;
}

document.addEventListener("DOMContentLoaded", boot);
})();
