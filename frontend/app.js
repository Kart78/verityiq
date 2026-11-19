/* BI Testing Assistant - single-page frontend (no build step, no secrets, no browser storage of credentials). */
"use strict";
const SNAP = window.__SNAPSHOT__ || null;          // static read-only snapshot mode
const S = { env: null, envs: [], platforms: [], session: null, route: "overview" };

// ------------------------------------------------------------------ utils
const $ = (s, el = document) => el.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = v => v === null || v === undefined ? "—" : (typeof v === "number" ? (Number.isInteger(v) ? v.toLocaleString() : (+v.toFixed(4)).toLocaleString()) : esc(v));
const when = s => s ? new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z").toLocaleString([], {dateStyle: "medium", timeStyle: "short"}) : "—";
function toast(msg) { const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 3200); }

async function api(path, opts = {}) {
  if (SNAP) {
    if (opts.method && opts.method !== "GET") { toast("Read-only snapshot. Run the app locally to execute actions."); throw new Error("snapshot"); }
    if (path in SNAP) return SNAP[path];
    throw new Error("Not in snapshot: " + path);
  }
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) { const t = await r.text(); throw new Error(`${r.status}: ${t.slice(0, 300)}`); }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}
const E = p => `/api/environments/${S.env}${p}`;

// ------------------------------------------------------------------ icons
const P = {
  home: '<path d="M3 11 12 4l9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  plug: '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0zM12 17v5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  list: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  play: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m10 8 6 4-6 4z"/>',
  db: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  fx: '<path d="M9 20c2 0 2-2 3-8s1-8 3-8M7 10h7M14 14l5 5M19 14l-5 5"/>',
  bug: '<rect x="7" y="7" width="10" height="13" rx="5"/><path d="M12 7V4M4 13h3M17 13h3M5 7l3 2M19 7l-3 2M5 19l3-2M19 19l-3-2"/>',
  doc: '<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6M9 13h7M9 17h7"/>',
  code: '<path d="m8 8-5 4 5 4M16 8l5 4-5 4M14 4l-4 16"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  shield: '<path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/>',
  gauge: '<path d="M4 18a8 8 0 1 1 16 0M12 18l4-6"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7zM2 12l10 5 10-5M2 17l10 5 10-5"/>',
  cursor: '<path d="m4 4 7 17 2-7 7-2z"/>',
  check: '<path d="m5 12 5 5 9-10"/>', x: '<path d="M6 6l12 12M18 6 6 18"/>', alert: '<path d="M12 7v6M12 17h.01"/>',
  minus: '<path d="M6 12h12"/>', spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>', scale: '<path d="M12 3v18M5 7h14M5 7l-3 7a3 3 0 0 0 6 0zM19 7l-3 7a3 3 0 0 0 6 0zM8 21h8"/>',
  trend: '<path d="M3 17l6-6 4 4 8-8M15 7h6v6"/>', calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5M4 21h16"/>', chevron: '<path d="m9 6 6 6-6 6"/>', plus: '<path d="M12 5v14M5 12h14"/>',
  upload: '<path d="M12 21V9m-5 5 5-5 5 5M5 3h14"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
};
const ico = (n, sw = 2) => `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round">${P[n] || ""}</svg>`;

const STATUS_META = {
  "PASS": {icon: "check", label: "Passed", color: "var(--pass)"},
  "FAIL": {icon: "x", label: "Failed", color: "var(--fail)"},
  "BLOCKED": {icon: "minus", label: "Blocked", color: "var(--blocked)"},
  "NOT VALIDATED": {icon: "alert", label: "Not validated", color: "var(--nv)"},
  "ERROR": {icon: "alert", label: "Error", color: "var(--error)"},
  "NOT EXECUTED": {icon: "clock", label: "Not executed", color: "var(--ne)"},
};
const pill = s => { if (!s) return '<span class="pill s-NOT-EXECUTED">Not run</span>'; const m = STATUS_META[s] || {icon: "alert", label: s}; return `<span class="pill s-${s.replace(/ /g, "-")}">${ico(m.icon, 3)}${m.label}</span>`; };
const catTag = c => `<span class="cat c-${esc(c).replace(/ /g, "-")}">${esc(c)}</span>`;
const sev = s => s ? `<span class="sev sev-${s}">${s}</span>` : "";

// ------------------------------------------------------------------ charts
function tickLabel(at, pts) {
  const d = new Date(at + (at.includes("+") ? "" : "Z"));
  const sameDay = pts.every(p => p.at.slice(0, 10) === pts[0].at.slice(0, 10));
  return sameDay ? d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}) : d.toLocaleDateString([], {month: "short", day: "numeric"});
}
function trendChart(trend) {
  const pts = trend.filter(t => t.rate !== null && t.rate !== undefined).slice(-12);
  if (!pts.length) return `<div class="empty">No completed runs yet.</div>`;
  const W = 520, H = 210, L = 44, R = 34, T = 26, B = 30;
  const x = i => L + (pts.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (pts.length - 1));
  const y = v => T + (100 - v) / 100 * (H - T - B);
  const grid = [0, 25, 50, 75, 100].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#e8edf5"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="#64748b">${v}%</text>`).join("");
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.rate)}`).join("");
  const area = path + `L${x(pts.length - 1)},${y(0)}L${x(0)},${y(0)}Z`;
  const labels = pts.map((p, i) => (pts.length <= 7 || i % Math.ceil(pts.length / 7) === 0 || i === pts.length - 1) ? `<text x="${x(i)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#64748b">${tickLabel(p.at, pts)}</text>` : "").join("");
  const last = pts[pts.length - 1];
  const dots = pts.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.rate)}" r="4.5" fill="#1f5fe0" stroke="#fff" stroke-width="2"/><rect class="hit" data-i="${i}" x="${x(i) - 14}" y="${T}" width="28" height="${H - T - B}" fill="transparent"/>`).join("");
  setTimeout(() => {
    const wrap = $("#trendWrap"); if (!wrap) return; const tip = $(".tooltip", wrap);
    wrap.querySelectorAll(".hit").forEach(h => {
      h.onmouseenter = () => { const p = pts[+h.dataset.i]; const r = wrap.getBoundingClientRect(), s = $("svg", wrap).getBoundingClientRect();
        tip.innerHTML = `<b>${p.rate}%</b> validated pass rate<br>Coverage ${p.coverage}% · ${p.mode}<br>${when(p.at)}`;
        tip.style.left = (x(+h.dataset.i) / W * s.width) + "px"; tip.style.top = (y(p.rate) / H * s.height) + "px"; tip.style.opacity = 1; };
      h.onmouseleave = () => tip.style.opacity = 0;
    });
  });
  return `<div class="chart-wrap" id="trendWrap"><svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Validated pass rate by run">
    <defs><linearGradient id="ga" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#1f5fe0" stop-opacity=".16"/><stop offset="1" stop-color="#1f5fe0" stop-opacity="0"/></linearGradient></defs>
    ${grid}<path d="${area}" fill="url(#ga)"/><path d="${path}" fill="none" stroke="#1f5fe0" stroke-width="2"/>${labels}${dots}
    <g transform="translate(${x(pts.length - 1)},${y(last.rate) - 16})"><rect x="-22" y="-14" width="44" height="20" rx="5" fill="#0b1f4d"/><text y="1" text-anchor="middle" font-size="11.5" font-weight="700" fill="#fff">${last.rate}%</text></g>
  </svg><div class="tooltip"></div></div>`;
}

function donut(counts, centerTop, centerSub) {
  const order = ["PASS", "FAIL", "ERROR", "BLOCKED", "NOT VALIDATED", "NOT EXECUTED"];
  const colors = {"PASS": "#14995a", "FAIL": "#d92d20", "ERROR": "#8f1d14", "BLOCKED": "#d98a1b", "NOT VALIDATED": "#8492a6", "NOT EXECUTED": "#c8d0dc"};
  const total = order.reduce((a, k) => a + (counts[k] || 0), 0) || 1;
  const R = 52, C = 2 * Math.PI * R; let off = 0;
  const segs = order.filter(k => counts[k]).map(k => { const len = counts[k] / total * C; const gap = Math.min(2, len / 2);
    const s = `<circle r="${R}" cx="70" cy="70" fill="none" stroke="${colors[k]}" stroke-width="18" stroke-dasharray="${len - gap} ${C - len + gap}" stroke-dashoffset="${-off}"><title>${STATUS_META[k].label}: ${counts[k]}</title></circle>`; off += len; return s; }).join("");
  return `<svg viewBox="0 0 140 140" width="150" height="150" style="transform:rotate(-90deg)">${segs}</svg>
    <div style="position:absolute;inset:0;display:grid;place-items:center;text-align:center;pointer-events:none"><div><div style="font-size:24px;font-weight:700">${centerTop}</div><div style="color:#64748b;font-size:12px">${centerSub}</div></div></div>`;
}

function sparkline(vals, color) {
  if (!vals || vals.length < 2) return "";
  const W = 160, H = 34, mx = Math.max(...vals), mn = Math.min(...vals), rg = (mx - mn) || 1;
  const pts = vals.map((v, i) => `${i * W / (vals.length - 1)},${H - 3 - (v - mn) / rg * (H - 8)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"/></svg>`;
}

// ------------------------------------------------------------------ robot illustration (original)
const ROBOT = `<svg class="robot" viewBox="0 0 150 110"><circle cx="40" cy="20" r="3" fill="#7fb0ff"/><circle cx="128" cy="30" r="2" fill="#7fb0ff"/>
<rect x="22" y="22" width="62" height="48" rx="22" fill="#fff" stroke="#c9d8f7" stroke-width="2"/><rect x="31" y="32" width="44" height="28" rx="13" fill="#0f2a66"/>
<circle cx="45" cy="46" r="5" fill="#5ee0ff"/><circle cx="61" cy="46" r="5" fill="#5ee0ff"/><rect x="14" y="38" width="9" height="18" rx="4" fill="#2f6ff0"/><rect x="83" y="38" width="9" height="18" rx="4" fill="#2f6ff0"/>
<path d="M18 40c0-22 70-22 70 0" fill="none" stroke="#2f6ff0" stroke-width="3"/><rect x="30" y="72" width="46" height="34" rx="14" fill="#fff" stroke="#c9d8f7" stroke-width="2"/><circle cx="53" cy="88" r="5" fill="#9cc4ff"/>
<g transform="rotate(8 112 62)"><rect x="90" y="34" width="44" height="58" rx="7" fill="#1f5fe0"/><rect x="95" y="40" width="34" height="46" rx="4" fill="#fff"/>
<path d="m99 50 3 3 5-6M99 63l3 3 5-6M99 76l3 3 5-6" stroke="#14995a" stroke-width="2.4" fill="none" stroke-linecap="round"/><path d="M111 50h13M111 63h13M111 76h13" stroke="#b8c8ea" stroke-width="2.4" stroke-linecap="round"/></g></svg>`;

function resultHtml(r) {
  const empty = e => e.content == null || (Array.isArray(e.content) && !e.content.length) || (typeof e.content === "object" && !Array.isArray(e.content) && !Object.keys(e.content).length);
  const ev = r.evidence.filter(e => !empty(e)).map(e => {
    let body;
    if (e.kind === "sql") body = `<pre class="code">${esc(e.content.sql)}</pre>${e.content.engine ? `<div style="font-size:12px;color:var(--muted)">Engine: ${esc(e.content.engine)} · ${e.content.elapsed_ms} ms · ${(e.content.rows || []).length} row(s)</div>` : ""}${e.content.rows?.length ? rowsTable(e.content.rows.slice(0, 15)) : ""}`;
    else if (e.kind === "dax") body = e.content.query ? `<pre class="code">${esc(e.content.query)}</pre>${e.content.disclaimer ? `<div class="note">${esc(e.content.disclaimer)}</div><details><summary style="cursor:pointer;font-size:12.5px">Emulation SQL (demo only)</summary><pre class="code">${esc(e.content.emulation_sql)}</pre></details>` : ""}${(e.content.notes || []).map(n => `<div class="note blue">${esc(n)}</div>`).join("")}${e.content.rows?.length ? rowsTable(e.content.rows.slice(0, 15)) : ""}`
      : `<pre class="code">${esc(e.content.expression)}</pre>`;
    else if (e.kind === "rows") body = rowsTable(e.content);
    else if (e.kind === "note" && Array.isArray(e.content)) body = rowsTable(e.content);
    else if (e.kind === "metadata" && e.content.static_findings) body = findingsHtml(e.content);
    else if (e.kind === "metadata" && Array.isArray(e.content.visuals)) {
      const V = {reacts: ["var(--pass)", "reacts"], ignored: ["var(--ne)", "set to ignore"], no_path: ["var(--fail)", "not reachable"], independent: ["var(--muted)", "independent (dimension / disconnected data)"], unknown: ["var(--muted)", "undetermined"]};
      body = `<div style="font-size:12.5px;color:var(--ink-2);margin-bottom:6px">Slicer field <span class="mono">${esc(e.content.slicer_field)}</span> (table ${esc(e.content.slicer_table || "?")})</div>
        <div style="overflow:auto;border:1px solid var(--line);border-radius:10px"><table><thead><tr><th>Visual</th><th>Interaction</th><th>Filter path</th><th>Result</th></tr></thead><tbody>
        ${e.content.visuals.map(x => `<tr><td><b>${esc(x.visual)}</b><div style="font-size:11.5px;color:var(--muted)">${esc(x.type)}</div></td><td>${esc(x.interaction === "NoFilter" ? "None" : x.interaction === "HighlightFilter" ? "Highlight" : "Filter")}</td>
          <td class="mono" style="font-size:12px">${esc(x.relationship_path || (x.verdict === "no_path" ? "no relationship to " + x.data_tables.join(", ") : "-"))}</td>
          <td><span class="dot" style="background:${V[x.verdict][0]}"></span>${V[x.verdict][1]}</td></tr>`).join("")}</tbody></table></div>`;
    }
    else body = typeof e.content === "string" ? `<div class="note">${esc(e.content)}</div>` : `<pre class="code">${esc(JSON.stringify(e.content, null, 2))}</pre>`;
    return `<div style="margin-bottom:12px"><div style="font-weight:600;margin-bottom:4px">${esc(e.label)} ${e.connector ? `<span class="badge b-planned">${esc(e.connector)}</span>` : ""} ${e.mode ? `<span class="badge ${e.mode === "DEMO" ? "b-demo" : "b-implemented"}">${e.mode}</span>` : ""}</div>${body}</div>`;
  }).join("");
  const nums = typeof r.expected === "number" || typeof r.actual === "number";
  const cause = (r.root_cause_hypothesis || "").replace(/^\[[^\]]+\]\s*/, "").replace(/^DAX analyzer finding \w+: /, "");
  return `<section><h4>Last result · ${when(r.executed_at)}${r.mode && r.mode !== "LIVE" ? ` <span class="badge ${r.mode === "DEMO" ? "b-demo" : "b-partial"}">${r.mode}</span>` : ""}</h4>
    <div class="rmsg-big">${pill(r.status)} ${sev(r.severity)} <span>${esc(r.message)}</span></div>
    ${nums ? `<div class="ea"><div><span>Expected (source)</span><b class="mono">${fmt(r.expected)}</b></div><div><span>Actual (dashboard)</span><b class="mono">${fmt(r.actual)}</b></div>${r.difference != null ? `<div><span>Difference</span><b class="mono" style="color:${r.status === "FAIL" ? "var(--fail)" : "inherit"}">${fmt(r.difference)}</b></div>` : ""}</div>` : ""}
    ${r.business_impact || cause || r.remediation_suggestion ? `<dl class="kv" style="margin-top:10px">
      ${r.business_impact ? `<dt>Impact</dt><dd>${esc(r.business_impact)}</dd>` : ""}
      ${cause && !r.message.includes(cause) ? `<dt>Likely cause</dt><dd>${esc(cause)} <span class="muted-i">(suggestion)</span></dd>` : ""}
      ${r.remediation_suggestion ? `<dt>Suggested fix</dt><dd>${esc(r.remediation_suggestion)}</dd>` : ""}</dl>` : ""}</section>
    ${ev ? `<details class="tdef"><summary>Evidence</summary><section>${ev}</section></details>` : ""}`;
}

function rowsTable(rows) {
  if (!rows || !rows.length) return '<div class="empty">No rows</div>';
  const cols = Object.keys(rows[0]);
  return `<div style="overflow:auto;max-height:300px;border:1px solid var(--line);border-radius:10px"><table><thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${cols.map(c => `<td class="mono">${c === "status" ? pill(r[c]) : typeof r[c] === "boolean" ? (r[c] ? "✓" : "✗") : fmt(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function findingsHtml(m) {
  const fl = [...m.static_findings, ...m.definition_findings];
  return `<div style="margin-bottom:8px">Static: <b>${m.static_status}</b> · Definition alignment: ${pill(m.definition_status)} · Overall: ${pill(m.overall_status)}</div>
    ${m.definition_checks.length ? `<div style="margin:8px 0"><b>Business definition checks</b>${m.definition_checks.map(c => `<div class="check-row"><div>${esc(c.check)}${c.detail ? `<div style="color:var(--muted);font-size:12px">${esc(c.detail)}</div>` : ""}</div><div>${c.result === "MATCH" ? pill("PASS") : c.result === "MISMATCH" ? pill("FAIL") : pill("NOT VALIDATED")}</div></div>`).join("")}</div>` : ""}
    ${fl.length ? `<b>Findings</b>${fl.map(f => `<div class="note ${f.severity === "info" ? "blue" : ""}" style="${f.severity === "error" ? "background:#fdecea;border-color:#f6c3bd;color:#8f1d14" : ""}"><b>${esc(f.rule)} · ${f.severity}</b> - ${esc(f.message)}${f.recommendation ? `<br><i>${esc(f.recommendation)}</i>` : ""}</div>`).join("")}` : '<div class="note blue">No findings.</div>'}`;
}

// ------------------------------------------------------------------ uploads / downloads
async function upload(path, form) {
  if (SNAP) { toast("Read-only snapshot. Run the app locally to upload."); throw new Error("snapshot"); }
  const r = await fetch(path, { method: "POST", body: form });
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail; } catch {} throw new Error(t); }
  return r.json();
}
function download(path) {
  if (SNAP) { toast("Downloads are available in the local app."); return; }
  const a = document.createElement("a"); a.href = path; a.click();
}
const copyText = async (txt) => { try { await navigator.clipboard.writeText(txt); toast("Copied"); } catch { toast("Copy blocked - select the text instead"); } };

// ------------------------------------------------------------------ navigation (wireframe)
const NAV = [
  ["overview", "Overview", "home"], ["dax", "DAX Analysis", "fx"], ["usage", "Model Usage", "layers"], ["tests", "Test Cases", "list"],
  ["execution", "Test Execution", "play"], ["validation", "Data Validation", "db"], ["reports", "Reports", "doc"], ["settings", "Settings", "gear"],
];
function renderNav() {
  $("#nav").innerHTML = `<a href="#project" class="newproj ${S.route === "project" ? "active" : ""}">${ico("plus")}${S.env ? "Project / Upload PBIP" : "Upload PBIP"}</a>` +
    NAV.map(([r, l, i]) => `<a href="#${r}" class="${S.route === r ? "active" : ""} ${S.env ? "" : "disabled"}">${ico(i)}${l}</a>`).join("");
}

// ------------------------------------------------------------------ drawer
function openDrawer(title, html, keepScroll) {
  const y = $("#drawer").scrollTop;
  $("#drawer").innerHTML = `<header><h3>${title}</h3><button class="btn btn-ghost btn-sm" id="dClose">Close</button></header>${html}`;
  $("#drawer").classList.add("open"); $("#drawerBg").classList.add("open"); $("#drawer").scrollTop = keepScroll ? y : 0;
  $("#dClose").onclick = closeDrawer;
}
function closeDrawer() { $("#drawer").classList.remove("open"); $("#drawerBg").classList.remove("open"); }
$("#drawerBg").onclick = closeDrawer;

const curEnv = () => S.envs.find(e => e.id === S.env);
const platName = k => (S.platforms.find(p => p.key === k) || {name: k}).name;
const biShort = env => ({powerbi: "Power BI", demo_bi: "Power BI", tableau: "Tableau", looker: "Looker", qlik: "Qlik", cognos: "Cognos"}[env?.bi?.platform] || "BI");
const CAT_LABEL = {"KPI": "Business Use Case", "Data Quality": "Data Validation", "Expression": "DAX & Calculations", "Functional": "Functional", "Visual": "Visuals", "Security": "Security", "Performance": "Performance"};
const catTag2 = c => `<span class="cat c-${esc(c).replace(/ /g, "-")}">${esc(CAT_LABEL[c] || c)}</span>`;

// dax status for the UI: PASS / FAIL, with the basis made explicit
function daxVerdict(m) {
  if (m.overall_status === "FAIL") return {s: "FAIL", note: ""};
  if (m.definition_status === "PASS") return {s: "PASS", note: "syntax + business logic"};
  return {s: "PASS", note: "syntax only"};
}
const verdictPill = m => { const v = daxVerdict(m); return `${pill(v.s)}${v.note === "syntax only" ? `<div class="vnote warn" title="No business definition - only syntax and references were checked">syntax only</div>` : ""}`; };
const logicCell = m => m.definition_status === "PASS" ? `<span class="pill s-PASS">${ico("check", 3)}Verified</span>`
  : m.definition_status === "FAIL" ? `<span class="pill s-FAIL">${ico("x", 3)}Mismatch</span>`
  : `<span class="pill s-NOT-VALIDATED">${ico("alert", 3)}No definition</span>`;

// ------------------------------------------------------------------ actions
async function doRun(btn) {
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Running…`; }
  try { const tests = await api(E("/tests")); if (!tests.length) await api(E("/tests/generate"), {method: "POST"});
    const r = await api(E("/runs"), {method: "POST", body: {}}); const c = r.summary.counts;
    toast(`Run finished: ${c.PASS} passed, ${c.FAIL} failed, ${c.BLOCKED + c["NOT VALIDATED"]} not validated (${r.summary.mode})`); route(); }
  catch (e) { if (e.message !== "snapshot") toast("Run failed: " + e.message); route(); }
}
async function doGenerate(btn) {
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Generating…`; }
  try { const t = await api(E("/tests/generate"), {method: "POST"}); toast(`${t.length} test cases generated with SQL`); location.hash = "#tests"; route(); }
  catch (e) { if (e.message !== "snapshot") toast("Generation failed: " + e.message); route(); }
}

// ------------------------------------------------------------------ pages
const PAGES = {};

// ---------- Project / Upload
PAGES.project = async () => {
  const env = curEnv();
  let summary = null;
  let ovw = null;
  if (env && env.project) { try { summary = await api(`/api/projects/${S.env}`); } catch {} try { ovw = await api(E("/overview")); } catch {} }
  $("#view").innerHTML = `
  <div class="page-h"><div><h3>${summary ? esc(summary.name) : "Start a new test project"}</h3><p>${summary ? "Setup checklist - fix anything that isn't green, then run." : "Upload a Power BI project (PBIP) to begin."}</p></div>
    ${summary ? `<button class="btn btn-ghost" id="upToggle">${ico("plus")} Upload another PBIP</button><button class="btn btn-ghost" id="reAn" title="Re-read the stored PBIP with the current version - keeps settings, table names, column fixes and approved definitions">${ico("refresh", 3)} Re-analyze</button>` : ""}</div>
  <div id="upWrap" style="${summary ? "display:none" : ""}"><div class="grid-2" style="grid-template-columns:1.1fr 1fr">
    <div class="card" style="padding:20px">
      <div class="drop" id="drop">${ico("download", 1.6).replace("<svg", '<svg style="width:40px;height:40px;transform:rotate(180deg);color:var(--blue-600)"')}
        <h4>Drop a PBIP folder or .zip here</h4><p>Must contain <span class="mono">*.SemanticModel/definition/*.tmdl</span>. A <span class="mono">*.Report</span> folder (PBIR) is optional and enables chart-level tests.</p>
        <div style="display:flex;gap:10px;justify-content:center;margin-top:12px"><label class="btn btn-primary">${ico("plus")} Choose PBIP folder<input type="file" id="fFolder" webkitdirectory multiple hidden></label>
        <label class="btn btn-outline">Choose .zip<input type="file" id="fZip" accept=".zip" hidden></label></div>
        <div id="picked" style="margin-top:10px;color:var(--ink-2);font-size:13px"></div></div>
      <div style="display:flex;gap:10px;margin-top:14px;align-items:center"><input class="select" id="pName" placeholder="Project name (optional, e.g. Customer Center UAT)" style="flex:1;max-width:none">
        <button class="btn btn-primary" id="upBtn" disabled>Upload & analyze</button></div>
      <div style="margin-top:14px;font-size:12.5px;color:var(--muted)">Model saved as <span class="mono">model.bim</span>? In Power BI Desktop enable <i>Preview features → Store semantic model using TMDL format</i>, then save as PBIP. Only text files are uploaded; data caches are skipped.</div>
      <div class="or-line"><span>or</span></div>
      <button class="btn btn-ghost" id="sampleBtn" style="width:100%;justify-content:center">${ico("spark")} Load the sample project (demo data, no connections needed)</button>
    </div>
    <div class="card" style="padding:20px"><h4 style="margin:0 0 10px">How it works</h4>
      <ol class="steps">
        <li><b>Upload</b> the PBIP - measures, visuals and source tables are read automatically.</li>
        <li><b>Connect</b> the data the dashboard comes from (Excel, Databricks, Snowflake…).</li>
        <li><b>Run</b> - every KPI on the dashboard is recalculated from the source and compared.</li></ol>
      <div class="note blue" style="margin-top:12px">A test only passes when the dashboard value matches an independently computed one. Anything unproven is shown as such - never as passed.</div></div>
  </div></div>
  ${summary ? projectSummaryHtml(summary, ovw) : ""}`;
  const ut = $("#upToggle"); if (ut) ut.onclick = () => { const w = $("#upWrap"); w.style.display = w.style.display === "none" ? "" : "none"; };
  let files = [];
  const KEEP = /\.(tmdl|json|pbir|pbism|pbip|platform|bim)$/i;
  const pick = (list, viaFolder) => {
    files = Array.from(list).filter(f => viaFolder ? (KEEP.test(f.name) && !/\/\.pbi\//.test(f.webkitRelativePath)) : true);
    $("#picked").innerHTML = files.length ? `${files.length} file(s) selected${viaFolder ? " (model & report definitions only)" : ""}` : "No usable files found";
    $("#upBtn").disabled = !files.length;
  };
  $("#fFolder").onchange = e => pick(e.target.files, true);
  $("#fZip").onchange = e => pick(e.target.files, false);
  const drop = $("#drop");
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); const fl = Array.from(e.dataTransfer.files);
    if (fl.length === 1 && /\.zip$/i.test(fl[0].name)) pick(fl, false); else toast("Drop a .zip, or use 'Choose PBIP folder' for a folder."); };
  $("#upBtn").onclick = async e => {
    const b = e.currentTarget; b.disabled = true; b.innerHTML = '<span class="spin"></span> Analyzing…';
    const fd = new FormData(); files.forEach(f => fd.append("files", f, f.webkitRelativePath || f.name)); fd.append("name", $("#pName").value);
    try { const s = await upload("/api/projects", fd); await afterProject(s.id); toast(`Project created: ${s.measures} measures, ${s.visuals} visuals`); location.hash = "#project"; }
    catch (x) { if (x.message !== "snapshot") toast(x.message); b.disabled = false; b.textContent = "Upload & analyze"; }
  };
  $("#sampleBtn").onclick = async e => { const b = e.currentTarget; b.disabled = true; b.innerHTML = '<span class="spin"></span> Creating sample…';
    try { const s = await api("/api/projects/sample", {method: "POST"}); await afterProject(s.id); location.hash = "#dax"; } catch (x) { if (x.message !== "snapshot") toast(x.message); b.disabled = false; } };
  if (summary) { bindSummary(); renderSourceFiles("#srcbox"); renderScope(); renderColumnFix(summary); }
};

function renderColumnFix(summary) {
  const sc = summary.source_check; const box = $("#colfix"); if (!box) return;
  const un = (sc && sc.unresolved) || [];
  const plat = platName(summary.data_platform);
  let head = "";
  if (!sc) head = "";
  else if (sc.platform && sc.platform !== summary.data_platform) head = `<div class="note blue">The last check was against ${esc(platName(sc.platform))}; the project now uses <b>${esc(plat)}</b> - click Test connections in Settings to re-check.</div>`;
  else if (sc.connection_error) head = `<div class="card" style="margin-top:16px;border-color:#f2c9c4"><div class="panel-h" style="color:var(--fail)">${ico("alert")}<span style="color:var(--ink)">Can't check the project against ${esc(plat)} - connection failed</span></div><div class="panel-b" style="font-size:13px">${esc(sc.connection_error)}<div style="color:var(--muted);margin-top:6px">Fix the connection in Settings; tests that need data stay BLOCKED until then.</div></div></div>`;
  else if (sc.checks) {
    const ch = sc.checks, bad = ch.filter(c => c.status !== "found"), used = ch.filter(c => c.used);
    const cols = ch.reduce((a, c) => [a[0] + c.columns_found, a[1] + c.columns_total], [0, 0]);
    const badge = c => c.status === "found" ? `<span class="dot" style="background:var(--pass)"></span>found` : c.status === "not_found" ? `<span class="dot" style="background:var(--fail)"></span><b style="color:var(--fail)">not found</b>` : `<span class="dot" style="background:var(--ne)"></span>no source`;
    head = `<div class="card" style="margin-top:16px;${bad.some(c => c.used) ? "border-color:#f2c9c4" : ""}"><div class="panel-h">${ico("check")} Project check against ${esc(plat)}<span class="sp"></span><span style="font-size:12.5px;color:var(--muted)">${sc.checked_at ? esc(new Date(sc.checked_at).toLocaleString()) : ""}</span></div><div class="panel-b">
      <div style="font-size:13px;margin-bottom:8px"><b>${ch.length - bad.length}/${ch.length}</b> tables found · <b>${cols[0]}/${cols[1]}</b> mapped columns found${(sc.converted || []).length ? ` · ${sc.converted.length} text date column(s) converted automatically` : ""}. ${bad.some(c => c.used) ? `<span style="color:var(--fail)">Tables marked <b>used</b> feed KPIs in test scope - enter their real name in ${esc(plat)} so those tests can run.</span>` : "All tables behind the KPIs in scope are available."}</div>
      <table><thead><tr><th>Model table</th><th>Used by KPIs in scope</th><th>Name in ${esc(plat)}</th><th>Columns found</th><th>Status</th></tr></thead><tbody>
      ${ch.map(c => `<tr><td><b>${esc(c.table)}</b></td><td>${c.used ? "used" : '<span style="color:var(--muted)">-</span>'}</td><td>${summary.data_platform === "files" ? `<span class="mono">${esc(c.source || "")}</span>` : `<input class="tsrc mono" data-t="${esc(c.table)}" value="${esc(c.source || "")}" placeholder="schema.table or database.schema.table" style="min-width:280px;font:inherit;font-size:12.5px;border:1px solid var(--line);border-radius:8px;padding:6px 8px">`}${c.error ? `<div style="color:var(--fail);font-size:11.5px;max-width:420px">${esc(c.error)}</div>` : ""}</td><td>${c.status === "found" ? `${c.columns_found}/${c.columns_total}` : "-"}</td><td>${badge(c)}</td></tr>`).join("")}</tbody></table>
      ${summary.data_platform === "files" ? "" : `<button class="btn btn-primary btn-sm" id="tsrcSave" style="margin-top:10px">Save table names & re-check</button>`}</div></div>`;
  }
  if (!un.length) { box.innerHTML = head; bindTsrc(); return; }
  box.innerHTML = head + `<div class="card" style="margin-top:16px;border-color:#f2c9c4"><div class="panel-h" style="color:var(--fail)">${ico("alert")}<span style="color:var(--ink)">${un.length} column(s) not found in the source - pick the real column</span></div>
    <div class="panel-b"><div style="font-size:13px;color:var(--ink-2);margin-bottom:8px">Power BI uses a different name than ${esc(platName(summary.data_platform))} (renamed in Power Query or a view). Choose the matching source column; tests and SQL regenerate.</div>
    <table><thead><tr><th>Model table</th><th>Power BI column</th><th>Name tried</th><th>Source column</th></tr></thead><tbody>
    ${un.map(u => `<tr><td>${esc(u.table)}</td><td><b>${esc(u.column)}</b></td><td class="mono">${esc(u.current)}</td><td><select class="select colsel" data-f="${esc(u.field)}" style="min-width:260px">
      <option value="">- choose -</option>${(u.suggestions || []).map(x => `<option value="${esc(x)}">${esc(x)} (suggested)</option>`).join("")}${((sc.columns || {})[u.table] || []).filter(x => !(u.suggestions || []).includes(x)).map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join("")}</select></td></tr>`).join("")}</tbody></table>
    <button class="btn btn-primary btn-sm" id="colSave" style="margin-top:10px">Save column mapping</button></div></div>`;
  bindTsrc();
  $("#colSave").onclick = async () => { const cs = {}; document.querySelectorAll(".colsel").forEach(x => { if (x.value) cs[x.dataset.f] = x.value; });
    if (!Object.keys(cs).length) { toast("Choose at least one column"); return; }
    try { await api(E("/settings"), {method: "PATCH", body: {column_sources: cs}}); toast("Column mapping saved; tests regenerated"); route(); } catch (e) { if (e.message !== "snapshot") toast(e.message); } };
}

function bindTsrc() {
  const b = $("#tsrcSave"); if (!b) return;
  b.onclick = async () => { const ts = {}; document.querySelectorAll(".tsrc").forEach(x => { ts[x.dataset.t] = x.value.trim(); });
    b.disabled = true; toast("Saving and re-checking…");
    try { await api(E("/settings"), {method: "PATCH", body: {table_sources: ts}}); S.envs = await api("/api/environments"); toast("Table names saved; project re-checked"); route(); }
    catch (e) { b.disabled = false; if (e.message !== "snapshot") toast(e.message); } };
}

// ------------------------------------------------------------------ Excel / CSV source files
async function renderSourceFiles(boxSel) {
  const box = $(boxSel); if (!box || !S.env) return;
  let sf; try { sf = await api(E("/source-files")); } catch { box.innerHTML = ""; return; }
  const isFiles = (curEnv() || {}).data?.platform === "files";
  if (!sf.file_based) {
    box.innerHTML = isFiles ? `<div class="card" style="margin-top:16px;border-color:#f6d58a"><div class="panel-h">${ico("layers")} Source files (Excel / CSV)</div><div class="panel-b" style="font-size:13px">
      <b>This project's Power BI model doesn't read from Excel or CSV files</b> - its tables come from ${esc(platName((curEnv() || {}).source_platform_detected || "a database"))}, so there are no source files to upload here.
      <ul style="margin:8px 0 0 18px;padding:0;color:var(--ink-2)"><li>To test an <b>Excel-based dashboard</b>: click <b>Project / Upload PBIP</b> and upload that dashboard's PBIP - its workbooks are listed here automatically.</li>
      <li>To test <b>this</b> dashboard: choose the database it reads from in <b>Data platform</b> (e.g. Databricks) and enter its connection.</li></ul></div></div>` : "";
    return;
  }
  if (!isFiles) { box.innerHTML = ""; return; }
  const stat = x => x === "source folder" ? `<span class="dot" style="background:var(--pass)"></span>found in folder` : x === "uploaded" ? `<span class="dot" style="background:var(--pass)"></span>uploaded` : x === "original path" ? `<span class="dot" style="background:var(--pass)"></span>found at original path` : `<span class="dot" style="background:var(--fail)"></span><b style="color:var(--fail)">missing</b>`;
  box.innerHTML = `<div class="card" style="margin-top:16px;${sf.missing.length ? "border-color:#f2c9c4" : ""}"><div class="panel-h">${ico("layers")} Source files (Excel / CSV) · expected values are computed from these<span class="sp"></span>
    <label class="btn btn-primary btn-sm" style="cursor:pointer">Upload files<input type="file" id="srcFiles" multiple accept=".xlsx,.xlsm,.csv,.tsv,.txt,.parquet,.json" hidden></label></div>
    <div class="panel-b"><div style="font-size:13px;color:var(--ink-2);margin-bottom:8px">${sf.missing.length ? `Upload <b>${sf.missing.map(esc).join(", ")}</b> - the same file(s) the dashboard was refreshed from. ` : "All source files are available. "}Each table is read the way Power Query reads it (sheet or table, header row, renamed columns), then the validation SQL runs on it locally. Re-upload whenever the file changes.</div>
    <table><thead><tr><th>Model table</th><th>File</th><th>Sheet / table</th><th>SQL name</th><th>Status</th></tr></thead><tbody>
    ${sf.tables.map(t => `<tr><td><b>${esc(t.table)}</b></td><td>${esc(t.file)}<div style="color:var(--muted);font-size:11.5px" class="mono">${esc(t.original_path || "")}</div></td><td>${esc(t.item || (/\.(xlsx|xlsm)$/i.test(t.file || "") ? "first sheet" : "—"))}${t.item_kind && t.item_kind !== "Sheet" ? ` <span style="color:var(--muted)">(${esc(t.item_kind.toLowerCase())})</span>` : ""}</td><td class="mono">${esc(t.sql_name)}</td><td>${stat(t.status)}</td></tr>`).join("")}</tbody></table></div></div>`;
  $("#srcFiles").onchange = async e => { const files = Array.from(e.target.files); if (!files.length) return;
    const fd = new FormData(); files.forEach(f => fd.append("files", f, f.name));
    toast("Uploading and loading files…");
    try { const r = await upload(E("/source-files"), fd); S.envs = await api("/api/environments");
      const d = r.connection?.data; toast(d ? (d.ok ? "Files loaded - " : "Check: ") + d.message : "Uploaded"); route(); } catch (x) { if (x.message !== "snapshot") toast(x.message); } };
}

async function renderScope() {
  const box = $("#scopebox"); if (!box) return;
  let sc; try { sc = await api(E("/scope")); } catch { return; }
  const modes = [["dashboard", "KPIs shown on the dashboard", `${sc.displayed_count} measure(s)/field(s) on visible pages, with each visual's report, page, slicer and visual filters applied`],
                 ["requirements", "KPIs from a requirement document or screenshot", "upload a .docx / .pdf / .xlsx / .pptx / .txt or a dashboard screenshot; matches are preselected for you to confirm"],
                 ["custom", "Hand-picked KPIs", "tick the KPIs below"], ["all", "All measures the report uses", `every DAX measure the report needs${sc.unused_count ? ` - ${sc.unused_count} unused measure(s) left out (see Model Usage)` : ""}`]];
  const INC = [["slicers", "Slicers", "wiring check, manual slicer test and 'filtered by slicer' value tests"], ["charts", "Charts point by point", "every axis value of the visuals showing the KPIs"],
               ["visual_checks", "Visual field checks", "the visuals' fields still exist in the model"], ["data_quality", "Data quality", "nulls, duplicates, orphans and freshness of the source tables"]];
  let filter = sc.mode === "dashboard" ? "shown" : sc.mode === "all" ? "all" : "selected";
  const draw = () => {
    const rows = sc.rows.filter(r => filter === "all" || (filter === "shown" ? r.displayed_on.length : (r.selected || r.evidence)));
    box.innerHTML = `<div class="card" style="margin-top:16px"><div class="panel-h">${ico("target")} Test scope <span class="sp"></span><span style="font-weight:400;font-size:13px;color:var(--ink-2)"><b>${sc.in_scope_count}</b> KPI(s) get test cases and SQL</span></div>
    <div class="panel-b"><div class="scope-modes">${modes.map(([k, t, d]) => `<label class="scope-mode ${sc.mode === k ? "on" : ""}"><input type="radio" name="smode" value="${k}" ${sc.mode === k ? "checked" : ""}><div><b>${t}</b><div>${d}</div></div></label>`).join("")}</div>
    <div class="inc-row"><b>Also test</b><span style="color:var(--muted);font-size:12px">${sc.mode === "dashboard" || sc.mode === "all" ? "on by default for this scope" : "off by default: only the selected KPIs are tested"}</span>
      ${INC.map(([k, l, d]) => `<label title="${esc(d)}"><input type="checkbox" class="incchk" data-k="${k}" ${sc.include?.[k] ? "checked" : ""}> ${l}</label>`).join("")}</div>
    <div class="drop" id="reqDrop" style="margin:12px 0;padding:16px;display:${sc.mode === "requirements" ? "block" : "none"}"><b>Requirement document or dashboard screenshot</b>
      <div style="font-size:12.5px;color:var(--ink-2);margin:4px 0 8px">KPI names and card/chart titles found in the file are matched to the model. Screenshots are read with the built-in OCR (small typos are tolerated) - review the ticked list before applying, or tick the KPIs you see.</div>
      <label class="btn btn-outline btn-sm">${ico("plus")} Choose files<input type="file" id="reqFiles" multiple accept=".docx,.pdf,.xlsx,.pptx,.txt,.md,.csv,.png,.jpg,.jpeg" hidden></label>
      ${(sc.files || []).map(f => `<div style="font-size:12.5px;margin-top:6px">${esc(f.file)} - read as ${esc(f.how)} (${f.chars} characters)${f.chars ? "" : " - nothing readable; tick KPIs manually"}</div>`).join("")}</div>
    <div class="filters" style="margin:6px 0 8px">${[["shown", "On dashboard"], ["selected", "Selected / matched"], ["all", "All"]].map(([k, l]) => `<button class="${filter === k ? "on" : ""}" data-sf="${k}">${l}</button>`).join("")}</div>
    <div style="max-height:380px;overflow:auto;border:1px solid var(--line);border-radius:10px"><table><thead><tr><th style="width:34px"></th><th>KPI</th><th>Shown on</th><th>Found in document</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><input type="checkbox" class="kchk" data-m="${esc(r.measure)}" ${(sc.mode === "dashboard" ? r.displayed_on.length : sc.mode === "all" ? true : r.selected) ? "checked" : ""} ${sc.mode === "dashboard" || sc.mode === "all" ? "disabled" : ""}></td>
      <td><b>${esc(r.name)}</b> <span class="mono" style="font-size:11px;color:var(--muted)">${esc(r.kpi_id)}${r.implicit ? " · field on visual" : ""}</span></td>
      <td style="font-size:12.5px">${r.displayed_on.length ? r.displayed_on.slice(0, 3).map(d => `${esc(d.page)} › ${esc(d.title)}`).join("<br>") + (r.displayed_on.length > 3 ? ` +${r.displayed_on.length - 3}` : "") : '<span style="color:var(--muted)">not on a visible page</span>'}</td>
      <td style="font-size:12.5px">${r.evidence ? `${esc(r.evidence.via)}: "${esc(r.evidence.phrase)}" <span style="color:var(--muted)">(${Math.round(r.evidence.score * 100)}%)</span>` : ""}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">Nothing here</td></tr>'}</tbody></table></div>
    <div style="margin-top:10px;display:flex;gap:8px"><button class="btn btn-primary btn-sm" id="scopeSave">Apply scope & regenerate tests</button></div></div></div>`;
    box.querySelectorAll(".incchk").forEach(c => c.onchange = () => { sc.include = {...sc.include, [c.dataset.k]: c.checked}; sc.incTouched = true; });
    box.querySelectorAll("input[name=smode]").forEach(x => x.onchange = () => { sc.mode = x.value; const broad = ["dashboard", "all"].includes(sc.mode); sc.include = {slicers: broad, charts: broad, visual_checks: broad, data_quality: broad}; sc.incTouched = true; filter = sc.mode === "dashboard" ? "shown" : sc.mode === "all" ? "all" : "selected"; if (sc.mode === "custom" && !sc.selected.length) sc.rows.forEach(r => r.selected = !!r.displayed_on.length); draw(); });
    box.querySelectorAll("button[data-sf]").forEach(b => b.onclick = () => { filter = b.dataset.sf; draw(); });
    box.querySelectorAll(".kchk").forEach(c => c.onchange = () => { const r = sc.rows.find(x => x.measure === c.dataset.m); r.selected = c.checked; });
    const rf = $("#reqFiles"); if (rf) rf.onchange = async e => { const fd = new FormData(); Array.from(e.target.files).forEach(f => fd.append("files", f, f.name));
      try { sc = await upload(E("/scope/files"), fd); filter = "selected"; toast(`${sc.rows.filter(r => r.selected).length} KPI(s) matched - review and apply`); draw(); } catch (x) { if (x.message !== "snapshot") toast(x.message); } };
    $("#scopeSave").onclick = async () => { const body = {mode: sc.mode, include: sc.include}; if (sc.mode === "custom" || sc.mode === "requirements") body.selected = sc.rows.filter(r => r.selected).map(r => r.measure);
      try { sc = await api(E("/scope"), {method: "POST", body}); toast(`Scope applied: ${sc.in_scope_count} KPI(s); tests regenerated`); draw(); } catch (e) { if (e.message !== "snapshot") toast(e.message); } };
  };
  draw();
}

async function afterProject(id) {
  S.envs = await api("/api/environments"); S.env = id; fillEnvSelect();
  try { await api(E("/tests/generate"), {method: "POST"}); } catch {}
}

function projectSummaryHtml(s, ovw) {
  const sc = s.source_check, sf = s.source_files, plat = platName(s.data_platform);
  const checks = sc && sc.checks ? sc.checks : [];
  const missingT = checks.filter(c => c.used && c.status !== "found").length;
  let data;
  if (s.data_platform === "demo_sql") data = ["ok", "Built-in sample data (demo)", ""];
  else if (sf && sf.missing && sf.missing.length && s.data_platform === "files") data = ["warn", `Upload ${sf.missing.map(esc).join(", ")}`, "Source files below"];
  else if (!sc || (sc.platform && sc.platform !== s.data_platform)) data = ["todo", `${esc(plat)} - not checked yet`, `<a href="#settings">Open Settings</a>`];
  else if (sc.connection_error) data = ["bad", `${esc(plat)} - connection failed`, `<a href="#settings">Fix in Settings</a>`];
  else if (missingT) data = ["warn", `${esc(plat)} - ${missingT} table(s) not found`, "Fix below"];
  else data = ["ok", `${esc(plat)} · ${checks.filter(c => c.status === "found").length}/${checks.length} tables found`, ""];
  const scope = s.scope || {}, MODE = {dashboard: "KPIs shown on the dashboard", requirements: "KPIs from your document / screenshot", custom: "Hand-picked KPIs", all: "All measures the report uses"};
  const cur = ovw?.current, last = ovw?.last_run, cc = cur?.counts || {};
  const tests = !cur || !cur.total ? ["todo", "No tests yet", `<button class="btn btn-primary btn-sm" id="ckGen">${ico("spark")} Generate</button>`]
    : !last ? ["todo", `${cur.total} tests ready - not run`, `<button class="btn btn-primary btn-sm" id="ckRun">${ico("play")} Run</button>`]
    : [(cc.FAIL || 0) + (cc.ERROR || 0) ? "bad" : "ok", `${cur.total} tests · ${cc.PASS || 0} passed · ${(cc.FAIL || 0) + (cc.ERROR || 0)} failed`, `<a href="#overview">Results</a> <button class="btn btn-ghost btn-sm" id="ckRun">${ico("play")} Run again</button>`];
  const blocking = [...s.notes, ...s.unsupported];
  const rows = [
    ["ok", "Power BI model", `${s.measures} measures · ${s.tables.length} tables · ${s.pages} pages · ${s.visuals} visuals`, `<a href="#dax">DAX Analysis</a>`],
    [data[0], "Data source", data[1], data[2]],
    [blocking.length ? "warn" : "ok", "Test scope", `${esc(MODE[scope.mode] || scope.mode)} · ${scope.in_scope_count ?? (scope.selected || []).length} KPI(s)${blocking.length ? ` · ${blocking.length} column(s) can't be tested` : ""}`, `<a href="#" id="ckScope">Change</a>`],
    [tests[0], "Tests", tests[1], tests[2]]];
  const ICON = {ok: ["var(--pass)", "check"], warn: ["#e8890c", "alert"], bad: ["var(--fail)", "alert"], todo: ["#94a3b8", "clock"]};
  return `<div class="card checklist">${rows.map(([st, t, d, act], i) => `<div class="ck ${st}"><span class="ck-i" style="background:${ICON[st][0]}">${ico(ICON[st][1], 3)}</span>
      <div class="ck-t"><b>${i + 1}. ${t}</b><span>${d}</span></div><div class="ck-a">${act}</div></div>`).join("")}</div>
    <div id="srcbox"></div><div id="colfix"></div><div id="scopebox"></div>
    ${blocking.length ? `<div class="note" style="margin-top:12px"><b>Can't be tested yet (${blocking.length})</b> - these columns are used by the tests in scope but have no source equivalent.<ul style="margin:6px 0 0;padding-left:18px">${blocking.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}
    <details class="card more"><summary>${ico("layers")} Model details & table sources<span class="sp"></span><span class="muted-s" style="margin:0">${s.tables.length} tables · detected ${esc(s.source_platform_detected || "unknown")} · ${s.kpis_business} business / ${s.kpis_inferred} inferred definitions${(s.notes_info || []).length ? ` · ${s.notes_info.length} note(s)` : ""}</span></summary><div class="panel-b">
      <table><thead><tr><th>Model table</th><th>Source type</th><th>Physical source (editable)</th><th>Columns</th><th>Measures</th></tr></thead><tbody>
      ${s.tables.map(t => `<tr><td><b>${esc(t.name)}</b></td><td>${esc(t.source_kind || "")}</td><td>${t.source_kind === "calculated" ? '<span style="color:var(--muted)">calculated in the model</span>' : `<input class="select src-in" data-t="${esc(t.name)}" value="${esc(t.source || "")}" placeholder="${["excel","csv","parquet","json"].includes(t.source_kind) ? "workbook.sheet" : "catalog.schema.table"}" style="min-width:300px" ${s.data_platform === "files" && ["excel","csv","parquet","json"].includes(t.source_kind) ? "readonly title='Read from the file - see Source files'" : ""}>`}</td><td>${t.columns}</td><td>${t.measures}</td></tr>`).join("")}</tbody></table>
      <div style="margin-top:10px;display:flex;gap:10px"><button class="btn btn-ghost btn-sm" id="saveSrc">Save source mapping</button>${curEnv()?.project ? `<span class="sp" style="flex:1"></span><button class="btn btn-ghost btn-sm" id="delProj" style="color:var(--fail)">Delete project</button>` : ""}</div>
      ${(s.notes_info || []).length ? `<div style="margin-top:12px;font-size:13px"><b>Other notes</b> <span style="color:var(--muted)">(don't affect the tests in scope)</span><ul style="margin:6px 0 0;padding-left:18px;color:var(--ink-2)">${s.notes_info.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}</div></details>`;
}

function bindSummary() {
  const g = $("#ckGen"); if (g) g.onclick = e => doGenerate(e.currentTarget);
  const r = $("#ckRun"); if (r) r.onclick = e => doRun(e.currentTarget);
  const cs = $("#ckScope"); if (cs) cs.onclick = e => { e.preventDefault(); $("#scopebox")?.scrollIntoView({behavior: "smooth", block: "start"}); };
  const ra = $("#reAn"); if (ra) ra.onclick = async () => { ra.disabled = true; ra.textContent = "Re-analyzing…";
    try { await api(`/api/projects/${S.env}/reanalyze`, {method: "POST"}); S.envs = await api("/api/environments"); toast("PBIP re-analyzed with the current version"); route(); }
    catch (e) { ra.disabled = false; if (e.message !== "snapshot") toast(e.message); } };
  const sv = $("#saveSrc"); if (sv) sv.onclick = async () => { const ts = {}; document.querySelectorAll(".src-in").forEach(i => ts[i.dataset.t] = i.value.trim());
    try { await api(E("/settings"), {method: "PATCH", body: {table_sources: ts}}); await api(E("/tests/generate"), {method: "POST"}); S.envs = await api("/api/environments"); toast("Source mapping saved; tests regenerated"); } catch (e) { if (e.message !== "snapshot") toast(e.message); } };
  const dp = $("#delProj"); if (dp) dp.onclick = async () => { if (!confirmBox("Delete this project and its results?")) return;
    try { await api(`/api/projects/${S.env}`, {method: "DELETE"}); S.envs = await api("/api/environments"); S.env = S.envs[0]?.id || null; fillEnvSelect(); location.hash = "#project"; route(); } catch (e) { if (e.message !== "snapshot") toast(e.message); } };
}
const confirmBox = msg => { try { return window.confirm(msg); } catch { return true; } };

// ---------- Overview (wireframe)
PAGES.overview = async () => {
  const [ov, testsRaw] = await Promise.all([api(E("/overview")), api(E("/tests"))]);
  const env = curEnv(); const cur = ov.current, c = cur.counts, latest = ov.latest;
  if (!testsRaw.length) { $("#view").innerHTML = `<div class="card empty" style="padding:40px">No tests yet.<div style="margin-top:12px"><button class="btn btn-primary" id="g0">${ico("spark")} Generate test cases</button></div></div>`; $("#g0").onclick = e => doGenerate(e.currentTarget); return; }
  const CORD = ["KPI", "Visual", "Expression", "Data Quality", "Functional"];
  const byId = Object.fromEntries(testsRaw.map(t => [t.id, t]));
  const issues = (c.FAIL || 0) + (c.ERROR || 0), unproven = (c.BLOCKED || 0) + (c["NOT VALIDATED"] || 0) + (c["NOT EXECUTED"] || 0);
  const tr = ov.trend; const prev = tr.length > 1 ? tr[tr.length - 2] : null;
  const delta = (a, b) => (a == null || b == null || a === b) ? "" : `<span class="delta" style="color:${a > b ? "var(--pass)" : "var(--fail)"}">${a > b ? "▲" : "▼"} ${Math.abs(+(a - b).toFixed(1))}</span>`;
  const lastRun = ov.last_run;
  const trust = ov.kpis.map(k => ({...k, verdict: (k.dax === "FAIL" || k.data === "FAIL") ? "At risk" : (k.basis === "business" && k.dax === "PASS" && k.data === "PASS") ? "Trusted" : "Unverified"}));
  const why = t => t.verdict === "Trusted" ? "Definition, DAX and data all check out" : t.dax === "FAIL" ? "DAX does not match the definition" : t.data === "FAIL" ? "Numbers differ from the source" :
    t.basis !== "business" ? "No business definition yet" : !t.data ? "Data not reconciled yet" : t.data === "PARTIAL" ? "Some numbers not reconciled" : "Evidence incomplete";
  const tcount = v => trust.filter(t => t.verdict === v).length;
  const ORDER = {FAIL: 0, ERROR: 1, BLOCKED: 2, "NOT VALIDATED": 3, "NOT EXECUTED": 4};
  const attention = Object.values(latest).filter(r => r.status in ORDER && byId[r.test_id])
    .sort((x, y) => ORDER[x.status] - ORDER[y.status] || (byId[x.test_id].display_id || "").localeCompare(byId[y.test_id].display_id || "", undefined, {numeric: true}));
  const headline = !lastRun ? "Not run yet" : issues ? `${issues} issue${issues > 1 ? "s" : ""} need${issues > 1 ? "" : "s"} attention` : unproven ? "No failures - some tests still unproven" : "All tests passed";
  const tone = !lastRun ? "var(--ink)" : issues ? "var(--fail)" : unproven ? "#b26a00" : "var(--pass)";
  const cats = Object.entries(ov.categories).sort((a, b) => CORD.indexOf(a[0]) - CORD.indexOf(b[0]));
  const CATCOL = {"KPI": "#2f6ff0", "Data Quality": "#14995a", "Expression": "#6d4fe0", "Functional": "#e8890c", "Visual": "#11a3b8"};
  const stat = (val, lbl, sub, color) => `<div class="card kstat"><div class="v" style="color:${color || "var(--ink)"}">${val}</div><div class="l">${lbl}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
  $("#view").innerHTML = `
  <div class="ovbar"><div><div class="hl" style="color:${tone}">${headline}</div>
      <div class="sub">${lastRun ? `Last run ${when(lastRun.finished_at)} · ${cur.total} tests${lastRun.mode !== "LIVE" ? ` · <span class="badge b-demo">${esc(lastRun.mode)}</span>` : ""}` : `${cur.total} tests ready`}</div></div>
    <span class="sp"></span><button class="btn btn-ghost" id="genBtn">${ico("spark")} Regenerate</button><button class="btn btn-primary" id="runBtn">${ico("play")} Run all tests</button></div>
  <div class="kstats">
    ${stat(`${cur.validated_pass_rate ?? "—"}${cur.validated_pass_rate != null ? "%" : ""} ${delta(cur.validated_pass_rate, prev?.rate)}`, "QA score", `${cur.coverage}% of tests proven`)}
    ${stat(`${c.PASS || 0}`, "Passed", `of ${cur.total}`, "var(--pass)")}
    ${stat(`${issues}`, "Failed", issues ? `${c.FAIL || 0} failed · ${c.ERROR || 0} errors` : "", issues ? "var(--fail)" : "")}
    ${stat(`${unproven}`, "Not proven", unproven ? "blocked · no definition · manual" : "", unproven ? "#b26a00" : "")}</div>
  <div class="ovgrid">
    <div class="card"><div class="panel-h">${ico("alert")} Needs attention <span class="cnt">${attention.length}</span><span class="sp"></span><a href="#execution" id="allIss" class="lnk">All results ${ico("chevron")}</a></div>
      <div class="att">${attention.length ? attention.slice(0, 8).map(r => { const t = byId[r.test_id];
        return `<div class="att-r click" data-id="${esc(t.id)}">${pill(r.status)}<div class="att-t"><b>${esc(t.name)}</b><span>${esc((r.message || "").slice(0, 140))}</span></div><span class="mono att-id">${esc(t.display_id || "")}</span></div>`; }).join("")
        + (attention.length > 8 ? `<a href="#execution" class="att-more">+${attention.length - 8} more</a>` : "")
        : `<div class="empty" style="padding:28px">${ico("check")} Nothing needs attention.</div>`}</div></div>
    <div class="card"><div class="panel-h">${ico("trend")} By category</div><div class="panel-b">
      ${cats.map(([k, v]) => { const ex = v.PASS + v.FAIL; const pct = ex ? Math.round(v.PASS / ex * 100) : null;
        return `<div class="catrow"><span>${esc(CAT_LABEL[k] || k)}</span><div class="bar"><i style="width:${pct ?? 0}%;background:${CATCOL[k] || "#94a3b8"}"></i></div><b>${pct == null ? "—" : pct + "%"}</b></div>`; }).join("")}
      ${tr.length > 1 ? `<div style="margin-top:14px">${trendChart(tr)}</div>` : `<div class="muted-s">Run again to see the trend.</div>`}</div></div></div>
  <div class="card"><div class="panel-h">${ico("shield")} KPI trust <span class="sp"></span>
    <span class="chipline"><span class="pill s-PASS">${tcount("Trusted")} trusted</span><span class="pill s-FAIL">${tcount("At risk")} at risk</span><span class="pill s-NOT-VALIDATED">${tcount("Unverified")} unverified</span></span></div>
    <div class="panel-b"><div class="trust-grid">${trust.map(t => `<a class="trust ${t.verdict.replace(" ", "-")}" href="#dax" title="DAX: ${esc(t.dax || "—")} · Data: ${esc(t.data || "not run")} · Definition: ${esc(t.basis)}"><div class="tn">${esc(t.name)}</div><div class="tv">${t.verdict}</div><div class="tw">${why(t)}</div></a>`).join("")}</div></div></div>`;
  $("#runBtn").onclick = e => doRun(e.currentTarget);
  $("#genBtn").onclick = e => doGenerate(e.currentTarget);
  $("#allIss").onclick = () => { S.execFilter = "FAIL"; };
  $("#view").querySelectorAll(".att-r").forEach(r => r.onclick = () => showTest(byId[r.dataset.id], latest[r.dataset.id]));
};
const shortResult = r => typeof r.actual === "number" ? fmt(r.actual) : r.status === "NOT VALIDATED" ? "No definition" : r.status === "BLOCKED" ? "Blocked" : esc(String(r.actual ?? "—")).slice(0, 22);

// ---------- Tab 1: DAX Analysis (KPI-wise)
PAGES.dax = async () => {
  const d = await api(E("/dax-analysis"));
  if (!d.available) { $("#view").innerHTML = `<div class="card empty">${esc(d.reason)} <a href="#project">Upload a PBIP</a></div>`; return; }
  const allMs = d.measures.filter(m => m.kpi_id).sort((a, b) => a.kpi_id.localeCompare(b.kpi_id));
  const showAll = S.daxAll || false;
  const ms = showAll ? allMs : allMs.filter(m => m.in_scope !== false);
  const fail = ms.filter(m => daxVerdict(m).s === "FAIL").length, pass = ms.length - fail;
  const verified = ms.filter(m => m.definition_status === "PASS").length, nodef = ms.filter(m => m.definition_status === "NOT VALIDATED").length;
  let filter = "All";
  $("#view").innerHTML = `<div class="page-h"><div><h3>DAX Analysis</h3><p>Is each measure written correctly - and does it do what the business definition says?</p></div>
    <button class="btn btn-ghost" id="tplBtn">${ico("download")} Definition template (Excel)</button>
    <label class="btn btn-primary">${ico("plus")} Upload KPI definitions<input type="file" id="defFile" accept=".xlsx,.csv" hidden></label></div>
  <div class="stats">
    <div class="card stat" style="min-height:92px"><div class="ic" style="background:var(--pass)">${ico("check", 3)}</div><div class="val">${pass} / ${ms.length}</div><div class="lbl">KPIs - DAX passed</div></div>
    <div class="card stat" style="min-height:92px"><div class="ic" style="background:var(--fail)">${ico("x", 3)}</div><div class="val">${fail} / ${ms.length}</div><div class="lbl">KPIs - DAX failed</div></div>
    <div class="card stat" style="min-height:92px"><div class="ic" style="background:var(--purple)">${ico("shield", 2.4)}</div><div class="val">${verified}</div><div class="lbl">Business logic verified</div></div>
    <div class="card stat" style="min-height:92px"><div class="ic" style="background:var(--nv)">${ico("alert", 3)}</div><div class="val">${nodef}</div><div class="lbl">No business definition</div><div class="sub">syntax-only checks</div></div></div>
  ${nodef ? `<div class="note blue">${nodef} KPI(s) have no business definition, so their PASS covers syntax and best practice only. Download the template, fill in the definitions with the business owner, and upload it. Or open a KPI and enter its definition.</div>` : ""}
  ${d.kpis_without_measure.length ? `<div class="note">KPIs whose measure is missing from the model: ${d.kpis_without_measure.map(k => `${k.kpi_id} → [${esc(k.measure)}]`).join(", ")}</div>` : ""}
  <div style="display:flex;align-items:center;gap:10px"><div class="filters" id="fl" style="flex:1"></div>
    <label style="font-size:13px;color:var(--ink-2);display:flex;gap:6px;align-items:center"><input type="checkbox" id="daxAll" ${showAll ? "checked" : ""}> Show all ${allMs.length} measures (not only the ${allMs.filter(m => m.in_scope !== false).length} in test scope)</label></div>
  <div class="card"><table><thead><tr><th>KPI</th><th>Measure</th><th style="width:22%">Business definition</th><th style="width:22%">What the DAX does</th><th>Syntax & refs</th><th>Business logic</th><th>DAX Status</th><th style="width:20%">Key finding</th></tr></thead><tbody id="dtb"></tbody></table></div>`;
  const draw = () => {
    const rows = ms.filter(m => filter === "All" || (filter === "FAIL" ? daxVerdict(m).s === "FAIL" : filter === "PASS" ? daxVerdict(m).s === "PASS" : m.definition_status === "NOT VALIDATED"));
    $("#fl").innerHTML = [["All", ms.length], ["PASS", pass], ["FAIL", fail], ["No definition", nodef]].map(([k, n]) => `<button class="${k === filter ? "on" : ""}" data-f="${k}">${k} (${n})</button>`).join("");
    $("#fl").querySelectorAll("button").forEach(b => b.onclick = () => { filter = b.dataset.f; draw(); });
    $("#dtb").innerHTML = rows.map(m => { const f = [...m.static_findings, ...m.definition_findings].sort((a, b) => ["error", "warning", "info"].indexOf(a.severity) - ["error", "warning", "info"].indexOf(b.severity))[0];
      return `<tr class="click" data-m="${esc(m.measure)}"><td class="mono" style="white-space:nowrap">${esc(m.kpi_id)}</td><td style="min-width:150px"><b>${esc(m.kpi_name || m.measure)}</b>${m.unused ? ' <span class="badge b-planned" title="Nothing in the report needs this measure - see Model Usage">unused</span>' : ""}<div class="mono" style="font-size:11.5px;color:var(--muted)">[${esc(m.measure)}]</div>${m.referenced_by.length ? `<div style="font-size:11px;color:var(--muted)">used by ${m.referenced_by.slice(0, 2).map(esc).join(", ")}${m.referenced_by.length > 2 ? ` +${m.referenced_by.length - 2}` : ""}</div>` : ""}</td>
      <td style="font-size:12.5px">${m.basis === "business" ? esc(m.business_definition) : '<span style="color:var(--muted)">Not supplied</span>'}</td>
      <td style="font-size:12.5px">${esc(m.dax_logic || "")}</td>
      <td>${m.static_status === "FAIL" ? pill("FAIL") : m.static_status === "PASS" ? pill("PASS") : '<span class="pill s-WARN">' + ico("alert", 3) + "Warnings</span>"}</td>
      <td>${logicCell(m)}</td><td>${verdictPill(m)}</td>
      <td style="font-size:12.5px;color:var(--ink-2)">${f ? `<b>${f.rule}</b> ${esc(f.message).slice(0, 110)}` : "—"}</td></tr>`; }).join("") || '<tr><td colspan="8" class="empty">No KPIs</td></tr>';
    $("#dtb").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => showKpi(ms.find(x => x.measure === tr.dataset.m)));
  };
  draw();
  $("#daxAll").onchange = e => { S.daxAll = e.target.checked; route(); };
  $("#tplBtn").onclick = () => download(E("/definitions/template?format=xlsx"));
  $("#defFile").onchange = async e => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
    try { const r = await upload(E("/definitions"), fd); toast(`${r.updated} definition(s) imported${r.errors.length ? `, ${r.errors.length} error(s)` : ""}; tests regenerated`);
      if (r.errors.length) openDrawer("Import messages", `<section>${r.errors.map(x => `<div class="note">${esc(x)}</div>`).join("")}</section>`); else route(); }
    catch (x) { if (x.message !== "snapshot") toast(x.message); } };
};

async function showKpi(m) {
  const kpis = await api(E("/kpis")); const k = kpis.find(x => x.id === m.kpi_id) || {};
  const model = (await api(E("/metadata"))).bi_model?.body; const tables = model ? model.tables : [];
  const spec = k.kind === "ratio" ? k.numerator : k.measure; const den = k.denominator;
  const col = s => s && s.column ? s.column.split(".").slice(1).join(".") : "";
  const fl = s => s ? s.filters.map(f => { const [t, ...c] = f.field.split("."); const ref = `${t}[${c.join(".")}]`; const v = f.value;
    const lit = v === true ? "TRUE" : v === false ? "FALSE" : typeof v === "number" ? v : Array.isArray(v) ? `{${v.map(x => JSON.stringify(x)).join(", ")}}` : JSON.stringify(v);
    return f.op === "is null" ? `ISBLANK(${ref})` : f.op === "is not null" ? `NOT ISBLANK(${ref})` : f.op === "in" ? `${ref} IN ${lit}` : `${ref} ${f.op === "!=" ? "<>" : f.op} ${lit}`; }).join("; ") : "";
  const biz = k.basis === "business";
  const opt = (vals, cur) => vals.map(v => `<option ${v === cur ? "selected" : ""}>${v}</option>`).join("");
  const AG = ["", "COUNT_ROWS", "COUNT_DISTINCT", "SUM", "AVG", "COUNT", "MIN", "MAX"];
  const tbls = ["", ...tables.map(t => t.name)];
  openDrawer(`${esc(m.kpi_id)} · ${esc(m.kpi_name || m.measure)}`, `
    <section><div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">${verdictPill(m)} ${logicCell(m)} <span class="badge ${biz ? "b-implemented" : "b-partial"}">${biz ? "business definition" : "inferred from DAX (draft)"}</span></div>
      <dl class="kv"><dt>Business definition</dt><dd>${biz ? esc(k.business_definition) : '<span style="color:var(--muted)">Not supplied</span>'}</dd>
      <dt>What the DAX does</dt><dd>${esc(m.dax_logic || "")}</dd>${m.basis === "business" ? `<dt>Definition as logic</dt><dd>${esc(m.logic || "")}</dd>` : ""}<dt>Used in visuals</dt><dd>${m.visuals.map(esc).join(", ") || "—"}</dd>
      ${(m.translation_notes || []).length ? `<dt>Notes</dt><dd>${m.translation_notes.map(esc).join("<br>")}</dd>` : ""}</dl></section>
    <section><h4>DAX expression</h4><pre class="code">${esc(m.expression)}</pre>${findingsHtml(m)}</section>
    <section><h4>Business definition ${biz ? "(edit)" : "(enter to verify business logic)"}</h4>
      <div class="form">
        <label class="full">Business definition<textarea id="fDef" rows="2" placeholder="Plain-language definition agreed with the business owner">${biz ? esc(k.business_definition) : ""}</textarea></label>
        <label>Type<select id="fType">${opt(["simple", "ratio"], k.kind === "ratio" ? "ratio" : "simple")}</select></label>
        <label>Aggregation${k.kind === "ratio" ? " (numerator)" : ""}<select id="fAgg">${opt(AG, biz && spec ? spec.aggregation : "")}</select></label>
        <label>Table<select id="fTbl">${opt(tbls, biz && spec ? spec.table : "")}</select></label>
        <label>Column<input id="fCol" value="${biz ? esc(col(spec)) : ""}" placeholder="e.g. Amount (blank for COUNT_ROWS)"></label>
        <label class="full">Filters (DAX style, ; separated)<input id="fFlt" value="${biz ? esc(fl(spec)) : ""}" placeholder='Sales[Status] = "Closed"; Sales[IsValid] = TRUE'></label>
        <div class="full den" style="display:${k.kind === "ratio" ? "contents" : "none"}">
          <label>Denominator aggregation<select id="fDAgg">${opt(AG, biz && den ? den.aggregation : "")}</select></label>
          <label>Denominator table<select id="fDTbl">${opt(tbls, biz && den ? den.table : "")}</select></label>
          <label>Denominator column<input id="fDCol" value="${biz ? esc(col(den)) : ""}"></label>
          <label class="full">Denominator filters<input id="fDFlt" value="${biz ? esc(fl(den)) : ""}"></label></div>
        <label>Multiplier<input id="fMul" value="${biz && k.multiplier !== 1 ? k.multiplier : ""}" placeholder="100 for %"></label>
        <label>Tolerance<select id="fTolT">${opt(["exact", "absolute", "percent"], k.tolerance?.type || "exact")}</select></label>
        <label>Tolerance value<input id="fTolV" value="${k.tolerance?.type !== "exact" ? k.tolerance?.value ?? "" : ""}"></label>
        <label>Rounding (dp)<input id="fRnd" value="${k.tolerance?.rounding ?? ""}"></label>
        <label class="full">Expected SQL (optional - overrides generated SQL, must return expected_value)<textarea id="fSql" rows="3" class="mono">${esc(k.expected_sql || "")}</textarea></label>
      </div>
      <div style="display:flex;gap:8px;margin-top:10px"><button class="btn btn-primary btn-sm" id="fSave">Save definition & re-check</button>
      ${k.basis === "inferred" ? `<button class="btn btn-ghost btn-sm" id="fUseInf" title="Copies the DAX interpretation into the form so you can correct it">Start from DAX interpretation</button>` : ""}</div>
    </section>`);
  $("#fType").onchange = e => $(".den").style.display = e.target.value === "ratio" ? "contents" : "none";
  const ui = $("#fUseInf"); if (ui) ui.onclick = () => { const s2 = k.kind === "ratio" ? k.numerator : k.measure; if (!s2) { toast("DAX could not be translated automatically"); return; }
    $("#fType").value = k.kind; $(".den").style.display = k.kind === "ratio" ? "contents" : "none"; $("#fAgg").value = s2.aggregation; $("#fTbl").value = s2.table; $("#fCol").value = col(s2); $("#fFlt").value = fl(s2);
    if (k.denominator) { $("#fDAgg").value = k.denominator.aggregation; $("#fDTbl").value = k.denominator.table; $("#fDCol").value = col(k.denominator); $("#fDFlt").value = fl(k.denominator); }
    $("#fMul").value = k.multiplier !== 1 ? k.multiplier : ""; toast("Review and correct against the business requirement before saving"); };
  $("#fSave").onclick = async () => {
    const row = {"Business Definition": $("#fDef").value, "Type (simple/ratio)": $("#fType").value, "Aggregation": $("#fAgg").value, "Table": $("#fTbl").value,
      "Column": $("#fCol").value, "Filters": $("#fFlt").value, "Multiplier": $("#fMul").value, "Tolerance Type (exact/absolute/percent)": $("#fTolT").value,
      "Tolerance Value": $("#fTolV").value, "Rounding (dp)": $("#fRnd").value, "Expected SQL (optional)": $("#fSql").value, "KPI Name": m.kpi_name || m.measure};
    if ($("#fType").value === "ratio") Object.assign(row, {"Denominator Aggregation": $("#fDAgg").value, "Denominator Table": $("#fDTbl").value, "Denominator Column": $("#fDCol").value, "Denominator Filters": $("#fDFlt").value});
    if (!row["Business Definition"].trim()) { toast("Enter the business definition"); return; }
    try { await api(E(`/kpis/${m.kpi_id}/definition`), {method: "POST", body: row}); toast("Definition saved; DAX re-checked and tests regenerated"); closeDrawer(); route(); }
    catch (e) { if (e.message !== "snapshot") toast(e.message.replace(/^\d+: /, "")); }
  };
}

// ---------- Tab 2: Test Cases with SQL
PAGES.tests = async () => {
  const [tests, latest, kpis] = await Promise.all([api(E("/tests")), api(E("/results/latest")), api(E("/kpis"))]);
  const kname = Object.fromEntries(kpis.map(k => [k.id, k.name]));
  const st = ui("tests");
  let cat = st.cat || "All", q = st.q || "", open = st.open || new Set(), limit = st.limit || 30;
  if (S.openTest) { open.add(S.openTest); S.openTest = null; }
  st.open = open;
  const cats = ["All", ...new Set(tests.map(t => t.category))];
  const groups = () => { const rows = tests.filter(t => (cat === "All" || t.category === cat) && (!q || (t.id + t.name).toLowerCase().includes(q)));
    const g = {}; rows.forEach(t => { const key = t.kpi_id ? `${kname[t.kpi_id] || t.kpi_id}\u0001${t.kpi_id}` : t.id.endsWith(".BIND") ? "Visual field checks" : t.category === "Visual" || t.category === "Functional" ? "Slicers & visuals" : "Data quality"; (g[key] = g[key] || []).push(t); });
    const rank = k => k.includes("\u0001") ? 0 : k.startsWith("Slicers") ? 1 : k.startsWith("Visual field") ? 2 : 3;
    const bad = k => (g[k] || []).some(t => latest[t.id] && !["PASS"].includes(latest[t.id].status)) ? 0 : 1;
    return Object.fromEntries(Object.entries(g).sort((a, b) => rank(a[0]) - rank(b[0]) || (rank(a[0]) === 0 ? bad(a[0]) - bad(b[0]) : 0) || a[0].localeCompare(b[0], undefined, {numeric: true}))); };
  $("#view").innerHTML = `<div class="page-h"><div><h3>Test Cases</h3><p>Every test with its ${esc(platName(curEnv().data.platform))} SQL (expected) and DAX query (actual).</p></div>
    <button class="btn btn-ghost" id="dlSql">${ico("download")} All SQL (.sql)</button><button class="btn btn-ghost" id="dlCsv">${ico("download")} Test cases (.csv)</button><button class="btn btn-primary" id="gen">${ico("spark")} Regenerate</button></div>
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px"><div class="filters" id="fl" style="margin:0;flex:1"></div><input class="search light" id="q" placeholder="Search tests…"></div><div id="groups"></div>`;
  const draw = () => {
    Object.assign(st, {cat, q, limit});
    $("#fl").innerHTML = cats.map(c => `<button class="${c === cat ? "on" : ""}" data-c="${esc(c)}">${esc(c === "All" ? "All" : CAT_LABEL[c] || c)} (${c === "All" ? tests.length : tests.filter(t => t.category === c).length})</button>`).join("");
    $("#fl").querySelectorAll("button").forEach(b => b.onclick = () => { cat = b.dataset.c; draw(); });
    const g = groups();
    const entries = Object.entries(g);
    const FOLD = n => n === "Visual field checks" || n === "Data quality";
    st.unfold = st.unfold || new Set();
    $("#groups").innerHTML = entries.slice(0, limit).map(([key, ts]) => { const [name, kid] = key.split("\u0001");
      const bad = ts.filter(t => latest[t.id] && latest[t.id].status !== "PASS"), folded = FOLD(name) && !st.unfold.has(name) && !q;
      const shown = folded ? bad : ts, passN = ts.filter(t => latest[t.id]?.status === "PASS").length;
      return `<div class="card tgroup"><div class="panel-h">${ico(kid ? "target" : name.startsWith("Slicers") ? "cursor" : name.startsWith("Visual") ? "layers" : "db")} ${esc(name)}${kid ? ` <span class="mono kid">${esc(kid)}</span>` : ""}<span class="sp"></span>
        <span class="gsum">${passN}/${ts.length} passed</span></div>
      ${shown.length ? `<table class="tlist"><tbody>
      ${shown.map(t => { const r = latest[t.id]; const isOpen = open.has(t.id); const p = t.params || {};
        return `<tr class="click" data-id="${esc(t.id)}"><td style="width:24px">${(p.sql || p.dax) ? `<span class="caret ${isOpen ? "open" : ""}">${ico("chevron")}</span>` : ""}</td><td class="mono tid" title="${esc(t.id)}">${esc(t.display_id || t.id)}</td>
          <td><span class="tname">${esc(t.name)}</span> <span class="tcat">${esc(CAT_LABEL[t.category] || t.category)}</span></td>
          <td style="width:130px">${pill(r?.status)}</td><td style="width:30px;text-align:center">${t.approval_status === "APPROVED" ? `<span title="Approved by ${esc(t.approved_by || "")}" style="color:var(--pass)">${ico("check", 3)}</span>` : t.approval_status === "REJECTED" ? `<span title="Rejected" style="color:var(--fail)">✕</span>` : ""}</td></tr>
          ${isOpen ? `<tr class="sqlrow"><td></td><td colspan="4"><div class="sqlgrid">
            ${p.sql ? `<div><div class="sqlh"><b>Expected · ${esc(platName(curEnv().data.platform))} SQL</b><button class="btn btn-ghost btn-sm" data-copy="sql" data-id="${esc(t.id)}">Copy</button></div><pre class="code">${esc(p.sql)}</pre></div>` : ""}
            ${p.dax ? `<div><div class="sqlh"><b>Actual · DAX query</b><button class="btn btn-ghost btn-sm" data-copy="dax" data-id="${esc(t.id)}">Copy</button></div><pre class="code">${esc(p.dax)}</pre></div>` : ""}</div>
            <div style="display:flex;gap:10px;margin:6px 0 4px;align-items:center"><button class="btn btn-primary btn-sm" data-det="${esc(t.id)}">Open test</button>${r ? `<span style="font-size:12.5px;color:var(--ink-2)">${esc(r.message).slice(0, 160)}</span>` : ""}</div></td></tr>` : ""}`; }).join("")}</tbody></table>` : ""}
      ${FOLD(name) && !q ? `<button class="fold" data-fold="${esc(name)}">${folded ? `${ts.length - bad.length ? `Show ${ts.length - bad.length} passing` : ""}${!bad.length && !(ts.length - bad.length) ? "" : ""}` : "Show problems only"}</button>` : ""}</div>`; }).join("")
      + (entries.length > limit ? `<div style="text-align:center;margin:10px 0"><button class="btn btn-ghost" id="more">Show more</button></div>` : "");
    $("#groups").querySelectorAll("button[data-fold]").forEach(b => b.onclick = () => { const n = b.dataset.fold; st.unfold.has(n) ? st.unfold.delete(n) : st.unfold.add(n); draw(); });
    const mb = $("#more"); if (mb) mb.onclick = () => { limit += 30; draw(); };
    $("#groups").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => { const id = tr.dataset.id; open.has(id) ? open.delete(id) : open.add(id); draw(); });
    $("#groups").querySelectorAll("button[data-copy]").forEach(b => b.onclick = e => { e.stopPropagation(); const t = tests.find(x => x.id === b.dataset.id); copyText(t.params[b.dataset.copy]); });
    $("#groups").querySelectorAll("button[data-det]").forEach(b => b.onclick = e => { e.stopPropagation(); showTest(tests.find(x => x.id === b.dataset.det), latest[b.dataset.det]); });
  };
  $("#q").value = q;
  $("#q").oninput = e => { q = e.target.value.toLowerCase(); draw(); };
  $("#gen").onclick = e => doGenerate(e.currentTarget);
  $("#dlSql").onclick = () => download(E("/sql-scripts"));
  $("#dlCsv").onclick = () => {
    if (SNAP) { toast("Downloads are available in the local app."); return; }
    const cols = ["Test ID", "Test name", "Business objective", "Category", "Preconditions", "Source mapping", "Target mapping", "Test steps", "Expected result", "Actual result", "Tolerance", "Execution status", "Evidence", "Explanation", "Approval status", "Expected SQL", "DAX query"];
    const q2 = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const lines = [cols.join(",")].concat(tests.map(t => { const r = latest[t.id] || {}; const tol = t.tolerance ? `${t.tolerance.type} ${t.tolerance.type !== "exact" ? t.tolerance.value : ""}` : "";
      return [t.id, t.name, t.business_objective, CAT_LABEL[t.category] || t.category, t.preconditions.join(" | "), t.source_mapping, t.target_mapping, t.steps.map((s, i) => `${i + 1}. ${s}`).join(" | "), t.expected_result, r.actual ?? "", tol, r.status || "NOT EXECUTED", (r.evidence || []).map(e => e.label).join("; "), t.ai_explanation, t.approval_status, t.params?.sql || "", t.params?.dax || ""].map(q2).join(","); }));
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["﻿" + lines.join("\n")], {type: "text/csv"})); a.download = `test_cases_${S.env}.csv`; a.click();
  };
  draw();
};

const NOT_AUTOMATED = t => t.executor === "manual";
async function refreshTest(id) {
  const [tests, latest] = await Promise.all([api(E("/tests")), api(E("/results/latest"))]);
  const t = tests.find(x => x.id === id);
  if (t) showTest(t, latest[id], true);
  softRefresh();
  return latest[id];
}

function showTest(t, r, keepScroll) {
  const tol = t.tolerance ? `${t.tolerance.type}${t.tolerance.type !== "exact" ? " " + t.tolerance.value + (t.tolerance.type === "percent" ? "%" : "") : ""}${t.tolerance.rounding != null ? ", round " + t.tolerance.rounding + "dp" : ""}` : "—";
  const ph = (t.filter_context || []).find(f => f.value === "<set value>");
  openDrawer(`${esc(t.display_id || t.id)} <span class="mono" style="font-size:11.5px;color:var(--muted);font-weight:400;margin-left:6px">${esc(t.id)}</span>`, `<section><div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">${pill(r?.status)} ${catTag2(t.category)}</div>
    <h4 style="font-size:16px">${esc(t.name)}</h4>
    ${ph ? `<div class="note">Choose a filter value for <b>${esc(ph.field)}</b>: <input class="select" id="fv" style="min-width:140px;padding:5px 8px"> <button class="btn btn-primary btn-sm" id="fvSave">Set</button></div>` : ""}
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      ${t.approval_status === "APPROVED" ? `<span class="btn btn-sm" style="background:#e3f6ec;color:var(--pass);cursor:default">${ico("check", 3)} Approved${t.approved_by ? " by " + esc(t.approved_by) : ""}</span><button class="btn btn-ghost btn-sm" data-a="PENDING">Undo approval</button><button class="btn btn-ghost btn-sm" data-a="REJECTED">Reject</button>`
        : t.approval_status === "REJECTED" ? `<span class="btn btn-sm" style="background:#fdeceb;color:var(--fail);cursor:default">Rejected${t.approved_by ? " by " + esc(t.approved_by) : ""}</span><button class="btn btn-primary btn-sm" data-a="APPROVED">Approve</button><button class="btn btn-ghost btn-sm" data-a="PENDING">Undo</button>`
        : `<button class="btn btn-primary btn-sm" data-a="APPROVED">Approve</button><button class="btn btn-ghost btn-sm" data-a="REJECTED">Reject</button>`}
      <span style="flex:1"></span>
      ${NOT_AUTOMATED(t) ? `<span style="font-size:12.5px;color:var(--muted)">Manual test - record the outcome below</span>` : `<button class="btn btn-ghost btn-sm" data-a="RUN">${ico("play", 3)} Run this test</button>`}</div>
    <div id="actMsg" style="margin-top:8px;font-size:13px"></div></section>
    ${r ? resultHtml(r) : '<section><div class="empty" style="padding:14px">Not run yet.</div></section>'}
    ${t.executor !== "kpi_value" ? `<section><h4>Record outcome ${NOT_AUTOMATED(t) ? "" : "manually"}</h4><div style="font-size:12.5px;color:var(--ink-2);margin-bottom:8px">${NOT_AUTOMATED(t) ? "This check is done by a person: follow the steps above on the report, then record what you saw." : "Use this when the automated check can't run here (e.g. no BI connection) and you verified it on the report yourself."} Logged as MANUAL evidence with your name.</div>
      <div class="form" style="grid-template-columns:3fr auto auto auto"><label>Observation<input id="oNote" placeholder="e.g. Selecting 'ABC East' updated all 6 visuals"></label>
      <button class="btn btn-sm" style="align-self:end;background:var(--pass);color:#fff" data-o="PASS">Passed</button><button class="btn btn-sm" style="align-self:end;background:var(--fail);color:#fff" data-o="FAIL">Failed</button><button class="btn btn-ghost btn-sm" style="align-self:end" data-o="BLOCKED">Blocked</button></div></section>` : ""}
    ${t.executor === "kpi_value" ? `<section><h4>Record result manually</h4><div style="font-size:12.5px;color:var(--ink-2);margin-bottom:8px">${r?.expected != null && r?.mode !== "MANUAL" ? "Read the value on the dashboard (same filters) and enter it as Actual - the expected value is already computed." : `Run the SQL in ${esc(platName(curEnv().data.platform))}, read the value in the report (same filters), enter both.`}</div>
      <div class="form" style="grid-template-columns:1fr 1fr;align-items:end"><label>Expected (source)<input id="mExp" inputmode="decimal" placeholder="${r?.expected != null && r?.mode !== "MANUAL" ? "computed: " + r.expected + " (leave blank)" : ""}"></label><label>Actual (${esc(biShort(curEnv()))})<input id="mAct" inputmode="decimal"></label><label>Note<input id="mNote" placeholder="e.g. UAT workspace, refreshed 09:00"></label><button class="btn btn-primary btn-sm" id="mSave" style="align-self:end">Record</button></div></section>` : ""}
    <details class="tdef"><summary>Test definition & queries</summary><section><dl class="kv"><dt>Business objective</dt><dd>${esc(t.business_objective)}</dd><dt>Preconditions</dt><dd>${t.preconditions.map(esc).join("<br>") || "—"}</dd>
    <dt>Source mapping</dt><dd>${esc(t.source_mapping)}</dd><dt>Target mapping</dt><dd class="mono">${esc(t.target_mapping)}</dd>
    <dt>Steps</dt><dd><ol style="margin:0;padding-left:18px">${t.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol></dd><dt>Expected result</dt><dd>${esc(t.expected_result)}</dd>
    <dt>Tolerance</dt><dd>${tol}</dd><dt>Explanation</dt><dd>${esc(t.ai_explanation)}</dd></dl></section>
    ${t.params?.sql ? `<section><h4>Expected value · SQL</h4><pre class="code">${esc(t.params.sql)}</pre></section>` : ""}
    ${t.params?.dax ? `<section><h4>Actual value · DAX</h4><pre class="code">${esc(t.params.dax)}</pre></section>` : ""}</details>`, keepScroll);
  const label = t.display_id || t.id;
  const busy = (b, on) => { b.disabled = on; if (on) b.dataset.txt = b.innerHTML, b.innerHTML = "Working…"; else if (b.dataset.txt) b.innerHTML = b.dataset.txt; };
  const said = res => `${label} → ${STATUS_META[res.status]?.label || res.status}${res.message ? ": " + res.message.slice(0, 160) : ""}`;
  $("#drawer").querySelectorAll("button[data-a]").forEach(b => b.onclick = async () => {
    busy(b, true);
    try {
      if (b.dataset.a === "RUN") {
        await api(E("/runs"), {method: "POST", body: {test_ids: [t.id]}});
        const res = await refreshTest(t.id);
        if (res) { toast(said(res)); const m = $("#actMsg"); if (m) m.innerHTML = `Ran just now: ${pill(res.status)} <span style="color:var(--ink-2)">${esc(res.message || "").slice(0, 200)}</span>`; }
      } else {
        await api(E(`/tests/${encodeURIComponent(t.id)}`), {method: "PATCH", body: {approval_status: b.dataset.a}});
        toast(b.dataset.a === "PENDING" ? `${label}: approval undone` : `${label} ${b.dataset.a.toLowerCase()}`);
        await refreshTest(t.id);
      }
    } catch (e) { busy(b, false); if (e.message !== "snapshot") toast(e.message); }
  });
  $("#drawer").querySelectorAll("button[data-o]").forEach(b => b.onclick = async () => {
    busy(b, true);
    try { const res = await api(E(`/tests/${encodeURIComponent(t.id)}/manual-result`), {method: "POST", body: {status: b.dataset.o, note: $("#oNote").value}});
      toast(said(res)); await refreshTest(t.id); }
    catch (e) { busy(b, false); if (e.message !== "snapshot") toast(e.message); }
  });
  const ms = $("#mSave"); if (ms) ms.onclick = async () => { const num = v => v.trim() === "" ? null : Number(v.replace(/,/g, ""));
    busy(ms, true);
    try { const res = await api(E(`/tests/${encodeURIComponent(t.id)}/manual-result`), {method: "POST", body: {expected: num($("#mExp").value), actual: num($("#mAct").value), note: $("#mNote").value}});
      toast(said(res)); await refreshTest(t.id); } catch (e) { busy(ms, false); if (e.message !== "snapshot") toast(e.message); } };
  const fv = $("#fvSave"); if (fv) fv.onclick = async () => { try { await api(E(`/tests/${encodeURIComponent(t.id)}`), {method: "PATCH", body: {filter_value: [ph.field, $("#fv").value]}}); toast("Filter value set"); await refreshTest(t.id); } catch (e) { if (e.message !== "snapshot") toast(e.message); } };
}

// ---------- Model usage (unused measures / columns / tables)
PAGES.usage = async () => {
  let u = await api(E("/usage"));
  if (!u.available) { $("#view").innerHTML = `<div class="card empty">${esc(u.reason)} <a href="#project">Upload a PBIP</a></div>`; return; }
  const st = ui("usage"); let tab = st.tab || "usage";
  const MB = b => b == null ? "" : b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : b >= 1024 ? Math.round(b / 1024) + " KB" : b + " B";
  const LBL = {report: ["var(--pass)", "Used in report"], indirect: ["#2f6fed", "Used indirectly"], structure: ["#7c5cff", "Needed by model"],
               kept: ["#0e9f9a", "Kept by you"], used_by_unused: ["#e8833a", "Used only by unused"], unused: ["var(--fail)", "Unused"]};
  const pct = (a, b) => b ? Math.round(a / b * 100) : 0;
  const head = () => {
    const S2 = u.summary;
    return `<div class="page-h"><div><h3>Model Usage</h3><p>What the report really uses, what it costs, and a cleaned model.</p></div>
      <label class="btn btn-ghost" style="cursor:pointer">${ico("plus")} Add other reports<input type="file" id="uRep" multiple hidden accept=".zip,.json,.pbir"></label>
      <label class="btn btn-ghost" style="cursor:pointer">${ico("db")} ${u.vpax ? "Replace" : "Upload"} model size (.vpax)<input type="file" id="uVpax" hidden accept=".vpax,.json"></label>
      <button class="btn btn-ghost" id="uCsv">${ico("download")} Export (.csv)</button></div>
    <div class="stats" style="grid-template-columns:repeat(${S2.size_known ? 5 : 4},1fr);margin-bottom:12px">${[
      [`${S2.measures_unused}<small> / ${S2.measures}</small>`, `Measures unused (${pct(S2.measures_unused, S2.measures)}%)${S2.measures_used_by_unused ? ` · ${S2.measures_used_by_unused} only by unused` : ""}`, S2.measures_unused > 0],
      [`${S2.columns_unused}<small> / ${S2.columns}</small>`, `Columns unused (${pct(S2.columns_unused, S2.columns)}%)`, S2.columns_unused > 0],
      [S2.calculated_columns_unused, "Calculated columns unused", S2.calculated_columns_unused > 0],
      [S2.tables_unused, "Tables entirely unused", S2.tables_unused > 0]].concat(S2.size_known ? [[`${MB(S2.size_unused_bytes)}<small> / ${MB(S2.size_total_bytes)}</small>`, `Memory in unused columns (${pct(S2.size_unused_bytes, S2.size_total_bytes)}%)`, S2.size_unused_bytes > 0]] : []).map(([v, l, bad]) =>
      `<div class="card" style="padding:16px 18px"><div style="font-size:26px;font-weight:700;line-height:1.1;color:${bad ? "var(--fail)" : "var(--ink)"}">${v}</div><div style="color:var(--ink-2);font-size:13px;margin-top:4px">${l}</div></div>`).join("")}</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px;font-size:12.5px;color:var(--ink-2)">
      <span>Scanned: <b>${S2.reports_scanned}</b> report(s), <b>${S2.visuals_scanned}</b> visuals</span>
      ${(u.extra_reports || []).map(r => `<span class="chip">${esc(r.name)} · ${r.visuals} visuals <a href="#" data-rmrep="${esc(r.name)}" title="Remove" style="margin-left:4px">✕</a></span>`).join("")}
      ${u.vpax ? `<span class="chip">Sizes: ${esc(u.vpax.source)} · ${MB(u.vpax.total_bytes)} <a href="#" id="rmVpax" title="Remove" style="margin-left:4px">✕</a></span>` : `<span style="color:var(--muted)">No model sizes - export a .vpax from DAX Studio (Advanced › Export Metrics) to rank unused columns by memory.</span>`}
      ${S2.kept ? `<span class="chip">${S2.kept} kept by you</span>` : ""}</div>
    ${u.notes.map(n => `<div class="note blue" style="margin-bottom:8px">${esc(n)}</div>`).join("")}
    ${u.unresolved_report_refs.length ? `<div class="note" style="margin-bottom:8px"><b>Broken references in the report</b> (field no longer in the model): ${u.unresolved_report_refs.slice(0, 6).map(esc).join(" · ")}</div>` : ""}
    <div class="filters" id="uTabs" style="margin:6px 0 12px">${[["usage", "Usage"], ["clean", "Clean up"], ["bpa", "Best practices"]].map(([k, l]) => `<button class="${tab === k ? "on" : ""}" data-tab="${k}">${l}</button>`).join("")}</div>
    <div id="uBody"></div>`;
  };
  const render = () => { $("#view").innerHTML = head(); bindHead(); ({usage: drawUsage, clean: drawClean, bpa: drawBpa})[tab](); };
  const refresh = async () => { u = await api(E("/usage")); render(); };
  const bindHead = () => {
    $("#uTabs").querySelectorAll("button").forEach(b => b.onclick = () => { tab = st.tab = b.dataset.tab; render(); });
    $("#uCsv").onclick = () => download(E("/usage.csv"));
    $("#uRep").onchange = async e => { const fd = new FormData(); Array.from(e.target.files).forEach(f => fd.append("files", f, f.webkitRelativePath || f.name));
      try { u = await upload(E("/usage/reports"), fd); toast(`Added: ${u.added.join(", ")} - their fields now count as used`); render(); } catch (x) { if (x.message !== "snapshot") toast(x.message); } };
    $("#uVpax").onchange = async e => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f, f.name);
      try { u = await upload(E("/usage/vpax"), fd); toast("Model sizes loaded"); render(); } catch (x) { if (x.message !== "snapshot") toast(x.message); } };
    $("#view").querySelectorAll("[data-rmrep]").forEach(a => a.onclick = async e => { e.preventDefault(); u = await api(E(`/usage/reports/${encodeURIComponent(a.dataset.rmrep)}`), {method: "DELETE"}); render(); });
    const rv = $("#rmVpax"); if (rv) rv.onclick = async e => { e.preventDefault(); u = await api(E("/usage/vpax"), {method: "DELETE"}); render(); };
  };

  // ---- Usage tab
  const drawUsage = () => {
    let filter = st.filter || "unused", type = st.type || "All", q = st.q || "", tbl = st.tbl || "";
    const sized = u.summary.size_known;
    $("#uBody").innerHTML = `<div class="grid-2" style="grid-template-columns:1fr 2.6fr;gap:16px;align-items:start">
      <div class="card"><div class="panel-h">${ico("db")} Tables</div><div class="panel-b" style="padding:0"><table><thead><tr><th>Table</th><th>Unused</th>${sized ? "<th>Size</th>" : ""}</tr></thead><tbody>
        <tr class="click ${tbl === "" ? "sel" : ""}" data-t=""><td><b>All tables</b></td><td></td>${sized ? "<td></td>" : ""}</tr>
        ${u.tables.map(t => `<tr class="click ${tbl === t.table ? "sel" : ""}" data-t="${esc(t.table)}"><td>${esc(t.table)}${t.entirely_unused ? ' <span class="badge b-planned">entire table unused</span>' : ""}${t.hidden ? ' <span style="color:var(--muted);font-size:11px">hidden</span>' : ""}</td><td>${t.unused}/${t.objects}</td>${sized ? `<td style="font-size:12px">${MB(t.size_bytes)}${t.size_unused_bytes ? `<div style="color:var(--fail)">${MB(t.size_unused_bytes)} unused</div>` : ""}</td>` : ""}</tr>`).join("")}</tbody></table></div></div>
      <div class="card"><div class="panel-b"><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px"><div class="filters" id="uF" style="margin:0"></div>
        <select class="select" id="uT">${["All", "Measure", "Column", "Calculated column"].map(t => `<option ${t === type ? "selected" : ""}>${t}</option>`).join("")}</select><input class="search light" id="uQ" placeholder="Search…" value="${esc(q)}"></div>
        <div id="uRows"></div></div></div></div>`;
    const draw = () => {
      Object.assign(st, {filter, type, q, tbl});
      const base = u.objects.filter(o => (!tbl || o.table === tbl) && (type === "All" || o.type === type) && (!q || (o.table + " " + o.name).toLowerCase().includes(q.toLowerCase())));
      const F = [["unused", "Unused", o => o.status === "unused" || o.status === "used_by_unused"], ["report", "Used in report", o => o.status === "report"], ["indirect", "Used indirectly", o => o.status === "indirect"],
                 ["structure", "Needed by model", o => o.status === "structure"], ["kept", "Kept", o => o.status === "kept"], ["all", "All", () => true]];
      $("#uF").innerHTML = F.map(([k, l, fn]) => `<button class="${filter === k ? "on" : ""}" data-f="${k}">${l} (${base.filter(fn).length})</button>`).join("");
      $("#uF").querySelectorAll("button").forEach(b => b.onclick = () => { filter = b.dataset.f; draw(); });
      const rows = base.filter(F.find(f => f[0] === filter)[2]);
      $("#uRows").innerHTML = `<div style="max-height:620px;overflow:auto;border:1px solid var(--line);border-radius:10px"><table><thead><tr><th>Object</th><th>Type</th><th>Status</th>${sized ? "<th>Size</th>" : ""}<th>Used by</th></tr></thead><tbody>
        ${rows.slice(0, 600).map(o => `<tr class="click" data-i="${u.objects.indexOf(o)}"><td><b>${esc(o.name)}</b><div class="mono" style="font-size:11px;color:var(--muted)">${esc(o.table)}${o.hidden ? " · hidden" : ""}</div></td><td style="font-size:12.5px">${esc(o.type)}</td>
          <td style="white-space:nowrap"><span class="dot" style="background:${LBL[o.status][0]}"></span>${LBL[o.status][1]}</td>${sized ? `<td style="font-size:12px;white-space:nowrap">${MB(o.size_bytes)}${o.cardinality != null ? `<div style="color:var(--muted)">${o.cardinality.toLocaleString()} distinct</div>` : ""}</td>` : ""}
          <td style="font-size:12px;color:var(--ink-2)">${o.used_by.slice(0, 2).map(esc).join("<br>")}${o.used_by_count > 2 ? `<br><span style="color:var(--muted)">+${o.used_by_count - 2} more</span>` : ""}</td></tr>`).join("") || `<tr><td colspan="${sized ? 5 : 4}" class="empty">Nothing here</td></tr>`}
        </tbody></table></div>${rows.length > 600 ? `<div style="font-size:12px;color:var(--muted);margin-top:6px">Showing 600 of ${rows.length} - narrow with the filters or export the CSV.</div>` : ""}`;
      $("#uRows").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => showObj(u.objects[+tr.dataset.i]));
    };
    $("#uT").onchange = e => { type = e.target.value; draw(); };
    $("#uQ").oninput = e => { q = e.target.value; draw(); };
    $("#uBody").querySelectorAll("tr[data-t]").forEach(tr => tr.onclick = () => { tbl = tr.dataset.t; $("#uBody").querySelectorAll("tr[data-t]").forEach(x => x.classList.toggle("sel", x === tr)); draw(); });
    draw();
  };
  const showObj = o => {
    const unusedish = o.status === "unused" || o.status === "used_by_unused";
    openDrawer(`${esc(o.type)} · ${esc(o.ref)}`, `<section><div style="margin-bottom:8px;display:flex;gap:10px;align-items:center"><span><span class="dot" style="background:${LBL[o.status][0]}"></span><b>${LBL[o.status][1]}</b></span>${o.hidden ? '<span class="badge b-partial">hidden</span>' : ""}
        ${o.size_bytes != null ? `<span class="badge b-implemented">${MB(o.size_bytes)}${o.cardinality != null ? ` · ${o.cardinality.toLocaleString()} distinct` : ""}</span>` : ""}<span style="flex:1"></span>
        ${o.status === "kept" ? `<button class="btn btn-ghost btn-sm" id="kp" data-keep="0">Stop keeping</button>` : unusedish ? `<button class="btn btn-ghost btn-sm" id="kp" data-keep="1" title="Used by another report, Excel or a tool outside this upload">Keep (used elsewhere)</button>` : ""}</div>
      ${o.status === "unused" ? `<div class="note">Nothing in the scanned report(s), the model's relationships, sort orders, hierarchies, calculated tables or security roles needs this ${o.type.toLowerCase()}. It is included in <b>Clean up</b>.</div>` : ""}
      ${o.status === "used_by_unused" ? `<div class="note">Only other unused objects reference it, so it can be removed together with them in one pass (included in <b>Clean up</b>).</div>` : ""}
      ${o.used_by.length ? `<h4>${o.status === "used_by_unused" ? "Referenced by" : "Needed by"}</h4><ul style="margin:0;padding-left:18px">${o.used_by.map(x => `<li>${esc(x)}</li>`).join("")}</ul>${o.used_by_count > o.used_by.length ? `<div style="color:var(--muted);font-size:12px">+${o.used_by_count - o.used_by.length} more</div>` : ""}` : ""}</section>
      ${o.expression ? `<section><h4>DAX</h4><pre class="code">${esc(o.expression)}</pre></section>` : ""}`);
    const kp = $("#kp"); if (kp) kp.onclick = async () => { u = await api(E("/usage/keep"), {method: "POST", body: {keys: [o.key], keep: kp.dataset.keep === "1"}});
      toast(kp.dataset.keep === "1" ? `${o.name} kept - excluded from clean-up and treated as used` : `${o.name} no longer kept`); const n = u.objects.find(x => x.key === o.key); render(); if (n) showObj(n); };
  };

  // ---- Clean up tab
  const drawClean = async () => {
    const o = st.clean || {measures: true, calc_columns: true, data_columns: true};
    const qs = () => `measures=${o.measures}&calc_columns=${o.calc_columns}&data_columns=${o.data_columns}`;
    $("#uBody").innerHTML = `<div class="card"><div class="panel-b">
      <div style="font-size:13.5px;margin-bottom:10px">Creates a <b>cleaned copy of your PBIP</b> with the unused objects removed (your upload is not changed). The copy is re-read and checked: if any remaining measure, calculated column or table, security role or visual would still reference a removed object, it is not offered.</div>
      <div class="inc-row" style="margin-top:0"><b>Remove</b>${[["measures", "Unused measures"], ["calc_columns", "Unused calculated columns"], ["data_columns", "Unused data columns (+ Power Query step)"]].map(([k, l]) => `<label><input type="checkbox" class="cop" data-k="${k}" ${o[k] ? "checked" : ""}> ${l}</label>`).join("")}</div>
      <div id="cRes" style="margin-top:12px"><div class="empty">Checking the cleaned copy…</div></div></div></div>`;
    $("#uBody").querySelectorAll(".cop").forEach(c => c.onchange = () => { o[c.dataset.k] = c.checked; st.clean = o; drawClean(); });
    let r; try { r = await api(E(`/usage/cleanup?${qs()}`)); } catch (e) { $("#cRes").innerHTML = `<div class="note">${esc(e.message)}</div>`; return; }
    const by = {}; r.changes.filter(c => !/^(cultures|perspectives)\//.test(c.file.replace(/\\/g, "/"))).forEach(c => (by[c.table] = by[c.table] || []).push(c));
    $("#cRes").innerHTML = `${r.ok ? `<div style="display:flex;gap:12px;align-items:center;margin-bottom:10px"><span class="pill s-PASS">${ico("check", 3)} Safety check passed</span>
        <span style="font-size:13px"><b>${r.counts.measures}</b> measures, <b>${r.counts.columns}</b> columns removed · <b>${r.counts.queries}</b> Power Query step(s) added</span><span style="flex:1"></span>
        <button class="btn btn-primary" id="cDl" ${r.counts.measures + r.counts.columns ? "" : "disabled"}>${ico("download")} Download cleaned PBIP (.zip)</button></div>
        <div class="note" style="margin-bottom:10px"><b>To open in Power BI:</b> extract the zip first (right-click › <i>Extract All</i> - Power BI can't open files inside a zip), then double-click <b class="mono">${esc((r.open_file || "the .pbip file").split("/").pop())}</b>.${(r.structure_fixes || []).length ? ` <span style="color:var(--ink-2)">Added so it opens: ${r.structure_fixes.map(esc).join("; ")}.</span>` : ""}</div>`
      : `<div class="note" style="border-color:#f2c9c4"><b>Not offered - the safety check found problems:</b><ul style="margin:6px 0 0 18px">${r.problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul></div>`}
      ${r.skipped.length ? `<div class="note" style="margin-bottom:10px"><b>Left for you:</b> ${r.skipped.map(s => `${esc(s.table)}: ${s.columns.map(esc).join(", ")}`).join(" · ")} - their Power Query is not a simple let … in query; use the M code below.</div>` : ""}
      <div style="max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:10px"><table><thead><tr><th>Table</th><th>Changes</th></tr></thead><tbody>
      ${Object.entries(by).map(([t, cs]) => `<tr><td><b>${esc(t)}</b></td><td style="font-size:12.5px">${cs.map(c => `${esc(c.action)}: <span class="mono">${esc(c.object)}</span>`).join("<br>")}</td></tr>`).join("") || '<tr><td colspan="2" class="empty">Nothing to remove</td></tr>'}</tbody></table></div>
      ${Object.keys(r.m_code).length ? `<h4 style="margin:16px 0 6px">Power Query (M) per table</h4><div style="font-size:12.5px;color:var(--ink-2);margin-bottom:6px">Already applied in the cleaned copy. To change an existing file instead, paste the query into Power Query › Advanced Editor.</div>
        ${Object.entries(r.m_code).map(([t, code]) => `<details style="margin-bottom:6px"><summary style="cursor:pointer"><b>${esc(t)}</b> <button class="btn btn-ghost btn-sm" data-cp="${esc(t)}" style="margin-left:8px">Copy</button></summary><pre class="code">${esc(code)}</pre></details>`).join("")}` : ""}
      <div style="font-size:12px;color:var(--muted);margin-top:10px">After downloading: unzip, open the .pbip in Power BI Desktop, refresh, check every page, then replace your original. Objects you mark "Keep" on the Usage tab are never removed.</div>`;
    const dl = $("#cDl"); if (dl) dl.onclick = () => download(E(`/usage/cleanup.zip?${qs()}`));
    $("#cRes").querySelectorAll("[data-cp]").forEach(b => b.onclick = e => { e.preventDefault(); copyText(r.m_code[b.dataset.cp]); });
  };

  // ---- Best practices tab
  const drawBpa = async () => {
    $("#uBody").innerHTML = '<div class="empty">Checking…</div>';
    const b = await api(E("/best-practices"));
    const SC = {High: "var(--fail)", Medium: "#e8833a", Low: "#c9a400", Info: "var(--muted)"};
    $("#uBody").innerHTML = `<div style="display:flex;gap:8px;margin-bottom:10px">${Object.entries(b.counts).map(([k, v]) => `<span class="chip"><span class="dot" style="background:${SC[k]}"></span>${k}: <b>${v}</b></span>`).join("")}
      <span style="font-size:12.5px;color:var(--muted);align-self:center">DAX rules are on the DAX Analysis tab.</span></div>
      ${b.findings.map((f, i) => `<div class="card" style="margin-bottom:10px"><div class="panel-b"><div style="display:flex;gap:10px;align-items:center"><span class="dot" style="background:${SC[f.severity]}"></span><b>${esc(f.title)}</b>
        <span class="badge ${f.scope === "model" ? "b-implemented" : "b-partial"}">${f.scope}</span><span class="mono" style="font-size:11px;color:var(--muted)">${f.rule}</span><span style="flex:1"></span><span style="font-size:12px;color:var(--muted)">${f.severity}</span></div>
        <div style="font-size:13px;color:var(--ink-2);margin:6px 0">${esc(f.why)} <b>Fix:</b> ${esc(f.fix)}</div>
        <details><summary style="cursor:pointer;font-size:12.5px">${f.count} object(s)</summary><div class="mono" style="font-size:12px;margin-top:6px;max-height:200px;overflow:auto">${f.objects.map(esc).join("<br>")}</div></details></div></div>`).join("") || '<div class="card empty">No findings - nicely kept model.</div>'}`;
  };
  render();
};

// ---------- Test execution
PAGES.execution = async () => {
  const runs = await api(E("/runs"));
  const st = ui("execution");
  let sel = (st.sel && runs.some(r => r.run_id === st.sel) && !st.followLatest) ? st.sel : runs[0]?.run_id, filter = S.execFilter || st.filter || "All"; S.execFilter = null;
  const drawRun = async () => {
    const [res, tests] = await Promise.all([sel ? api(`/api/runs/${sel}/results`) : [], api(E("/tests"))]);
    const tmap = Object.fromEntries(tests.map(t => [t.id, t]));
    const ORD = {FAIL: 0, ERROR: 1, BLOCKED: 2, "NOT VALIDATED": 3, "NOT EXECUTED": 4, PASS: 5};
    const sts = ["All", ...Object.keys(STATUS_META).filter(k => res.some(r => r.status === k))];
    const rows = res.filter(r => filter === "All" || r.status === filter || (filter === "FAIL" && r.status === "ERROR"))
      .sort((a, b) => (ORD[a.status] ?? 9) - (ORD[b.status] ?? 9) || (tmap[a.test_id]?.display_id || "").localeCompare(tmap[b.test_id]?.display_id || "", undefined, {numeric: true}));
    const num = v => typeof v === "number";
    const result = r => num(r.expected) && num(r.actual) && ["kpi_value"].includes(tmap[r.test_id]?.executor) ? `<span class="mono">${fmt(r.actual)}</span> <span class="muted-i">dashboard</span> vs <span class="mono">${fmt(r.expected)}</span> <span class="muted-i">source</span>${r.status !== "PASS" ? `<div class="rmsg">${esc(r.message).slice(0, 140)}</div>` : ""}`
      : `<span class="rmsg0">${esc(r.message).slice(0, 170)}</span>`;
    $("#res").innerHTML = `<div class="filters">${sts.map(s => `<button class="${s === filter ? "on" : ""}" data-s="${s}">${s === "All" ? "All" : STATUS_META[s].label} (${s === "All" ? res.length : res.filter(r => r.status === s).length})</button>`).join("")}</div>
      <div class="card"><table class="tlist"><thead><tr><th>Test</th><th style="width:130px">Status</th><th>Result</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="click" data-id="${esc(r.test_id)}"><td><span class="tname">${esc(tmap[r.test_id]?.name || r.test_id)}</span><div class="mono" style="font-size:11.5px;color:var(--muted)">${esc(tmap[r.test_id]?.display_id || "")} · ${esc(CAT_LABEL[tmap[r.test_id]?.category] || tmap[r.test_id]?.category || "")}</div></td>
        <td>${pill(r.status)}</td><td style="font-size:13px">${result(r)}</td></tr>`).join("") || '<tr><td colspan="3" class="empty">Nothing in this filter</td></tr>'}</tbody></table></div>`;
    Object.assign(st, {sel, filter});
    $("#res").querySelectorAll(".filters button").forEach(b => b.onclick = () => { filter = b.dataset.s; drawRun(); });
    $("#res").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => showTest(tmap[tr.dataset.id], res.find(r => r.test_id === tr.dataset.id)));
  };
  $("#view").innerHTML = `<div class="page-h"><div><h3>Test Execution</h3><p>Results per run - problems first. Click a row for evidence.</p></div>
    <select class="select" id="runSel">${runs.map(r => `<option value="${r.run_id}">${when(r.started_at)} · ${r.mode} · ${r.summary ? r.summary.counts.PASS + "/" + r.summary.total + " pass" : "running"}</option>`).join("")}</select>
    <button class="btn btn-primary" id="run">${ico("play")} Run all tests</button></div><div id="res">${runs.length ? "" : '<div class="card empty">No runs yet.</div>'}</div>`;
  $("#run").onclick = e => doRun(e.currentTarget);
  $("#runSel").value = sel || ""; st.followLatest = false;
  $("#runSel").onchange = e => { sel = e.target.value; drawRun(); };
  if (runs.length) drawRun();
};

// ---------- Data validation
PAGES.validation = async () => {
  const [latest, tests] = await Promise.all([api(E("/results/latest")), api(E("/tests"))]);
  const tmap = Object.fromEntries(tests.map(t => [t.id, t]));
  const kpiRes = Object.values(latest).filter(r => tmap[r.test_id]?.executor === "kpi_value");
  const grain = Object.values(latest).filter(r => tmap[r.test_id]?.executor === "kpi_grain");
  const dq = Object.values(latest).filter(r => tmap[r.test_id]?.category === "Data Quality");
  const sortId = (a, b) => a.test_id.localeCompare(b.test_id, undefined, {numeric: true});
  $("#view").innerHTML = `<div class="page-h"><div><h3>Data Validation</h3><p>Source value vs ${biShort(curEnv())} value, same filters.</p></div></div>
  <div class="card" style="margin-bottom:16px"><div class="panel-h">${ico("scale")} KPI reconciliation</div><table><thead><tr><th>Test</th><th>Context</th><th>Expected (source)</th><th>Actual (BI)</th><th>Difference</th><th>Tolerance</th>${new Set(kpiRes.map(r => r.mode)).size > 1 ? "<th>Mode</th>" : ""}<th>Status</th></tr></thead><tbody>
    ${kpiRes.sort(sortId).map(r => { const t = tmap[r.test_id]; return `<tr class="click" data-id="${esc(r.test_id)}"><td><b>${esc(t.name)}</b><div class="mono" style="font-size:11.5px;color:var(--muted)">${esc(t.display_id || "")}</div></td><td style="font-size:12px">${t.filter_context.map(f => `${esc(f.field)} ${esc(f.op)} ${esc(f.value)}`).join("<br>") || "—"}</td>
      <td class="mono">${fmt(r.expected)}</td><td class="mono">${fmt(r.actual)}</td><td class="mono" style="color:${r.status === "FAIL" ? "var(--fail)" : "inherit"}">${fmt(r.difference)}</td><td style="font-size:12px">${t.tolerance ? t.tolerance.type + (t.tolerance.type !== "exact" ? " " + t.tolerance.value : "") : ""}</td>${new Set(kpiRes.map(x => x.mode)).size > 1 ? `<td><span class="badge ${r.mode === "DEMO" ? "b-demo" : r.mode === "MANUAL" ? "b-partial" : "b-implemented"}">${r.mode}</span></td>` : ""}<td>${pill(r.status)}</td></tr>`; }).join("") || '<tr><td colspan="8" class="empty">Not run yet</td></tr>'}</tbody></table></div>
  <div class="card" style="margin-bottom:16px"><div class="panel-h">${ico("trend")} Breakdowns & charts (row-level reconciliation)</div><table><thead><tr><th>Test</th><th>Rows (source / BI)</th><th>Rows outside tolerance</th><th>Status</th></tr></thead><tbody>
    ${grain.sort(sortId).map(r => `<tr class="click" data-id="${esc(r.test_id)}"><td><b>${esc(tmap[r.test_id].name)}</b><div class="mono" style="font-size:11.5px;color:var(--muted)">${esc(r.test_id)}</div></td><td class="mono">${fmt(r.expected)} / ${fmt(r.actual)}</td><td class="mono">${fmt(r.difference)}</td><td>${pill(r.status)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">Not run yet</td></tr>'}</tbody></table></div>
  <div class="card"><div class="panel-h">${ico("db")} Data quality checks</div><table><thead><tr><th>Check</th><th>Source object</th><th>Result</th><th>Status</th></tr></thead><tbody>
    ${dq.sort(sortId).map(r => `<tr class="click" data-id="${esc(r.test_id)}"><td>${esc(tmap[r.test_id].name)}</td><td class="mono">${esc(tmap[r.test_id].source_mapping)}</td><td class="mono">${fmt(r.actual)}</td><td>${pill(r.status)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">Not run yet</td></tr>'}</tbody></table></div>`;
  $("#view").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => showTest(tmap[tr.dataset.id], latest[tr.dataset.id]));
};

// ---------- Reports
PAGES.reports = async () => {
  const [ov, tests, dax] = await Promise.all([api(E("/overview")), api(E("/tests")), api(E("/dax-analysis"))]);
  const env = curEnv(); const cur = ov.current, c = cur.counts, latest = ov.latest;
  const tmap = Object.fromEntries(tests.map(t => [t.id, t]));
  const list = Object.values(latest).filter(r => tmap[r.test_id]);
  const ms = (dax.measures || []).filter(m => m.kpi_id);
  $("#view").innerHTML = `<div class="page-h no-print"><div><h3>QA Report</h3><p>Print or save as PDF from your browser.</p></div><button class="btn btn-primary" id="pr">${ico("download")} Print / PDF</button></div>
  <div class="card report" style="padding:26px 30px"><h2 style="margin:0">${biShort(env)} QA Report - ${esc(env.name)}</h2>
  <div style="color:var(--muted);margin:4px 0 14px">Generated ${when(new Date().toISOString())} · latest result per test · modes: ${[...new Set(list.map(r => r.mode))].join(", ") || "—"}</div>
  ${list.some(r => r.mode === "DEMO") ? '<div class="note">Includes DEMO results on sample data. Not evidence about any production system.</div>' : ""}
  <h3>Summary</h3><p><b>${c.PASS}</b> of <b>${cur.total}</b> tests passed, <b>${c.FAIL}</b> failed and <b>${c.ERROR}</b> errored. QA score <b>${cur.validated_pass_rate ?? "—"}%</b> (PASS ÷ PASS+FAIL) at <b>${cur.coverage}%</b> coverage. <b>${c.BLOCKED + c["NOT VALIDATED"] + c["NOT EXECUTED"]}</b> tests could not be validated and are <u>not</u> counted as passes.
  DAX: <b>${ov.dax.pass}</b>/${ov.dax.total} measures pass, <b>${ov.dax.fail}</b> fail, business logic verified for <b>${ov.dax.verified}</b>.</p>
  <h3>KPI traceability</h3><p style="color:var(--muted);margin-top:-6px">Business definition → DAX measure → source SQL → expected vs actual → result</p>
  <table><thead><tr><th>KPI</th><th>Definition basis</th><th>DAX status</th><th>Business logic</th><th>Data reconciliation</th><th>Verdict</th></tr></thead><tbody>
  ${ov.kpis.map(k => { const m = ms.find(x => x.kpi_id === k.id); const v = (k.dax === "FAIL" || k.data === "FAIL") ? "At risk" : (k.basis === "business" && k.dax === "PASS" && k.data === "PASS") ? "Trusted" : "Unverified";
    return `<tr><td><b>${k.id}</b> ${esc(k.name)}</td><td>${k.basis}</td><td>${m ? verdictPill(m) : "—"}</td><td>${m ? logicCell(m) : "—"}</td><td>${k.data ? (k.data === "PARTIAL" ? '<span class="pill s-WARN">Partial</span>' : pill(k.data)) : "not run"}</td><td><b class="verdict-${v.replace(" ", "-")}">${v}</b></td></tr>`; }).join("")}</tbody></table>
  <h3>Failures and errors</h3>${list.filter(r => ["FAIL", "ERROR"].includes(r.status)).map(r => `<div style="border-left:3px solid var(--fail);padding:6px 12px;margin:8px 0"><b>${esc(r.test_id)}</b> - ${esc(tmap[r.test_id]?.name)} ${sev(r.severity)}<br>${esc(r.message)}${r.root_cause_hypothesis && !r.message.includes(r.root_cause_hypothesis.replace(/^\[[^\]]+\]\s*/, "").replace(/^DAX analyzer finding \w+: /, "")) ? `<br><i style="color:var(--muted)">Likely cause: ${esc(r.root_cause_hypothesis.replace(/^\[[^\]]+\]\s*/, "").replace(/^DAX analyzer finding \w+: /, ""))}</i>` : ""}</div>`).join("") || "<p>None.</p>"}
  <h3>Not validated / blocked</h3>${list.filter(r => ["BLOCKED", "NOT VALIDATED", "NOT EXECUTED"].includes(r.status)).map(r => `<div style="padding:4px 0">${pill(r.status)} <b>${esc(r.test_id)}</b> - ${esc(r.message)}</div>`).join("") || "<p>None.</p>"}</div>`;
  $("#pr").onclick = () => SNAP ? toast("Printing is available in the local app.") : window.print();
};

// ---------- Settings
PAGES.settings = async () => {
  const env = curEnv(); const se = S.session;
  let audit = []; try { audit = await api("/api/audit"); } catch {}
  const bi = env.bi.config, da = env.data.config;
  const dataOpts = S.platforms.filter(p => ["data", "database", "file"].includes(p.kind));
  const biOpts = S.platforms.filter(p => p.kind === "bi");
  const opt = (list, cur, sqlOnly) => list.map(p => `<option value="${p.key}" ${p.key === cur ? "selected" : ""} ${p.support === "planned" && !sqlOnly ? "disabled" : ""}>${esc(p.name)}${p.support === "planned" ? (sqlOnly ? " - SQL scripts only (run manually)" : " (planned)") : p.support === "demo" ? " (demo)" : ""}</option>`).join("");
  const conn = env.connections || {};
  const st = side => conn[side] ? `<span class="dot" style="background:${conn[side].ok ? "var(--pass)" : "var(--fail)"}"></span>${conn[side].ok ? "Connected" : "Failed"} · <span style="color:var(--muted)">${esc(conn[side].message)}</span>` : `<span class="dot" style="background:var(--ne)"></span><span style="color:var(--muted)">Not tested</span>`;
  $("#view").innerHTML = `<div class="page-h"><div><h3>Settings</h3><p>Where the dashboard values (BI) and the expected values (data) come from.</p></div><button class="btn btn-ghost" id="tc">${ico("plug")} Test connections</button></div>
  <div class="grid-2" style="grid-template-columns:1fr 1fr">
    <div class="card" style="padding:18px"><h4 style="margin:0 0 4px">BI platform</h4><div style="font-size:13px;margin-bottom:10px">${st("bi")}</div><div class="form">
      <label>Platform<select id="biP">${opt(biOpts, env.bi.platform)}</select></label><label>Tenant ID<input id="biT" value="${esc(bi.tenant_id || "")}"></label>
      <label>Client (app) ID<input id="biC" value="${esc(bi.client_id || "")}"></label><label>Client secret<input id="biS" type="password" autocomplete="new-password" placeholder="${bi.client_secret ? "saved - leave blank to keep" : "paste the secret"}"></label>
      <label>Workspace ID<input id="biW" value="${esc(bi.workspace_id || "")}"></label><label>Semantic model (dataset) ID<input id="biD" value="${esc(bi.dataset_id || "")}"></label></div>
      <div style="font-size:12px;color:var(--muted);margin-top:8px">Needs tenant setting <i>Dataset Execute Queries REST API</i> and Build permission for the service principal.</div></div>
    <div class="card" style="padding:18px"><h4 style="margin:0 0 4px">Data platform (expected values)</h4><div style="font-size:13px;margin-bottom:10px">${st("data")}</div><div class="form">
      <label>Platform / SQL dialect<select id="daP">${opt(dataOpts, env.data.platform, true)}</select></label><label class="db-f">Server hostname<input id="daH" value="${esc(da.server_hostname || "")}" placeholder="adb-123.4.azuredatabricks.net"></label>
      <label class="full db-f">HTTP path<input id="daPath" value="${esc(da.http_path || "")}" placeholder="/sql/1.0/warehouses/1a2b3c4d5e6f7a8b"></label>
      <label class="db-f">Sign-in method<select id="daAuth"><option value="sp" ${da.access_token && !da.client_id ? "" : "selected"}>Service principal (client ID + secret)</option><option value="pat" ${da.access_token && !da.client_id ? "selected" : ""}>Personal access token</option></select></label>
      <label class="sp-f db-f">Client (application) ID<input id="daCid" value="${esc(da.client_id || "")}" autocomplete="off"></label>
      <label class="sp-f db-f">Client secret<input id="daSec" type="password" autocomplete="new-password" placeholder="${da.client_secret ? "saved - leave blank to keep" : "paste the secret"}"></label>
      <label class="sp-f db-f">Tenant ID <span style="color:var(--muted)">(only if the secret was created in Azure)</span><input id="daTen" value="${esc(da.tenant_id || "")}" placeholder="leave blank for a Databricks-created secret"></label>
      <label class="pat-f db-f">Access token<input id="daTok" type="password" autocomplete="new-password" placeholder="${da.access_token ? "saved - leave blank to keep" : "dapi..."}"></label>
      <label class="db-f">Default catalog<input id="daCat" value="${esc(da.catalog || "")}"></label></div>
      <div class="form gen-f" id="genF" style="margin-top:4px"></div>
      <div class="file-f" style="margin-top:8px"><label style="display:block">Folder path with the files (optional)<input id="daFolder" value="${esc(env.data.config?.source_folder || "")}" placeholder="C:\\Users\\you\\OneDrive - Company\\Sales Reports" style="width:100%"></label>
        <div style="font-size:12.5px;color:var(--ink-2);margin-top:6px">Values are computed from the dashboard's own Excel/CSV files - no server, HTTP path or sign-in needed. Either <b>upload</b> them under <b>Source files</b> below, or enter a <b>folder</b> on this computer that holds them (e.g. a SharePoint library synced with OneDrive - read fresh on every run; separate several folders with ;). Files at the path written in the Power Query are found automatically.</div></div><div class="db-f" style="font-size:12px;color:var(--muted);margin-top:8px">Secrets are stored in this computer's credential vault (Windows Credential Manager) and never shown again. Read-only: only single SELECT statements are run. The identity needs CAN USE on the warehouse and SELECT on the tables.</div><div class="gen-f" style="font-size:12px;color:var(--muted);margin-top:8px">Passwords and secrets go to this computer's credential vault (Windows Credential Manager) and are never shown again. Read-only: only single SELECT statements are run (sessions are opened read-only where the database supports it). A read-only login with SELECT on the dashboard's tables is enough.</div></div></div>
  <div id="srcboxS" style="margin-bottom:16px"></div>
  <div class="card" style="padding:18px;margin-bottom:16px"><h4 style="margin:0 0 10px">Test context</h4><div class="form" style="grid-template-columns:1fr 1fr 2fr">
    <label>Period label<input id="cxL" value="${esc(env.default_context.label)}"></label><label>Relative days (blank = all data)<input id="cxD" value="${env.default_context.relative_days ?? ""}"></label>
    <div style="font-size:12.5px;color:var(--muted);align-self:end">Applied to every KPI that has a date relationship, identically in SQL and DAX. Regenerate tests after changing.</div></div>
    <div style="margin-top:12px"><button class="btn btn-primary" id="save">Save settings</button></div></div>
  <details class="card more"><summary>${ico("gear")} Advanced<span class="sp"></span><span class="muted-s" style="margin:0">session, AI, connector support, audit log</span></summary><div class="panel-b">
  <div class="grid-2" style="grid-template-columns:1fr 1fr"><div class="card" style="padding:18px"><h4 style="margin:0 0 10px">Session & AI</h4><dl class="kv"><dt>User</dt><dd>${esc(se.user)} (${esc(se.role)})</dd><dt>Auth</dt><dd>${esc(se.auth_mode)}</dd><dt>AI</dt><dd>${se.ai.enabled ? "Enabled · " + esc(se.ai.provider) : "Not configured (rule engine)"}</dd><dt>AI policy</dt><dd>${esc(se.ai.policy)}</dd></dl></div>
  <div class="card" style="padding:18px"><h4 style="margin:0 0 10px">Connector support</h4><div style="max-height:220px;overflow:auto">${S.platforms.map(p => `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed #edf0f5;font-size:13px"><span>${esc(p.name)}</span><span class="badge b-${p.support}">${p.support}</span></div>`).join("")}</div></div></div>
  <div class="card" style="margin-top:16px"><div class="panel-h">${ico("clock")} Audit log</div><table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th></tr></thead><tbody>${audit.slice(0, 40).map(l => `<tr><td>${when(l.at)}</td><td>${esc(l.actor)}</td><td class="mono">${esc(l.action)}</td><td class="mono">${esc(l.target)}</td></tr>`).join("")}</tbody></table></div></div></details>`;
  const authSwap = () => { const sp = $("#daAuth").value === "sp"; document.querySelectorAll(".sp-f").forEach(e => e.style.display = sp ? "" : "none"); document.querySelectorAll(".pat-f").forEach(e => e.style.display = sp ? "none" : ""); };
  const genSpec = () => (S.platforms.find(p => p.key === $("#daP").value) || {}).fields;
  const renderGen = () => { const fs = genSpec(); const box = $("#genF"); if (!fs) { box.innerHTML = ""; return; }
    const keep = $("#daP").value === env.data.platform ? da : {};
    box.innerHTML = fs.map(f => { const v = keep[f.key]; const saved = v && /secret|password|passphrase|token/i.test(f.key);
      const inp = f.kind === "select" ? `<select data-k="${f.key}">${f.options.map(o => `<option value="${esc(o.value)}" ${String(v ?? "") === o.value ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`
        : `<input data-k="${f.key}" ${f.kind === "password" ? 'type="password" autocomplete="new-password"' : 'autocomplete="off"'} value="${saved ? "" : esc(v ?? "")}" placeholder="${esc(saved ? "saved - leave blank to keep" : f.placeholder || "")}">`;
      return `<label class="${f.key === "catalog" || f.key === "server" || f.key === "account" ? "full" : ""}" data-show='${esc(JSON.stringify(f.show_if || null))}'><span>${esc(f.label)}${f.required ? ' <span style="color:var(--fail)">*</span>' : ""}</span>${inp}${f.help ? `<span style="color:var(--muted);font-size:11.5px">${esc(f.help)}</span>` : ""}</label>`; }).join("");
    const vis = () => box.querySelectorAll("label[data-show]").forEach(l => { const c = JSON.parse(l.dataset.show || "null"); if (!c) return;
      const [k, vals] = Object.entries(c)[0]; const sel = box.querySelector(`[data-k="${k}"]`); l.style.display = sel && vals.includes(sel.value) ? "" : "none"; });
    box.querySelectorAll("select").forEach(x => x.onchange = vis); vis(); };
  const platSwap = () => { const v = $("#daP").value, gen = !!genSpec(), f = v === "files" || v === "demo_sql" || gen;
    document.querySelectorAll(".db-f").forEach(e => e.style.display = f ? "none" : ""); document.querySelectorAll(".file-f").forEach(e => e.style.display = v === "files" ? "" : "none"); document.querySelectorAll(".gen-f").forEach(e => e.style.display = gen ? "" : "none");
    renderGen(); if (!f) authSwap(); };
  $("#daAuth").onchange = authSwap; authSwap(); $("#daP").onchange = platSwap; platSwap(); renderSourceFiles("#srcboxS");
  $("#save").onclick = async () => {
    const patch = {bi: {platform: $("#biP").value, config: {tenant_id: $("#biT").value, client_id: $("#biC").value, client_secret: $("#biS").value, workspace_id: $("#biW").value, dataset_id: $("#biD").value}},
      data: genSpec() ? (() => { const cfg = {}; document.querySelectorAll("#genF [data-k]").forEach(x => { const l = x.closest("label"); if (l.style.display !== "none" || x.tagName === "SELECT") cfg[x.dataset.k] = x.value; });
          const keys = genSpec().map(f => f.key); return {platform: $("#daP").value, config: cfg, clear: Object.keys(da).filter(k => !keys.includes(k) && k !== "source_folder")}; })()
        : $("#daP").value === "files" ? {platform: "files", config: {source_folder: $("#daFolder").value.trim()}}
        : $("#daP").value === "demo_sql" ? {platform: "demo_sql", config: {}}
        : $("#daAuth").value === "sp"
        ? {platform: $("#daP").value, config: {server_hostname: $("#daH").value, http_path: $("#daPath").value, client_id: $("#daCid").value, client_secret: $("#daSec").value, tenant_id: $("#daTen").value, catalog: $("#daCat").value}, clear: ["access_token"]}
        : {platform: $("#daP").value, config: {server_hostname: $("#daH").value, http_path: $("#daPath").value, access_token: $("#daTok").value, catalog: $("#daCat").value}, clear: ["client_id", "client_secret", "tenant_id"]},
      default_context: {label: $("#cxL").value || "All data", relative_days: $("#cxD").value ? +$("#cxD").value : null}};
    try { await api(E("/settings"), {method: "PATCH", body: patch}); S.envs = await api("/api/environments");
      toast("Saved - testing connections…"); await api(E("/test-connection"), {method: "POST"}); S.envs = await api("/api/environments"); route(); } catch (e) { if (e.message !== "snapshot") toast(e.message.replace(/^\d+: /, "")); }
  };
  $("#tc").onclick = async e => { e.currentTarget.disabled = true; try { await api(E("/test-connection"), {method: "POST"}); S.envs = await api("/api/environments"); } catch (x) { if (x.message !== "snapshot") toast(x.message); } route(); };
};

// ------------------------------------------------------------------ router / boot
const TITLES = {overview: "Dashboard Assurance", dax: "DAX Analysis", usage: "Model Usage", tests: "Test Cases", execution: "Test Execution", validation: "Data Validation", reports: "Reports", settings: "Settings", project: "Project"};

function fillEnvSelect() {
  $("#envSelect").innerHTML = S.envs.length ? S.envs.map(e => `<option value="${e.id}" ${e.id === S.env ? "selected" : ""}>${esc(e.name)}</option>`).join("") : '<option>No project yet</option>';
}

// Re-render the current page in place (after an action in the side panel): keeps page number, filters,
// expanded rows and scroll position, and leaves the side panel open.
async function softRefresh() {
  const sc = [document.scrollingElement, $(".main"), $("#view")].filter(Boolean).map(e => [e, e.scrollTop]);
  await route({soft: true});
  sc.forEach(([e, y]) => { e.scrollTop = y; });
}
const ui = key => { S.ui = S.ui || {}; const k = `${S.env}|${key}`; return (S.ui[k] = S.ui[k] || {}); };

async function route(opts = {}) {
  S.route = (location.hash || "#overview").slice(1);
  if (!PAGES[S.route]) S.route = "overview";
  if (!S.env) S.route = "project";
  renderNav();
  const env = curEnv();
  $("#brandBi").textContent = S.session.app_name || "VerityIQ";
  $("#pageTitle").textContent = `${env ? biShort(env) + " " : ""}${TITLES[S.route]}`;
  document.title = `${S.session.app_name || "VerityIQ"} · ${TITLES[S.route]}`;
  $("#ctxChip").innerHTML = `${ico("calendar")} ${esc(env?.default_context.label || "All data")} ${ico("chevron")}`;
  $("#modeBanner").innerHTML = (SNAP ? `<div class="mode-banner">${ico("alert")}<span><b>Static snapshot</b> of the sample project (${esc(SNAP.__captured__ || "")}). Read-only - run the app locally to upload your own PBIP.</span></div>`
    : env && env.mode === "DEMO" ? `<div class="mode-banner">${ico("alert")}<span><b>Sample project</b> · demo data - results are labelled DEMO</span></div>` : "");
  if (!opts.soft) { $("#view").innerHTML = '<div class="empty">Loading…</div>'; closeDrawer(); }
  try { await PAGES[S.route](); } catch (e) { $("#view").innerHTML = `<div class="card empty">Could not load: ${esc(e.message)}</div>`; }
}

async function boot() {
  [S.session, S.platforms, S.envs] = await Promise.all([api("/api/session"), api("/api/platforms"), api("/api/environments")]);
  S.env = (SNAP && SNAP.__env__) || S.envs[S.envs.length - 1]?.id || null;
  fillEnvSelect();
  $("#envSelect").onchange = e => { if (SNAP) { toast("Snapshot contains one project only."); e.target.value = S.env; return; } S.env = e.target.value; closeDrawer(); route(); };
  $("#avatar").textContent = S.session.user.split(/[-.@ ]/).filter(Boolean).slice(0, 2).map(x => x[0].toUpperCase()).join("");
  $("#avatar").title = `${S.session.user} (${S.session.role})`;
  $("#brandTag").textContent = S.session.app_tagline || "Universal BI & Data Quality Assurance";
  $("#aiCard").innerHTML = `${ico("spark").replace("<svg", '<svg style="width:24px;height:24px;color:#7fb0ff"')}<b>Powered by AI</b>${S.session.ai.enabled ? "LLM explanations on - metadata only, labelled as suggestions." : "Rule engine active. Add an LLM key for AI explanations."}<a href="#settings" class="learn">Learn More ${ico("chevron").replace("<svg", '<svg style="width:12px"')}</a>`;
  window.addEventListener("hashchange", route);
  route();
}
boot();
