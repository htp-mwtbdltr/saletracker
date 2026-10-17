"use strict";
// ---------- helpers ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const view = $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const img = (u) => (u ? "/api/img?u=" + encodeURIComponent(u) : "");
const money = (v) => (v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const SITE = { microcenter: ["Micro Center", "mc"], amazon: ["Amazon", "amz"] };
const COLORS = {
  price: "#4f6bed", new: "#4f6bed", amazon: "#e88b00", open_box: "#12a150", clearance: "#d93a3a",
  used: "#12a150", used_like_new: "#0fb5ae", used_very_good: "#8b5cf6", used_good: "#d946ef",
  used_acceptable: "#94a3b8", renewed: "#64748b",
};
const ALERT_TYPES = { below: "Price at or below", drop_pct: "Drops by at least %", all_time_low: "New all-time low" };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || `HTTP ${r.status}`);
  return data;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 0) return "soon";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
const fmtDate = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? 6000 : 3000);
}

const lowest = (prices) => {
  const v = Object.values(prices || {}).filter((x) => typeof x === "number");
  return v.length ? Math.min(...v) : null;
};

function statusPill(t) {
  if (!t.enabled) return `<span class="pill">⏸ Paused</span>`;
  if (t.fail_count) return `<span class="pill bad" title="${esc(t.last_error)}"><span class="dot"></span>Failing ×${t.fail_count}</span>`;
  if (!t.last_ok) return `<span class="pill warn"><span class="dot"></span>Pending</span>`;
  return `<span class="pill good"><span class="dot"></span>${ago(t.last_ok)}</span>`;
}
const sitePill = (site) => `<span class="pill ${SITE[site][1]}">${SITE[site][0]}</span>`;
const kindPill = (kind) => `<span class="pill">${kind === "search" ? "🔎 Search" : "📦 Product"}</span>`;

const modal = $("#modal");
function openModal(html) {
  $("#modalBody").innerHTML = html;
  if (!modal.open) modal.showModal();
}
function closeModal() { modal.close(); }
modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });

async function busy(btn, fn) {
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> ${label}`;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.innerHTML = label; }
}

// ---------- router ----------
let refreshTimer;
async function route() {
  clearInterval(refreshTimer);
  if (modal.open) closeModal();
  charts.forEach((c) => c.destroy());
  charts.length = 0;
  const h = location.hash || "#/";
  $$("[data-nav]").forEach((a) => a.classList.toggle("active",
    (h === "#/" && a.dataset.nav === "home") || h.startsWith("#/" + a.dataset.nav)));
  try {
    let m;
    if ((m = h.match(/^#\/t\/(\d+)/))) await renderTracker(+m[1]);
    else if (h.startsWith("#/activity")) await renderActivity();
    else if (h.startsWith("#/settings")) await renderSettings();
    else await renderHome();
  } catch (e) {
    view.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

async function updateBotStatus() {
  try {
    const s = await api("/api/status");
    const el = $("#botStatus");
    if (!s.bot.configured) { el.className = "pill warn"; el.innerHTML = `<span class="dot"></span>No bot token`; }
    else if (s.bot.ready) { el.className = "pill good"; el.innerHTML = `<span class="dot"></span>${esc(s.bot.user)}`; }
    else { el.className = "pill bad"; el.innerHTML = `<span class="dot"></span>Bot offline`; }
    return s;
  } catch { return null; }
}

// ---------- home ----------
let homeFilter = "all";
async function renderHome() {
  const trackers = await api("/api/trackers");
  const shown = trackers.filter((t) => homeFilter === "all" || t.kind === homeFilter || t.site === homeFilter);
  view.innerHTML = `
    <div class="toolbar">
      <h1>Trackers</h1>
      <span class="spacer"></span>
      <div class="seg">${[["all", "All"], ["product", "Products"], ["search", "Searches"], ["microcenter", "Micro Center"], ["amazon", "Amazon"]]
        .map(([k, l]) => `<button data-f="${k}" class="${homeFilter === k ? "on" : ""}">${l}</button>`).join("")}</div>
      <button class="primary" id="addBtn">＋ Add tracker</button>
    </div>
    ${shown.length ? `<div class="cards">${shown.map(card).join("")}</div>`
      : `<div class="empty"><p>No trackers${trackers.length ? " match this filter" : " yet"}.</p>
         <p>Paste a Micro Center or Amazon search or product URL to start.</p></div>`}`;
  $$("[data-f]").forEach((b) => b.onclick = () => { homeFilter = b.dataset.f; renderHome(); });
  $("#addBtn").onclick = () => addDialog();
  $$(".card").forEach((c) => c.onclick = () => (location.hash = `#/t/${c.dataset.id}`));
  refreshTimer = setInterval(() => { if (!modal.open) renderHome(); }, 60000);
}

function card(t) {
  const labels = t.series || {};
  if (t.kind === "product") {
    const it = t.item;
    const prices = it ? Object.entries(it.prices).filter(([, v]) => v != null) : [];
    const best = it ? lowest(it.prices) : null;
    const lowAll = it ? lowest(Object.fromEntries(Object.entries(it.lows || {}).map(([k, v]) => [k, v.low]))) : null;
    return `<div class="card" data-id="${t.id}">
      <div class="head">
        ${it && it.image ? `<img class="thumb" src="${img(it.image)}" alt="" loading="lazy">` : ""}
        <div style="min-width:0">
          <div class="title">${esc(it ? it.name : t.name)}</div>
          <div class="meta">${sitePill(t.site)}${kindPill(t.kind)}${t.alert_count ? `<span class="pill">🔔 ${t.alert_count}</span>` : ""}</div>
        </div>
      </div>
      <div><span class="big">${money(best)}</span> ${lowAll != null && best != null && lowAll < best ? `<span class="muted">· low ${money(lowAll)}</span>` : ""}</div>
      <div class="rows">${prices.map(([k, v]) => `<span>${esc(labels[k] || k)}</span><span>${money(v)}</span>`).join("")}
        ${it && it.extra && it.extra.stock != null ? `<span>Stock</span><span>${esc(it.extra.stock)}</span>` : ""}</div>
      <div class="foot"><span>#${t.id} · every ${t.interval_effective}m</span>${statusPill(t)}</div>
    </div>`;
  }
  return `<div class="card" data-id="${t.id}">
    <div class="head"><div style="min-width:0">
      <div class="title">${esc(t.name)}</div>
      <div class="meta">${sitePill(t.site)}${kindPill(t.kind)}${t.alert_count ? `<span class="pill">🔔 ${t.alert_count}</span>` : ""}
        ${t.new_24h ? `<span class="pill good">${t.new_24h} new today</span>` : ""}</div>
    </div></div>
    <div><span class="big">${t.item_count}</span> <span class="muted">items · from ${money(t.cheapest)}</span></div>
    <div class="thumbs">${(t.thumbs || []).map((s) => `<img src="${img(s)}" alt="" loading="lazy">`).join("")}</div>
    <div class="foot"><span>#${t.id} · every ${t.interval_effective}m</span>${statusPill(t)}</div>
  </div>`;
}

// ---------- add tracker ----------
function optionsFields(site, kind, opts = {}) {
  const chk = (k, label, def) => `<label class="check"><input type="checkbox" data-opt="${k}" ${(opts[k] ?? def) ? "checked" : ""}> ${label}</label>`;
  const num = (k, label, ph, hint = "") => `<label class="field">${label}<input type="number" step="any" data-opt="${k}" value="${esc(opts[k] ?? "")}" placeholder="${esc(ph)}">${hint ? `<span class="hint">${hint}</span>` : ""}</label>`;
  const txt = (k, label, ph) => `<label class="field">${label}<input data-opt="${k}" value="${esc(opts[k] ?? "")}" placeholder="${esc(ph)}"></label>`;
  let html = "";
  if (kind === "search") {
    html += `<div class="grid-form">
      ${chk("notify_new", "Notify on new items", true)}
      ${chk("notify_returning", "Notify when an item comes back", true)}
      ${chk("notify_price_drop", "Notify on any price drop", false)}
      ${chk("notify_first_run", "Notify for items found on first check", false)}
    </div><div class="grid-form" style="margin-top:12px">
      ${num("max_price", "Only notify new items under $", "no limit")}
      ${txt("include_keywords", "Must contain one of (comma sep.)", "e.g. oled, 4k")}
      ${txt("exclude_keywords", "Ignore items containing", "e.g. refurbished, curved")}
      ${num("max_pages", "Max result pages", site === "amazon" ? "2" : "5")}
      ${num("return_after_hours", "“Came back” after missing for (hours)", "12")}
    </div>`;
  } else {
    html += `<div class="grid-form">${chk("notify_stock", "Notify when back in stock", true)}
      ${site === "amazon" ? chk("include_shipping", "Include shipping in prices", false) : ""}</div>`;
  }
  if (site === "microcenter") {
    html += `<div class="grid-form" style="margin-top:12px">
      ${num("store_id", "Store ID", "default from settings", "141 = Columbus")}
      ${kind === "search" ? chk("in_stock_only", "Only items in stock at my store", true) : ""}</div>`;
  }
  if (site === "amazon" && kind === "search") {
    html += `<div class="grid-form" style="margin-top:12px">${chk("skip_sponsored", "Skip sponsored results", true)}</div>`;
  }
  return html;
}

function readOptions(root) {
  const o = {};
  $$("[data-opt]", root).forEach((el) => {
    const k = el.dataset.opt;
    if (el.type === "checkbox") o[k] = el.checked;
    else if (el.type === "number") o[k] = el.value === "" ? null : Number(el.value);
    else o[k] = el.value.trim();
  });
  return o;
}

function seriesOptions(series, selected = "lowest") {
  return Object.entries(series).map(([k, v]) => `<option value="${k}" ${k === selected ? "selected" : ""}>${esc(v)}</option>`).join("");
}

function previewHtml(p) {
  const items = p.items || [];
  let body = "";
  if (p.kind === "product" && items[0]) {
    const it = items[0];
    body = `<div class="detail-head" style="margin:0">
      ${it.image ? `<img src="${img(it.image)}" alt="">` : ""}
      <div class="info"><b>${esc(it.name)}</b>
        <div class="price-tiles">${Object.entries(it.prices).filter(([, v]) => v != null)
          .map(([k, v]) => `<div class="tile"><div class="label">${esc(p.series[k] || k)}</div><div class="val">${money(v)}</div></div>`).join("") || `<span class="muted">No prices found</span>`}</div>
        ${it.extra.stock != null ? `<span class="hint">Stock: ${esc(it.extra.stock)} ${esc(it.extra.store || "")}</span>` : ""}
        ${it.extra.availability ? `<span class="hint">${esc(it.extra.availability)}</span>` : ""}
      </div></div>
      ${it.offers && it.offers.length ? `<div class="hint" style="margin-top:8px">${it.offers.length} offers found, e.g. ${it.offers.slice(0, 3).map((o) => `${esc(o.condition)} ${money(o.price)} (${esc(o.seller || "?")})`).join(" · ")}</div>` : ""}`;
  } else {
    body = `<div class="hint" style="margin-bottom:6px">${items.length} items found${p.total_count != null ? ` (site reports ${p.total_count})` : ""} across ${p.pages} page(s)</div>
      <div class="preview-list">${items.map((i) => `<div class="row">${i.image ? `<img src="${img(i.image)}" alt="" loading="lazy">` : ""}
        <span class="n" title="${esc(i.name)}">${esc(i.name)}</span><b>${money(lowest(i.prices))}</b></div>`).join("") || `<div class="row muted">No items right now; you'll be notified when some appear.</div>`}</div>`;
  }
  return `<div class="panel" style="margin:12px 0 0">
    <div class="meta" style="display:flex;gap:6px;margin-bottom:10px">${sitePill(p.site)}${kindPill(p.kind)}</div>${body}</div>`;
}

function addDialog(prefillUrl = "") {
  openModal(`
    <div class="modal-head"><h2 style="margin:0">Add tracker</h2><button class="small" id="mClose">✕</button></div>
    <div style="display:flex;gap:8px">
      <input id="aUrl" placeholder="https://www.microcenter.com/… or https://www.amazon.com/dp/…" value="${esc(prefillUrl)}">
      <button class="primary" id="aPreview">Preview</button>
    </div>
    <div class="hint" style="margin-top:6px">Search/category result pages notify you about new listings. Product pages track price history and alert on price levels.</div>
    <div id="aPrev"></div>
    <div id="aForm"></div>`);
  $("#mClose").onclick = closeModal;
  const go = () => busy($("#aPreview"), async () => {
    const url = $("#aUrl").value.trim();
    if (!url) return;
    $("#aPrev").innerHTML = `<p class="muted"><span class="spinner"></span> Loading page…</p>`;
    let p;
    try {
      p = await api("/api/preview", { method: "POST", body: { url } });
      $("#aPrev").innerHTML = previewHtml(p);
    } catch (e) {
      $("#aPrev").innerHTML = `<div class="err" style="margin-top:12px">${esc(e.message)}<br>You can still add it; the tracker will keep retrying.</div>`;
      p = null;
    }
    addForm(url, p);
  });
  $("#aPreview").onclick = go;
  $("#aUrl").onkeydown = (e) => { if (e.key === "Enter") go(); };
  $("#aUrl").focus();
  if (prefillUrl) go();
}

async function addForm(url, p) {
  let site = p && p.site, kind = p && p.kind, series = p && p.series;
  if (!p) {
    // Guess from the URL so options still make sense.
    site = /amazon|amzn|a\.co/.test(url) ? "amazon" : "microcenter";
    kind = /\/dp\/|\/gp\/product|\/product\//.test(url) ? "product" : "search";
    series = { lowest: "Lowest of any" };
  }
  $("#aForm").innerHTML = `
    <div class="grid-form" style="margin-top:14px">
      <label class="field">Name<input id="aName" value="${esc(p ? p.title : "")}" placeholder="Display name"></label>
      <label class="field">Check every (minutes)<input id="aInt" type="number" min="2" placeholder="default"></label>
    </div>
    <fieldset><legend>Price alert (optional)</legend>
      <div class="alert-form" style="margin-top:0">
        <label class="field">When<select id="aType">${Object.entries(ALERT_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
        <label class="field">Price type<select id="aSeries">${seriesOptions(series)}</select></label>
        <label class="field">Value<input id="aVal" type="number" step="any" placeholder="e.g. 130"></label>
      </div>
      ${kind === "search" ? `<div class="hint" style="margin-top:6px">On a search, the alert applies to every item in the results.</div>` : ""}
    </fieldset>
    <fieldset><legend>Options</legend>${optionsFields(site, kind)}</fieldset>
    <div class="modal-actions"><button id="aCancel">Cancel</button><button class="primary" id="aSave">Start tracking</button></div>`;
  $("#aCancel").onclick = closeModal;
  $("#aSave").onclick = () => busy($("#aSave"), async () => {
    const alerts = [];
    const type = $("#aType").value, val = $("#aVal").value;
    if (type === "all_time_low" || val) alerts.push({ type, series: $("#aSeries").value, value: val ? Number(val) : null });
    const t = await api("/api/trackers", { method: "POST", body: {
      url: p ? p.url : url, name: $("#aName").value.trim(),
      interval_min: $("#aInt").value ? Number($("#aInt").value) : null,
      options: readOptions($("#aForm")), alerts } });
    closeModal();
    toast("Tracker added");
    location.hash = `#/t/${t.id}`;
  });
}

// ---------- charts ----------
const charts = [];
function seriesColor(k, i) { return COLORS[k] || ["#4f6bed", "#12a150", "#e88b00", "#d946ef", "#0fb5ae", "#d93a3a"][i % 6]; }

function drawChart(canvas, history, labels, hidden = new Set()) {
  const bySeries = {};
  for (const h of history) (bySeries[h.series] ||= []).push({ x: h.ts * 1000, y: h.price });
  const now = Date.now();
  const datasets = Object.entries(bySeries).map(([k, pts], i) => {
    const last = pts[pts.length - 1];
    if (last && last.y != null) pts.push({ x: now, y: last.y });
    const c = seriesColor(k, i);
    return { label: labels[k] || k, key: k, data: pts, borderColor: c, backgroundColor: c, stepped: "after",
      pointRadius: pts.length > 60 ? 0 : 2, borderWidth: 2, spanGaps: false, hidden: hidden.has(k) };
  });
  const xs = datasets.flatMap((d) => d.data.map((p) => p.x));
  const shortRange = xs.length && Math.max(...xs) - Math.min(...xs) < 2 * 86400000;
  const tickFmt = shortRange ? { hour: "numeric", minute: "2-digit" } : { month: "short", day: "numeric" };
  const css = getComputedStyle(document.documentElement);
  const grid = css.getPropertyValue("--border").trim(), muted = css.getPropertyValue("--muted").trim();
  const chart = new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: {
        x: { type: "linear", grid: { color: grid }, ticks: { color: muted, maxTicksLimit: 8,
          callback: (v) => shortRange ? new Date(v).toLocaleTimeString(undefined, tickFmt) : new Date(v).toLocaleDateString(undefined, tickFmt) } },
        y: { grid: { color: grid }, ticks: { color: muted, callback: (v) => "$" + v } },
      },
      plugins: {
        legend: { labels: { color: muted, boxWidth: 12 } },
        tooltip: { callbacks: {
          title: (items) => new Date(items[0].parsed.x).toLocaleString(),
          label: (c) => `${c.dataset.label}: ${money(c.parsed.y)}` } },
      },
    },
  });
  charts.push(chart);
  return chart;
}

async function chartPanel(container, itemId, labels, days = 30) {
  container.innerHTML = `
    <div class="panel-head"><h2>Price history</h2>
      <div class="seg">${[[7, "7d"], [30, "30d"], [90, "90d"], [0, "All"]].map(([d, l]) => `<button data-days="${d}" class="${d === days ? "on" : ""}">${l}</button>`).join("")}</div>
    </div>
    <div class="chart-wrap"><canvas></canvas></div>`;
  const data = await api(`/api/items/${itemId}?days=${days}`);
  const canvas = $("canvas", container);
  if (!data.history.length) {
    $(".chart-wrap", container).innerHTML = `<div class="empty">No history yet. It builds up as the tracker runs.</div>`;
  } else {
    drawChart(canvas, data.history, labels);
  }
  $$("[data-days]", container).forEach((b) => b.onclick = () => chartPanel(container, itemId, labels, +b.dataset.days));
  return data;
}

// ---------- tracker detail ----------
let itemSort = { key: "lowest", dir: 1 }, itemFilter = "", showGone = false;

async function renderTracker(id) {
  const t = await api(`/api/trackers/${id}`);
  const labels = t.series;
  const product = t.kind === "product";
  const it = product ? t.items[0] : null;
  view.innerHTML = `
    <div class="detail-head">
      ${it && it.image ? `<img src="${img(it.image)}" alt="">` : ""}
      <div class="info">
        <h1>${esc(it ? it.name : t.name)}</h1>
        <div class="meta" style="display:flex;gap:6px;flex-wrap:wrap">${sitePill(t.site)}${kindPill(t.kind)}${statusPill(t)}
          <span class="pill">every ${t.interval_effective}m</span>
          ${it && it.extra.stock != null ? `<span class="pill">${esc(it.extra.stock)} in stock ${esc(it.extra.store || "")}</span>` : ""}
          ${it && it.extra.availability && t.site === "amazon" ? `<span class="pill">${esc(it.extra.availability)}</span>` : ""}
        </div>
        <a href="${esc(t.url)}" target="_blank" rel="noopener" class="hint" style="word-break:break-all">${esc(t.url)}</a>
        ${t.last_error ? `<div class="err">Last check failed ${ago(t.last_checked)}: ${esc(t.last_error)}</div>` : ""}
      </div>
      <div class="actions">
        <button id="tCheck">↻ Check now</button>
        <button id="tToggle">${t.enabled ? "⏸ Pause" : "▶ Resume"}</button>
        <button class="danger" id="tDelete">Delete</button>
      </div>
    </div>
    ${product ? `
      <div class="panel"><div class="panel-head"><h2>Current prices</h2><span class="hint">checked ${ago(t.last_checked)}</span></div>
        <div class="price-tiles">${Object.entries(it ? it.prices : {}).filter(([k, v]) => v != null || t.item.lows[k]).map(([k, v]) => {
          const lo = (t.item.lows[k] || {}).low, hi = (t.item.lows[k] || {}).high;
          return `<div class="tile ${v == null ? "off" : ""}"><div class="label"><span class="swatch" style="background:${seriesColor(k, 0)}"></span>${esc(labels[k] || k)}</div>
            <div class="val">${money(v)}</div><div class="sub">${lo != null ? `low ${money(lo)} · high ${money(hi)}` : "no history"}</div></div>`;
        }).join("") || `<span class="muted">Waiting for the first check…</span>`}</div>
      </div>
      <div class="panel" id="chartPanel"></div>
      ${t.site === "amazon" && it && it.extra.offers ? offersPanel(it.extra) : ""}` : `
      <div class="panel" id="itemsPanel"></div>`}
    <div class="two-col">
      <div>
        <div class="panel" id="alertsPanel"></div>
        <div class="panel" id="settingsPanel"></div>
      </div>
      <div class="panel"><h2>Recent notifications</h2><div class="events">${eventsHtml(t.events) || `<span class="muted">None yet</span>`}</div></div>
    </div>`;

  $("#tCheck").onclick = () => busy($("#tCheck"), async () => {
    const r = await api(`/api/trackers/${id}/check`, { method: "POST" });
    toast(r.ok ? `Checked: ${r.items} item(s), ${r.notices} notification(s)` : `Check failed: ${r.error}`, !r.ok);
    renderTracker(id);
  });
  $("#tToggle").onclick = () => busy($("#tToggle"), async () => {
    await api(`/api/trackers/${id}`, { method: "PATCH", body: { enabled: !t.enabled } });
    renderTracker(id);
  });
  $("#tDelete").onclick = async () => {
    if (!confirm(`Delete "${t.name}" and all its price history?`)) return;
    await api(`/api/trackers/${id}`, { method: "DELETE" });
    toast("Deleted");
    location.hash = "#/";
  };

  if (product && it) chartPanel($("#chartPanel"), it.id, labels);
  else if (product) $("#chartPanel").innerHTML = `<h2>Price history</h2><div class="empty">Waiting for the first check…</div>`;
  else renderItems(t);
  renderAlerts(t);
  renderTrackerSettings(t);
  if (!t.last_checked) refreshTimer = setInterval(async () => {
    const x = await api(`/api/trackers/${id}`);
    if (x.last_checked) { clearInterval(refreshTimer); renderTracker(id); }
  }, 4000);
}

function offersPanel(extra) {
  const offers = extra.offers || [];
  return `<div class="panel"><div class="panel-head"><h2>Offers (${offers.length})</h2>
    <span class="hint">${extra.offer_source === "page" ? "From the product page (full offer list unavailable)" : "All sellers, cheapest first"}</span></div>
    <div class="table-scroll"><table><thead><tr><th>Condition</th><th class="num">Price</th><th class="num">Shipping</th><th>Seller</th><th>Ships from</th></tr></thead>
    <tbody>${offers.map((o) => `<tr><td>${esc(o.condition)}</td><td class="num"><b>${money(o.price)}</b></td>
      <td class="num">${o.shipping ? money(o.shipping) : "Free"}</td><td>${esc(o.seller)}${o.amazon ? " ✓" : ""}</td><td>${esc(o.ships_from)}</td></tr>`).join("")}</tbody></table></div></div>`;
}

function renderItems(t) {
  const el = $("#itemsPanel");
  const gone = t.items.filter((i) => !i.present).length;
  el.innerHTML = `
    <div class="panel-head"><h2>Items (${t.item_count})</h2>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input id="iFilter" placeholder="Filter…" value="${esc(itemFilter)}" style="width:180px">
        ${gone ? `<label class="check hint"><input type="checkbox" id="iGone" ${showGone ? "checked" : ""}> show ${gone} no longer listed</label>` : ""}
      </div></div>
    <div class="table-scroll"><table id="iTable"></table></div>`;
  $("#iFilter").oninput = (e) => { itemFilter = e.target.value.toLowerCase(); fillItems(t); };
  if ($("#iGone")) $("#iGone").onchange = (e) => { showGone = e.target.checked; fillItems(t); };
  fillItems(t);
}

function fillItems(t) {
  const table = $("#iTable");
  const labels = t.series;
  const seriesKeys = Object.keys(labels).filter((k) => k !== "lowest" && t.items.some((i) => i.prices[k] != null));
  const items = t.items.filter((i) => (showGone || i.present) && (!itemFilter || i.name.toLowerCase().includes(itemFilter)));
  const val = (i, k) => k === "name" ? i.name.toLowerCase() : k === "first_seen" ? -(i.first_seen || 0) : k === "lowest" ? i.lowest : k === "stock" ? i.extra.stock : i.prices[k];
  items.sort((a, b) => {
    const x = val(a, itemSort.key), y = val(b, itemSort.key);
    if (x == null) return 1;
    if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * itemSort.dir;
  });
  const th = (k, l, cls = "") => `<th class="${cls}" data-sort="${k}">${l}${itemSort.key === k ? (itemSort.dir > 0 ? " ▲" : " ▼") : ""}</th>`;
  const isNew = (i) => i.first_seen > Math.max(Date.now() / 1000 - 86400, t.created_at + 600);
  const mc = t.site === "microcenter";
  table.innerHTML = `
    <thead><tr><th></th>${th("name", "Name")}${th("lowest", "Lowest", "num")}${seriesKeys.map((k) => th(k, esc(labels[k]), "num")).join("")}
      ${mc ? th("stock", "Stock", "num") : ""}${th("first_seen", "First seen")}<th></th></tr></thead>
    <tbody>${items.map((i) => `<tr class="${i.present ? "" : "gone"} ${i.ignored ? "ignored" : ""}" data-iid="${i.id}">
      <td>${i.image ? `<img src="${img(i.image)}" alt="" loading="lazy">` : ""}</td>
      <td class="name-cell"><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.name)}</a> ${isNew(i) ? `<span class="pill good">new</span>` : ""}</td>
      <td class="num"><b>${money(i.lowest)}</b></td>
      ${seriesKeys.map((k) => `<td class="num">${money(i.prices[k])}</td>`).join("")}
      ${mc ? `<td class="num">${esc(i.extra.stock ?? "")}</td>` : ""}
      <td class="nowrap muted">${ago(i.first_seen)}</td>
      <td class="nowrap"><button class="small" data-act="hist" title="History, alerts and actions">📈 Details</button></td>
    </tr>`).join("") || `<tr><td colspan="${5 + seriesKeys.length + (mc ? 1 : 0)}" class="muted">No items${t.last_checked ? "" : " yet; waiting for the first check"}.</td></tr>`}</tbody>`;
  $$("[data-sort]", table).forEach((h) => h.onclick = () => {
    const k = h.dataset.sort;
    itemSort = { key: k, dir: itemSort.key === k ? -itemSort.dir : 1 };
    fillItems(t);
  });
  $$("[data-act=hist]", table).forEach((b) => b.onclick = () => itemDialog(t, +b.closest("tr").dataset.iid));
}

async function itemDialog(t, iid) {
  const it = t.items.find((i) => i.id === iid);
  openModal(`
    <div class="modal-head"><h2 style="margin:0">${esc(it.name)}</h2><button class="small" id="mClose">✕</button></div>
    <div class="detail-head" style="margin-bottom:10px">
      ${it.image ? `<img src="${img(it.image)}" alt="">` : ""}
      <div class="info">
        <div class="price-tiles">${Object.entries(it.prices).filter(([, v]) => v != null).map(([k, v]) =>
          `<div class="tile"><div class="label">${esc(t.series[k] || k)}</div><div class="val">${money(v)}</div></div>`).join("")}</div>
        <div class="hint">First seen ${fmtDate(it.first_seen)} · last seen ${ago(it.last_seen)}${it.extra.sku ? ` · SKU ${esc(it.extra.sku)}` : ""}</div>
      </div>
    </div>
    <div id="mChart"></div>
    <fieldset><legend>Alert for just this item</legend>
      <div class="alert-form" style="margin-top:0">
        <label class="field">When<select id="mType">${Object.entries(ALERT_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
        <label class="field">Price type<select id="mSeries">${seriesOptions(t.series)}</select></label>
        <label class="field">Value<input id="mVal" type="number" step="any"></label>
        <button id="mAddAlert">Add</button>
      </div>
    </fieldset>
    <div class="modal-actions">
      <a class="btn" href="${esc(it.url)}" target="_blank" rel="noopener">Open listing ↗</a>
      <button id="mIgnore">${it.ignored ? "Un-ignore" : "Ignore item"}</button>
      <button class="primary" id="mTrack">Track as its own product</button>
    </div>`);
  $("#mClose").onclick = closeModal;
  chartPanel($("#mChart"), iid, t.series);
  $("#mAddAlert").onclick = () => busy($("#mAddAlert"), async () => {
    const v = $("#mVal").value;
    await api(`/api/trackers/${t.id}/alerts`, { method: "POST", body: {
      type: $("#mType").value, series: $("#mSeries").value, value: v ? Number(v) : null, item_id: iid } });
    toast("Alert added");
    closeModal();
    renderTracker(t.id);
  });
  $("#mIgnore").onclick = () => busy($("#mIgnore"), async () => {
    await api(`/api/items/${iid}`, { method: "PATCH", body: { ignored: !it.ignored } });
    closeModal();
    renderTracker(t.id);
  });
  $("#mTrack").onclick = () => busy($("#mTrack"), async () => {
    const nt = await api(`/api/items/${iid}/track`, { method: "POST" });
    closeModal();
    toast("Now tracking as a product");
    location.hash = `#/t/${nt.id}`;
  });
}

function renderAlerts(t) {
  const el = $("#alertsPanel");
  el.innerHTML = `
    <h2>Alerts</h2>
    ${t.alerts.map((a) => `<div class="alert-row" data-aid="${a.id}">
      <input type="checkbox" data-act="toggle" ${a.enabled ? "checked" : ""} title="Enabled">
      <span class="txt">${esc(a.label.replace(/ \[off\]$/, ""))}${a.note ? ` <span class="hint">· ${esc(a.note)}</span>` : ""}</span>
      <button class="small danger" data-act="del">Remove</button></div>`).join("")
      || `<p class="muted">No alerts yet. ${t.kind === "search" ? "New items are still announced (see options)." : "Add one to get notified at your price."}</p>`}
    <div class="alert-form">
      <label class="field">When<select id="alType">${Object.entries(ALERT_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
      <label class="field">Price type<select id="alSeries">${seriesOptions(t.series)}</select></label>
      <label class="field">Value<input id="alVal" type="number" step="any" placeholder="e.g. 130"></label>
      <button class="primary" id="alAdd">Add alert</button>
    </div>
    <div class="hint" style="margin-top:6px">“At or below” alerts fire once when the price reaches your level, again if it drops further, and re-arm after it goes back above.</div>`;
  $("#alType").onchange = () => { $("#alVal").disabled = $("#alType").value === "all_time_low"; $("#alVal").placeholder = $("#alType").value === "drop_pct" ? "percent, e.g. 10" : "e.g. 130"; };
  $("#alAdd").onclick = () => busy($("#alAdd"), async () => {
    const v = $("#alVal").value;
    await api(`/api/trackers/${t.id}/alerts`, { method: "POST", body: { type: $("#alType").value, series: $("#alSeries").value, value: v ? Number(v) : null } });
    toast("Alert added; checking now…");
    await api(`/api/trackers/${t.id}/check`, { method: "POST" }).catch(() => {});
    renderTracker(t.id);
  });
  $$(".alert-row", el).forEach((row) => {
    const aid = row.dataset.aid;
    $("[data-act=toggle]", row).onchange = (e) => api(`/api/alerts/${aid}`, { method: "PATCH", body: { enabled: e.target.checked } }).catch((er) => toast(er.message, true));
    $("[data-act=del]", row).onclick = async () => { await api(`/api/alerts/${aid}`, { method: "DELETE" }); renderTracker(t.id); };
  });
}

async function renderTrackerSettings(t) {
  const el = $("#settingsPanel");
  const channels = await api("/api/channels").catch(() => []);
  el.innerHTML = `
    <h2>Tracker settings</h2>
    <div class="grid-form">
      <label class="field">Name<input id="sName" value="${esc(t.name)}"></label>
      <label class="field">Check every (minutes)<input id="sInt" type="number" min="2" value="${esc(t.interval_min ?? "")}" placeholder="default (${t.interval_effective})"></label>
      <label class="field">Notify channel<select id="sChan"><option value="">Default channel</option>
        ${channels.map((c) => `<option value="${c.id}" ${c.id === t.channel_id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
        ${t.channel_id && !channels.some((c) => c.id === t.channel_id) ? `<option value="${esc(t.channel_id)}" selected>${esc(t.channel_id)}</option>` : ""}</select></label>
    </div>
    <label class="field" style="margin-top:12px">URL<input id="sUrl" value="${esc(t.url)}"></label>
    <fieldset><legend>Options</legend>${optionsFields(t.site, t.kind, t.options)}</fieldset>
    <div class="modal-actions">
      ${t.kind === "search" ? `<button id="sReset" title="Forget all items; the next check re-learns the results without notifying">Reset items</button>` : ""}
      <button class="primary" id="sSave">Save settings</button></div>`;
  $("#sSave").onclick = () => busy($("#sSave"), async () => {
    const body = { name: $("#sName").value.trim(), interval_min: $("#sInt").value ? Number($("#sInt").value) : null,
      channel_id: $("#sChan").value || null, options: readOptions(el) };
    if ($("#sUrl").value.trim() !== t.url) body.url = $("#sUrl").value.trim();
    await api(`/api/trackers/${t.id}`, { method: "PATCH", body });
    toast("Saved");
    renderTracker(t.id);
  });
  if ($("#sReset")) $("#sReset").onclick = async () => {
    if (!confirm("Forget all items and price history for this search? The next check will re-learn the current results without sending notifications.")) return;
    await api(`/api/trackers/${t.id}/reset-baseline`, { method: "POST" });
    await api(`/api/trackers/${t.id}/check`, { method: "POST" });
    renderTracker(t.id);
  };
}

// ---------- activity ----------
const EVENT_ICON = { new_item: "🆕", returned: "🔁", price_alert: "💰", price_drop: "📉", all_time_low: "🏆", back_in_stock: "✅", error: "⚠️", recovered: "✅", test: "🔔" };
function eventsHtml(events, showTracker = false) {
  return (events || []).map((e) => `<div class="event">
    <span class="when" title="${fmtDate(e.ts)}">${ago(e.ts)}</span>
    <div class="what">
      <div>${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a>` : esc(e.title)}
        ${e.sent ? "" : `<span class="pill bad" title="No channel/webhook delivered this">not sent</span>`}</div>
      ${e.message ? `<div class="desc">${esc(e.message.replace(/\*\*|_/g, ""))}</div>` : ""}
      ${showTracker && e.tracker_name ? `<div class="hint"><a href="#/t/${e.tracker_id}">${esc(e.tracker_name)}</a></div>` : ""}
    </div></div>`).join("");
}

async function renderActivity() {
  const events = await api("/api/events?limit=300");
  view.innerHTML = `<div class="toolbar"><h1>Activity</h1></div>
    <div class="panel"><div class="events">${eventsHtml(events, true) || `<span class="muted">Nothing yet.</span>`}</div></div>`;
  refreshTimer = setInterval(renderActivity, 60000);
}

// ---------- settings ----------
async function renderSettings() {
  const [s, st, channels] = await Promise.all([api("/api/settings"), updateBotStatus(), api("/api/channels").catch(() => [])]);
  const invite = st && st.bot.user_id ? `https://discord.com/oauth2/authorize?client_id=${st.bot.user_id}&scope=bot+applications.commands&permissions=117760` : "";
  view.innerHTML = `
    <div class="toolbar"><h1>Settings</h1></div>
    <div class="panel"><h2>Discord</h2>
      <p class="hint">${!st ? "" : !st.bot.configured ? "No DISCORD_TOKEN set; only the webhook below can deliver notifications."
        : st.bot.ready ? `Bot <b>${esc(st.bot.user)}</b> is online in: ${st.bot.guilds.map((g) => esc(g.name)).join(", ") || "no servers"}.` : "Bot is configured but not connected (check the token/logs)."}
        ${invite ? ` <a href="${invite}" target="_blank" rel="noopener">Invite link</a>` : ""}</p>
      <div class="grid-form">
        <label class="field">Default notification channel
          <select data-s="default_channel_id"><option value="">(none)</option>
          ${channels.map((c) => `<option value="${c.id}" ${c.id === s.default_channel_id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}
          ${s.default_channel_id && !channels.some((c) => c.id === s.default_channel_id) ? `<option value="${esc(s.default_channel_id)}" selected>${esc(s.default_channel_id)}</option>` : ""}
          </select><span class="hint">Or run /setchannel in Discord</span></label>
        <label class="field">Mention with each notification<input data-s="mention" value="${esc(s.mention)}" placeholder="e.g. <@123…> or @here"></label>
        <label class="field">Webhook URL (fallback / no bot)<input data-s="webhook_url" value="${esc(s.webhook_url)}" placeholder="https://discord.com/api/webhooks/…"></label>
      </div>
      <label class="check" style="margin-top:12px"><input type="checkbox" data-s="notify_failures" ${s.notify_failures === "1" ? "checked" : ""}> Notify when a tracker keeps failing (and when it recovers)</label>
    </div>
    <div class="panel"><h2>Checking</h2>
      <div class="grid-form">
        <label class="field">Default interval (minutes)<input type="number" min="2" data-s="default_interval_min" value="${esc(s.default_interval_min)}"></label>
        <label class="field">Micro Center store ID<input data-s="mc_store_id" value="${esc(s.mc_store_id)}"><span class="hint">141 = Columbus</span></label>
        <label class="field">FlareSolverr URL (optional)<input data-s="flaresolverr_url" value="${esc(s.flaresolverr_url)}" placeholder="http://host.docker.internal:8191"><span class="hint">Fallback if Micro Center's Cloudflare blocks direct requests</span></label>
        <label class="field">Public URL of this GUI<input data-s="web_url" value="${esc(s.web_url)}" placeholder="http://192.168.1.10:8295"><span class="hint">Used for links in Discord</span></label>
      </div>
    </div>
    <div class="modal-actions" style="justify-content:flex-start"><button class="primary" id="setSave">Save</button><button id="setTest">Send test notification</button></div>`;
  const collect = () => Object.fromEntries($$("[data-s]").map((el) => [el.dataset.s, el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value.trim()]));
  $("#setSave").onclick = () => busy($("#setSave"), async () => { await api("/api/settings", { method: "PUT", body: collect() }); toast("Settings saved"); });
  $("#setTest").onclick = () => busy($("#setTest"), async () => {
    await api("/api/settings", { method: "PUT", body: collect() });
    await api("/api/settings/test", { method: "POST" });
    toast("Test notification sent");
  });
}

// ---------- boot ----------
updateBotStatus();
setInterval(updateBotStatus, 30000);
route();
