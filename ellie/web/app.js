import { barChart, heatmap, lineChart, rankBars, redrawAll, scatter } from "./charts.js";

// ---------------------------------------------------------------- data access

const cache = new Map();
async function api(path, { fresh = false } = {}) {
  if (!fresh && cache.has(path)) return cache.get(path);
  const p = fetch(path).then(async (r) => {
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
    return r.json();
  });
  cache.set(path, p);
  p.catch(() => cache.delete(path));
  return p;
}

// ---------------------------------------------------------------- formatting

const isNum = (v) => typeof v === "number" && isFinite(v);
const pct = (v, d = 1, sign = true) => (isNum(v) ? `${sign && v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%` : "–");
const pctPlain = (v, d = 1) => pct(v, d, false);
const num = (v, d = 2) => (isNum(v) ? v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : "–");
const money = (v) => (isNum(v) ? `$${num(v, 2)}` : "–");
function compact(v, prefix = "") {
  if (!isNum(v)) return "–";
  const a = Math.abs(v);
  const [d, s] = a >= 1e12 ? [1e12, "T"] : a >= 1e9 ? [1e9, "B"] : a >= 1e6 ? [1e6, "M"] : a >= 1e3 ? [1e3, "K"] : [1, ""];
  return `${v < 0 ? "-" : ""}${prefix}${(a / d).toFixed(a / d >= 100 ? 0 : 1)}${s}`;
}
const cls = (v) => (isNum(v) ? (v > 0 ? "up" : v < 0 ? "down" : "") : "");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SECTOR_ABBR = {
  "Information Technology": "Tech", "Communication Services": "Comm", "Consumer Discretionary": "Disc",
  "Consumer Staples": "Stpl", "Health Care": "Hlth", "Real Estate": "RE", "Financials": "Fin",
  "Industrials": "Ind", "Materials": "Matl", "Energy": "Enrg", "Utilities": "Util",
};
const SECTOR_SHORT = {
  "Information Technology": "Tech", "Communication Services": "Comm Svcs", "Consumer Discretionary": "Cons Disc",
  "Consumer Staples": "Staples", "Health Care": "Health Care", "Real Estate": "Real Estate", "Financials": "Financials",
  "Industrials": "Industrials", "Materials": "Materials", "Energy": "Energy", "Utilities": "Utilities",
};

// Status: icon + label, never color alone.
const STATUS_ICON = {
  good: '<circle cx="8" cy="8" r="7" fill="var(--good)"/><path d="M4.8 8.2l2.1 2.1 4.3-4.6" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  warning: '<path d="M8 1.5l7 12.5H1z" fill="var(--warning)"/><path d="M8 6v3.6" stroke="#0b0b0b" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.8" r="0.9" fill="#0b0b0b"/>',
  serious: '<rect x="2.3" y="2.3" width="11.4" height="11.4" rx="2" transform="rotate(45 8 8)" fill="var(--serious)"/><path d="M8 4.8v3.8" stroke="#0b0b0b" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11" r="0.9" fill="#0b0b0b"/>',
  critical: '<path d="M5 1h6l4 4v6l-4 4H5l-4-4V5z" fill="var(--critical)"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#fff" stroke-width="1.7" stroke-linecap="round"/>',
  info: '<circle cx="8" cy="8" r="7" fill="var(--axis)"/><path d="M8 7.2v4" stroke="var(--text-primary)" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="4.8" r="0.9" fill="var(--text-primary)"/>',
};
const STATUS_LABEL = { good: "Positive", warning: "Warning", serious: "Serious", critical: "Critical", info: "Info" };
const status = (sev, label) => `<span class="status"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">${STATUS_ICON[sev] || STATUS_ICON.info}</svg>${label ?? STATUS_LABEL[sev] ?? sev}</span>`;

const RULE_LABEL = { outsized_move: "Outsized move", new_52w_high: "52-week high", new_52w_low: "52-week low", volume_spike: "Volume spike" };

function tile(label, value, delta = "", opts = {}) {
  return `<div class="tile ${opts.hero ? "hero" : ""}"><div class="label">${label}</div><div class="value">${value}</div>${delta ? `<div class="delta">${delta}</div>` : ""}</div>`;
}
function seg(name, options, active) {
  return `<div class="seg" role="group" data-seg="${name}">${options.map(([v, l]) => `<button type="button" data-v="${v}" class="${String(v) === String(active) ? "on" : ""}" aria-pressed="${String(v) === String(active)}">${l}</button>`).join("")}</div>`;
}
function onSeg(root, name, fn) {
  const g = root.querySelector(`[data-seg="${name}"]`);
  g.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    g.querySelectorAll("button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
    fn(b.dataset.v);
  });
}
function sliceSeries(s, days) {
  if (!days || days === "all") return s;
  const n = Math.min(s.dates.length, Math.round(+days));
  return { dates: s.dates.slice(-n), values: s.values.slice(-n) };
}
function tableView(headers, rows) {
  return `<details class="table-view"><summary>View as table</summary><div class="table-wrap" style="max-height:260px"><table><thead><tr>${headers.map((h, i) => `<th class="${i ? "" : "l"}">${h}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? "" : "l"}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
}
const RANGES = [["21", "1M"], ["63", "3M"], ["126", "6M"], ["252", "1Y"], ["all", "All"]];

// ---------------------------------------------------------------- views

async function viewOverview(root) {
  const [ov, alerts] = await Promise.all([api("/api/overview"), api("/api/alerts")]);
  const ix = ov.index, b = ov.breadth;
  const alertTotal = Object.values(ov.alert_counts).reduce((a, c) => a + c, 0);
  root.innerHTML = `
    <div class="page-head"><div><h1>Market overview</h1><p>All ${ov.sectors.reduce((a, s) => a + (s.members || 0), 0)} S&P 500 members · close of ${ov.as_of}</p></div></div>
    <div class="tiles">
      ${tile(esc(ix.name), num(ix.level, 2), `<span class="${cls(ix.returns["1D"])}">${pct(ix.returns["1D"], 2)}</span> today`, { hero: true })}
      ${tile("Year to date", `<span class="${cls(ix.returns.YTD)}">${pct(ix.returns.YTD)}</span>`, `1Y ${pct(ix.returns["1Y"])}`)}
      ${tile("Index volatility (1Y, annualized)", pctPlain(ix.vol_1y))}
      ${tile("Above 200-day average", pctPlain(b.pct_above_200dma, 0), `${pctPlain(b.pct_above_50dma, 0)} above 50-day`)}
      ${tile("Advancers / decliners", `${b.advancers} / ${b.decliners}`, `${b.new_52w_highs} new highs · ${b.new_52w_lows} new lows`)}
      ${tile("Active alerts", alertTotal, `${ov.alert_counts.critical || 0} critical · ${ov.alert_counts.serious || 0} serious`)}
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h2>Index level</h2><div class="sub">Equal-weighted across all members, rebalanced daily (base 100)</div></div>${seg("range", RANGES, "252")}</div>
      <div class="chart" id="c-index"></div>
    </div>
    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card">
        <div class="card-head"><div><h2>Sector performance</h2><div class="sub">Equal-weighted sector return · click a sector to screen it</div></div>${seg("speriod", [["1D", "1D"], ["1W", "1W"], ["1M", "1M"], ["3M", "3M"], ["YTD", "YTD"], ["1Y", "1Y"]], "1M")}</div>
        <div class="chart" id="c-sectors"></div>
      </div>
      <div class="card">
        <div class="card-head"><div><h2>Sector returns by period</h2><div class="sub">Blue = gain, red = loss; color scaled within each period</div></div></div>
        <div id="c-heat"></div>
      </div>
    </div>
    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card">
        <div class="card-head"><div><h2>Market breadth</h2><div class="sub">Share of members above their own 200-day moving average</div></div></div>
        <div class="chart" id="c-breadth"></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Top movers today</h2>${seg("movers", [["gainers", "Gainers"], ["losers", "Losers"]], "gainers")}</div>
        <div id="movers"></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><div><h2>Alerts</h2><div class="sub">Rule-based signals on the latest session</div></div>${seg("sev", [["all", "All"], ["critical", "Critical"], ["serious", "Serious"], ["warning", "Warning"], ["good", "Positive"]], "all")}</div>
      <div id="alerts" class="alert-list"></div>
    </div>`;

  const drawIndex = (r) => lineChart(root.querySelector("#c-index"), {
    series: [{ name: "Index", ...sliceSeries(ix.series, r), color: "--series-1" }],
    area: true, endLabel: true, height: 280, yFormat: (v) => num(v, v >= 1000 ? 0 : 1),
  });
  drawIndex("252");
  onSeg(root, "range", drawIndex);

  const drawSectors = (p) => {
    const rows = ov.sectors.map((s) => ({ label: s.sector, value: s[p], onClick: () => (location.hash = `#/screener?sector=${encodeURIComponent(s.sector)}`), extra: `<div class="tt-row"><span>Members</span><span>${s.members}</span></div>` }))
      .sort((a, b) => b.value - a.value);
    barChart(root.querySelector("#c-sectors"), rows, { format: (v) => pct(v), valueName: `${p} return`, label: `Sector returns ${p}` });
  };
  drawSectors("1M");
  onSeg(root, "speriod", drawSectors);

  const periods = ["1D", "1W", "1M", "3M", "YTD", "1Y"];
  const secs = [...ov.sectors].sort((a, b) => (b["1Y"] ?? 0) - (a["1Y"] ?? 0));
  // Scale each column separately so a 1-day move is not washed out by a 1-year one.
  const colMax = periods.map((p) => Math.max(...secs.map((s) => Math.abs(s[p] ?? 0)), 1e-9));
  heatmap(root.querySelector("#c-heat"), {
    rows: secs.map((s) => s.sector), cols: periods,
    values: secs.map((s) => periods.map((p, j) => (s[p] == null ? null : s[p] / colMax[j]))),
    labels: secs.map((s) => periods.map((p) => pct(s[p]))),
    format: (v) => v, maxAbs: 1, legendLabels: ["Largest loss", "Largest gain"],
    onRowClick: (i) => (location.hash = `#/screener?sector=${encodeURIComponent(secs[i].sector)}`),
  });

  lineChart(root.querySelector("#c-breadth"), {
    series: [{ name: "% above 200-day", ...ov.breadth_history, color: "--series-1" }],
    height: 220, yMin: 0, yMax: 1, area: true, yFormat: (v) => pctPlain(v, 0),
  });

  const drawMovers = (k) => {
    root.querySelector("#movers").innerHTML = `<div class="table-wrap"><table><thead><tr><th>Stock</th><th class="l">Sector</th><th>Price</th><th>1D</th></tr></thead><tbody>${ov.top_movers[k].map((m) => `<tr class="link" data-sym="${m.symbol}"><td><span class="sym">${m.symbol}</span> <span class="nm">${esc(m.name)}</span></td><td class="l muted">${SECTOR_SHORT[m.sector] || m.sector}</td><td>${money(m.price)}</td><td class="${cls(m.ret_1D)}">${pct(m.ret_1D)}</td></tr>`).join("")}</tbody></table></div>`;
    linkRows(root.querySelector("#movers"));
  };
  drawMovers("gainers");
  onSeg(root, "movers", drawMovers);

  let showAll = false, sevNow = "all";
  const drawAlerts = (sev = sevNow) => {
    sevNow = sev;
    const matching = alerts.filter((a) => sev === "all" || a.severity === sev);
    const list = showAll ? matching : matching.slice(0, 12);
    root.querySelector("#alerts").innerHTML = list.length
      ? list.map((a) => `<div class="alert-item">${status(a.severity, RULE_LABEL[a.rule])}<a href="#/stock/${a.symbol}" class="sym"><b>${a.symbol}</b></a><span class="detail" title="${esc(a.name)}">${esc(a.detail)} · <span class="muted">${esc(a.name)}</span></span></div>`).join("")
        + (matching.length > 12 ? `<p class="note"><button class="btn" id="more-alerts">${showAll ? "Show fewer" : `Show all ${matching.length}`}</button></p>` : "")
      : `<div class="empty">No alerts in this category.</div>`;
    root.querySelector("#more-alerts")?.addEventListener("click", () => { showAll = !showAll; drawAlerts(); });
  };
  drawAlerts("all");
  onSeg(root, "sev", (v) => { showAll = false; drawAlerts(v); });
}

function linkRows(scope) {
  scope.querySelectorAll("tr[data-sym]").forEach((tr) => tr.addEventListener("click", () => (location.hash = `#/stock/${tr.dataset.sym}`)));
}

// ---------------------------------------------------------------- screener

const SCREEN_COLS = [
  ["symbol", "Stock", "l", (r) => `<span class="sym">${r.symbol}</span> <span class="nm">${esc(r.name)}</span>`],
  ["sector", "Sector", "l", (r) => `<span class="muted">${SECTOR_SHORT[r.sector] || r.sector}</span>`],
  ["price", "Price", "", (r) => money(r.price)],
  ["ret_1D", "1D", "", (r) => `<span class="${cls(r.ret_1D)}">${pct(r.ret_1D)}</span>`],
  ["ret_1M", "1M", "", (r) => `<span class="${cls(r.ret_1M)}">${pct(r.ret_1M)}</span>`],
  ["ret_YTD", "YTD", "", (r) => `<span class="${cls(r.ret_YTD)}">${pct(r.ret_YTD)}</span>`],
  ["ret_1Y", "1Y", "", (r) => `<span class="${cls(r.ret_1Y)}">${pct(r.ret_1Y)}</span>`],
  ["vol_1y", "Vol 1Y", "", (r) => pctPlain(r.vol_1y, 0)],
  ["beta", "Beta", "", (r) => num(r.beta)],
  ["sharpe_1y", "Sharpe", "", (r) => num(r.sharpe_1y)],
  ["max_dd_1y", "Max DD", "", (r) => pct(r.max_dd_1y, 0)],
  ["var95_1d", "VaR 95%", "", (r) => pctPlain(r.var95_1d)],
  ["pe", "P/E", "", (r) => num(r.pe, 1)],
  ["market_cap", "Mkt cap", "", (r) => compact(r.market_cap, "$")],
  ["rsi_14", "RSI", "", (r) => num(r.rsi_14, 0)],
];
const PRESETS = {
  all: ["All", () => true],
  momentum: ["Momentum leaders", (r) => r.momentum_12_1 > 0.2 && r.above_200dma],
  lowvol: ["Low volatility", (r) => r.vol_1y < 0.2],
  value: ["Cheap vs sector", (r) => r.pe_vs_sector < -0.25 && r.pe > 0],
  oversold: ["Oversold (RSI < 30)", (r) => r.rsi_14 < 30],
  highs: ["Near 52-week high", (r) => r.pct_from_52w_high > -0.02],
  stressed: ["Drawdown > 30%", (r) => r.max_dd_1y < -0.3],
};

async function viewScreener(root, params) {
  const rows = await api("/api/screener");
  const sectors = [...new Set(rows.map((r) => r.sector))].sort();
  const st = { q: params.get("q") || "", sector: params.get("sector") || "", preset: params.get("preset") || "all", key: "market_cap", dir: -1 };
  if (!rows.some((r) => r.market_cap != null)) st.key = "symbol", st.dir = 1;
  root.innerHTML = `
    <div class="page-head"><div><h1>Screener</h1><p>Filter and rank every member on returns, risk and valuation</p></div><button class="btn" id="export">Export CSV</button></div>
    <div class="card">
      <div class="filters">
        <input id="q" type="search" placeholder="Filter by ticker or name" value="${esc(st.q)}" aria-label="Filter" />
        <select id="sector" aria-label="Sector"><option value="">All sectors</option>${sectors.map((s) => `<option ${s === st.sector ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
        <select id="preset" aria-label="Screen">${Object.entries(PRESETS).map(([k, [l]]) => `<option value="${k}" ${k === st.preset ? "selected" : ""}>${l}</option>`).join("")}</select>
        <span class="muted small" id="count"></span>
      </div>
      <div class="table-wrap tall" id="tbl"></div>
    </div>`;
  let current = [];
  const draw = () => {
    const q = st.q.toLowerCase();
    current = rows.filter((r) => (!q || r.symbol.toLowerCase().includes(q) || r.name.toLowerCase().includes(q)) && (!st.sector || r.sector === st.sector) && PRESETS[st.preset][1](r));
    current.sort((a, b) => {
      const x = a[st.key], y = b[st.key];
      if (x == null) return 1; if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * st.dir;
    });
    root.querySelector("#count").textContent = `${current.length} of ${rows.length} stocks`;
    root.querySelector("#tbl").innerHTML = `<table><thead><tr>${SCREEN_COLS.map(([k, l, c]) => `<th class="sortable ${c}" data-k="${k}">${l}${st.key === k ? `<span class="arrow">${st.dir > 0 ? "▲" : "▼"}</span>` : ""}</th>`).join("")}</tr></thead><tbody>${current.map((r) => `<tr class="link" data-sym="${r.symbol}">${SCREEN_COLS.map(([, , c, f]) => `<td class="${c}">${f(r)}</td>`).join("")}</tr>`).join("")}</tbody></table>${current.length ? "" : '<div class="empty">No stocks match these filters.</div>'}`;
    root.querySelectorAll("th[data-k]").forEach((th) => th.addEventListener("click", () => {
      st.dir = st.key === th.dataset.k ? -st.dir : (["symbol", "sector"].includes(th.dataset.k) ? 1 : -1);
      st.key = th.dataset.k; draw();
    }));
    linkRows(root.querySelector("#tbl"));
  };
  root.querySelector("#q").addEventListener("input", (e) => { st.q = e.target.value; draw(); });
  root.querySelector("#sector").addEventListener("change", (e) => { st.sector = e.target.value; draw(); });
  root.querySelector("#preset").addEventListener("change", (e) => { st.preset = e.target.value; draw(); });
  root.querySelector("#export").addEventListener("click", () => {
    const keys = Object.keys(rows[0]);
    const csv = [keys.join(","), ...current.map((r) => keys.map((k) => { const v = r[k]; return typeof v === "string" && /[",]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v ?? ""; }).join(","))].join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = "ellie-screener.csv"; a.click();
  });
  draw();
}

// ---------------------------------------------------------------- stock detail

async function viewStock(root, params, symbol) {
  const s = await api(`/api/stocks/${encodeURIComponent(symbol)}`);
  const m = s.metrics, med = s.sector_median, p = s.profile;
  root.innerHTML = `
    <div class="page-head">
      <div><h1>${esc(p.name)} <span class="pill">${s.symbol}</span></h1><p>${esc(p.sector)} · ${esc(p.sub_industry || "")}${p.headquarters ? " · " + esc(p.headquarters) : ""}</p></div>
      <div style="text-align:right"><div style="font-size:28px;font-weight:600">${money(m.price)}</div><div class="${cls(m.ret_1D)}">${pct(m.ret_1D, 2)} today</div></div>
    </div>
    <div class="tiles">
      ${tile("1-year return", `<span class="${cls(m.ret_1Y)}">${pct(m.ret_1Y)}</span>`, `Sector median ${pct(med.ret_1Y)}`)}
      ${tile("Volatility (1Y)", pctPlain(m.vol_1y), `Sector median ${pctPlain(med.vol_1y)}`)}
      ${tile("Beta vs index", num(m.beta), `Sector median ${num(med.beta)}`)}
      ${tile("Sharpe ratio (1Y)", num(m.sharpe_1y), `Sector median ${num(med.sharpe_1y)}`)}
      ${tile("1-day VaR (95%)", pctPlain(m.var95_1d), `Expected shortfall ${pctPlain(m.es95_1d)}`)}
      ${tile("Max drawdown (1Y)", pct(m.max_dd_1y), `${pct(m.pct_from_52w_high)} from 52-week high`)}
      ${tile("P/E", num(m.pe, 1), isNum(m.pe_vs_sector) ? `${pct(m.pe_vs_sector, 0)} vs sector median` : "")}
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-head">
        <div><h2>Price and forecast</h2><div class="sub" id="fc-model">Loading forecast model…</div></div>
        <div class="filters" style="margin:0">${seg("hrange", RANGES.slice(1), "252")}${seg("horizon", [["21", "1M ahead"], ["42", "2M"], ["63", "3M"]], params.get("h") || "21")}</div>
      </div>
      <div class="grid" style="grid-template-columns:minmax(0,1fr) 240px" id="fc-grid">
        <div class="chart" id="c-price"></div>
        <div id="fc-summary" class="muted small">Running models…</div>
      </div>
    </div>
    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card"><div class="card-head"><div><h2>Performance relative to the index</h2><div class="sub">Stock ÷ equal-weight index, rebased to 100</div></div></div><div class="chart" id="c-rel"></div></div>
      <div class="card"><div class="card-head"><div><h2>Drawdown</h2><div class="sub">Decline from running peak</div></div></div><div class="chart" id="c-dd"></div></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h2>Forecast scorecard</h2><div class="sub">Walk-forward backtest: each model refit at 8 past dates and scored on what actually happened next</div></div></div>
      <div id="scorecard" class="muted small">Backtesting…</div>
    </div>
    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card"><div class="card-head"><div><h2>Volatility forecast</h2><div class="sub">GARCH(1,1) expected annualized volatility by day ahead</div></div></div><div id="vol"></div></div>
      <div class="card"><div class="card-head"><div><h2>Direction model</h2><div class="sub">Gradient-boosted classifier · 5-day horizon</div></div></div><div id="dir"></div></div>
    </div>
    <div class="grid cols-2">
      <div class="card"><div class="card-head"><div><h2>Valuation vs sector</h2><div class="sub">Latest annual filing; sector = median of ${esc(p.sector)} members</div></div></div><div id="val"></div></div>
      <div class="card"><div class="card-head"><div><h2>Reported fundamentals</h2><div class="sub">Annual (10-K) · source: ${esc(s.fundamentals_source || "n/a")}</div></div></div><div id="funda"></div></div>
    </div>`;

  if (window.matchMedia("(max-width: 900px)").matches) root.querySelector("#fc-grid").style.gridTemplateColumns = "minmax(0,1fr)";

  lineChart(root.querySelector("#c-rel"), { series: [{ name: "Relative", ...s.relative, color: "--series-1" }], height: 220, yFormat: (v) => num(v, 0) });
  lineChart(root.querySelector("#c-dd"), { series: [{ name: "Drawdown", ...s.drawdown, color: "--div-neg" }], height: 220, area: true, zeroLine: true, yMax: 0, yFormat: (v) => pct(v, 0, false) });

  const valRows = [
    ["P/E", "pe", (v) => num(v, 1)], ["Price / sales", "ps", (v) => num(v, 1)], ["Price / book", "pb", (v) => num(v, 1)],
    ["Earnings yield", "earnings_yield", (v) => pctPlain(v)], ["Net margin", "net_margin", (v) => pctPlain(v)],
    ["Return on equity", "roe", (v) => pctPlain(v)], ["Debt / equity", "debt_to_equity", (v) => num(v)],
    ["Revenue growth (YoY)", "revenue_growth", (v) => pct(v)], ["EPS growth (YoY)", "eps_growth", (v) => pct(v)],
    ["Market cap", "market_cap", (v) => compact(v, "$")],
  ];
  root.querySelector("#val").innerHTML = `<table><thead><tr><th>Metric</th><th>${s.symbol}</th><th>Sector median</th></tr></thead><tbody>${valRows.map(([l, k, f]) => `<tr><td>${l}</td><td>${f(m[k])}</td><td class="muted">${f(med[k])}</td></tr>`).join("")}</tbody></table>`;

  const F = s.fundamentals;
  const fRows = [["Revenue", "revenue", (v) => compact(v, "$")], ["Net income", "net_income", (v) => compact(v, "$")], ["Diluted EPS", "eps_diluted", (v) => money(v)], ["Operating cash flow", "operating_cash_flow", (v) => compact(v, "$")], ["Total assets", "total_assets", (v) => compact(v, "$")], ["Total liabilities", "total_liabilities", (v) => compact(v, "$")], ["Equity", "equity", (v) => compact(v, "$")]];
  const years = F.slice(-5);
  root.querySelector("#funda").innerHTML = years.length
    ? `<div class="table-wrap"><table><thead><tr><th>Fiscal year end</th>${years.map((y) => `<th>${y.period_end}</th>`).join("")}</tr></thead><tbody>${fRows.map(([l, k, f]) => `<tr><td>${l}</td>${years.map((y) => `<td>${f(y[k])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">No filings available.</div>`;

  let range = "252", horizon = params.get("h") || "21", fc = null;
  const drawPrice = () => {
    const hist = sliceSeries(s.history, range);
    const opts = { series: [{ name: "Close", ...hist, color: "--series-1" }], height: 300, yFormat: (v) => money(v) };
    if (fc) {
      const d = [fc.last_date, ...fc.paths.map((r) => r.date)];
      opts.series.push({ name: "Forecast median", dates: d, values: [fc.last_close, ...fc.paths.map((r) => r.p50)], color: "--series-2" });
      opts.bands = [{ name: "80% interval", dates: d, lo: [fc.last_close, ...fc.paths.map((r) => r.p10)], hi: [fc.last_close, ...fc.paths.map((r) => r.p90)], color: "--series-2", opacity: 0.16 }];
    }
    lineChart(root.querySelector("#c-price"), opts);
  };
  drawPrice();
  onSeg(root, "hrange", (v) => { range = v; drawPrice(); });

  const loadForecast = async () => {
    root.querySelector("#fc-summary").innerHTML = `<span class="muted small">Running models…</span>`;
    try {
      fc = await api(`/api/stocks/${s.symbol}/forecast?horizon=${horizon}`);
    } catch (e) {
      root.querySelector("#fc-summary").innerHTML = `<div class="error">${esc(e.message)}</div>`;
      return;
    }
    renderForecast(root, fc);
    drawPrice();
  };
  onSeg(root, "horizon", (v) => { horizon = v; loadForecast(); });
  loadForecast();
}

function renderForecast(root, fc) {
  const end = fc.paths[fc.paths.length - 1];
  const bt = Object.fromEntries(fc.backtest.map((r) => [r.model, r]));
  root.querySelector("#fc-model").textContent = `${fc.model} · ${fc.horizon_days} trading days ahead`;
  root.querySelector("#fc-summary").innerHTML = `
    <dl class="kv">
      <dt>Median target</dt><dd>${money(end.p50)}</dd>
      <dt>Implied change</dt><dd class="${cls(end.p50 / fc.last_close - 1)}">${pct(end.p50 / fc.last_close - 1)}</dd>
      <dt>80% range</dt><dd>${money(end.p10)} – ${money(end.p90)}</dd>
      <dt>95% range</dt><dd>${money(end.p05)} – ${money(end.p95)}</dd>
      <dt>Probability higher</dt><dd>${pctPlain(end.prob_up, 0)}</dd>
    </dl>
    <p class="note">Backtest: this model's typical ${fc.horizon_days}-day error is ${pctPlain(bt.arima_garch?.mae)} (random walk ${pctPlain(bt.naive?.mae)}). The 80% band contained the outcome ${pctPlain(bt.arima_garch?.coverage_80, 0)} of the time.</p>`;

  const best = fc.backtest.reduce((a, r) => (r.model !== "naive" && (!a || r.skill_vs_naive > a.skill_vs_naive) ? r : a), null);
  root.querySelector("#scorecard").innerHTML = `
    <div class="table-wrap"><table><thead><tr><th>Model</th><th>Mean abs. error</th><th>RMSE</th><th>Direction hit rate</th><th>80% band coverage</th><th>Skill vs random walk</th></tr></thead>
    <tbody>${fc.backtest.map((r) => `<tr><td>${esc(r.label)}</td><td>${pctPlain(r.mae, 2)}</td><td>${pctPlain(r.rmse, 2)}</td><td>${r.hit_rate == null ? "n/a" : pctPlain(r.hit_rate, 0)}</td><td>${pctPlain(r.coverage_80, 0)}</td><td class="${cls(r.skill_vs_naive)}">${r.model === "naive" ? "benchmark" : pct(r.skill_vs_naive, 1)}</td></tr>`).join("")}</tbody></table></div>
    <p class="note">${best && best.skill_vs_naive > 0 ? `${esc(best.label)} beat the random-walk benchmark by ${pct(best.skill_vs_naive, 1)} on average error.` : "No model beat the random-walk benchmark on this stock — treat the median as a neutral anchor and rely on the interval for risk sizing."} Errors are in log-price terms over ${fc.horizon_days} trading days; ${fc.backtest[0]?.n ?? 0} forecast origins.</p>`;

  const v = fc.volatility;
  root.querySelector("#vol").innerHTML = `
    <div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(110px,1fr));margin-bottom:12px">
      ${tile("Realized (21d)", pctPlain(v.realized_21d))}${tile("EWMA", pctPlain(v.ewma))}${tile("GARCH next day", pctPlain(v.garch_next_day))}${tile("Long-run", pctPlain(v.garch_long_run))}
    </div><div class="chart" id="c-vol"></div>
    <p class="note">Persistence ${num(v.persistence, 3)} — ${v.persistence > 0.97 ? "volatility shocks fade slowly" : "volatility shocks fade within weeks"}.</p>`;
  lineChart(root.querySelector("#c-vol"), {
    series: [{ name: "Expected volatility", dates: fc.paths.map((r) => r.date), values: v.term_structure, color: "--series-1" }],
    height: 160, yFormat: (x) => pctPlain(x, 1),
  });

  const d = fc.direction;
  root.querySelector("#dir").innerHTML = d.available ? `
    <dl class="kv" style="margin-bottom:12px">
      <dt>Probability higher in ${d.horizon_days} days</dt><dd>${pctPlain(d.prob_up, 0)}</dd>
      <dt>Walk-forward accuracy</dt><dd>${pctPlain(d.walk_forward_accuracy, 1)}</dd>
      <dt>Majority-class baseline</dt><dd>${pctPlain(d.baseline_accuracy, 1)}</dd>
      <dt>Edge over baseline</dt><dd class="${cls(d.edge)}">${pct(d.edge, 1)}</dd>
    </dl>
    <h3 style="margin-bottom:6px">What the model leans on</h3>
    <div class="chart" id="c-imp"></div>
    <p class="note">${d.edge > 0.02 ? "The classifier has shown a modest out-of-sample edge." : "No reliable out-of-sample edge — the probability above should not drive decisions on its own."} ${d.folds} time-ordered folds with a ${d.horizon_days}-day gap to prevent look-ahead.</p>`
    : `<div class="empty">${esc(d.reason)}</div>`;
  if (d.available) {
    const names = { ret_1: "1-day return", ret_5: "5-day return", ret_21: "1-month return", ret_63: "3-month return", vol_21: "1-month volatility", vol_ratio: "Vol regime", rsi_14: "RSI (14)", dist_50dma: "vs 50-day avg", mkt_ret_5: "Market 5-day", mkt_ret_21: "Market 1-month" };
    rankBars(root.querySelector("#c-imp"), d.feature_importance.slice(0, 6).map((f) => ({ label: names[f.feature] || f.feature, value: f.importance })), { format: (x) => pctPlain(x, 0) });
  }
}

// ---------------------------------------------------------------- sectors

async function viewSectors(root) {
  const [sec, ov] = await Promise.all([api("/api/sectors"), api("/api/overview")]);
  const names = Object.keys(sec.series).sort();
  let chosen = "Information Technology" in sec.series ? "Information Technology" : names[0];
  root.innerHTML = `
    <div class="page-head"><div><h1>Sectors</h1><p>Equal-weighted GICS sector indices, correlations and risk profile</p></div></div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h2>Sector vs market</h2><div class="sub">Both rebased to 100 at the start of the window</div></div>
        <div class="filters" style="margin:0"><select id="sec" aria-label="Sector">${names.map((n) => `<option ${n === chosen ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>${seg("range", RANGES.slice(1), "252")}</div></div>
      <div class="chart" id="c-sec"></div>
    </div>
    <div class="grid cols-2">
      <div class="card"><div class="card-head"><div><h2>Correlation of daily returns (1Y)</h2><div class="sub">Low correlation = better diversification</div></div></div><div id="c-corr"></div></div>
      <div class="card"><div class="card-head"><div><h2>Sector risk profile</h2><div class="sub">Median across members</div></div></div><div class="table-wrap" id="med"></div></div>
    </div>`;
  let range = "252";
  const rebase = (s) => ({ dates: s.dates, values: s.values.map((v) => (v / s.values[0]) * 100) });
  const draw = () => lineChart(root.querySelector("#c-sec"), {
    series: [
      { name: chosen, ...rebase(sliceSeries(sec.series[chosen], range)), color: "--series-1" },
      { name: "All S&P 500 (equal-weight)", ...rebase(sliceSeries(ov.index.series, range)), color: "--series-2" },
    ],
    height: 280, yFormat: (v) => num(v, 0),
  });
  draw();
  root.querySelector("#sec").addEventListener("change", (e) => { chosen = e.target.value; draw(); });
  onSeg(root, "range", (v) => { range = v; draw(); });

  const flat = sec.correlation.matrix.flat().filter((v) => v < 0.9999);
  heatmap(root.querySelector("#c-corr"), {
    rows: sec.correlation.labels.map((l) => SECTOR_SHORT[l] || l), cols: sec.correlation.labels.map((l) => SECTOR_ABBR[l] || l),
    values: sec.correlation.matrix, format: (v) => v.toFixed(2), domain: [Math.floor(Math.min(...flat) * 10) / 10, 1], rowWidth: "minmax(70px, 90px)",
  });
  const cols = [["vol_1y", "Vol", (v) => pctPlain(v, 0)], ["beta", "Beta", (v) => num(v)], ["sharpe_1y", "Sharpe", (v) => num(v)], ["max_dd_1y", "Max DD", (v) => pct(v, 0)], ["var95_1d", "VaR 95%", (v) => pctPlain(v)], ["pe", "P/E", (v) => num(v, 1)], ["ret_YTD", "YTD", (v) => pct(v)]];
  root.querySelector("#med").innerHTML = `<table><thead><tr><th>Sector</th>${cols.map(([, l]) => `<th>${l}</th>`).join("")}</tr></thead><tbody>${sec.medians.map((r) => `<tr class="link" data-sector="${esc(r.sector)}"><td>${esc(r.sector)}</td>${cols.map(([k, , f]) => `<td class="${k === "ret_YTD" ? cls(r[k]) : ""}">${f(r[k])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  root.querySelectorAll("tr[data-sector]").forEach((tr) => tr.addEventListener("click", () => (location.hash = `#/screener?sector=${encodeURIComponent(tr.dataset.sector)}`)));
}

// ---------------------------------------------------------------- risk

async function viewRisk(root) {
  const r = await api("/api/risk");
  const sectors = [...new Set(r.scatter.map((p) => p.sector))].sort();
  root.innerHTML = `
    <div class="page-head"><div><h1>Risk</h1><p>Where the index carries its volatility, tail risk and drawdowns</p></div></div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h2>Risk vs return (1Y)</h2><div class="sub">Each dot is one member · click to open</div></div>
        <select id="hl" aria-label="Highlight sector"><option value="">Highlight a sector…</option>${sectors.map((s) => `<option>${esc(s)}</option>`).join("")}</select></div>
      <div class="chart" id="c-scatter"></div>
    </div>
    <div class="grid cols-2">
      <div class="card"><div class="card-head"><div><h2>Largest 1-day value at risk</h2><div class="sub">Historical 95% VaR and expected shortfall, last 252 sessions</div></div></div><div class="table-wrap" id="t-var"></div></div>
      <div class="card"><div class="card-head"><div><h2>Deepest drawdowns</h2><div class="sub">Peak-to-trough over the last year</div></div></div><div class="table-wrap" id="t-dd"></div></div>
    </div>`;
  const draw = (hl) => scatter(root.querySelector("#c-scatter"), r.scatter.map((p) => ({ x: p.vol_1y, y: p.ret_1Y, label: `${p.symbol} · ${p.sector}`, symbol: p.symbol, highlight: hl && p.sector === hl })), {
    xName: "Annualized volatility", yName: "1-year return", xFormat: (v) => pctPlain(v, 0), yFormat: (v) => pct(v, 0), height: 360,
    onClick: (p) => (location.hash = `#/stock/${p.symbol}`),
  });
  draw("");
  root.querySelector("#hl").addEventListener("change", (e) => draw(e.target.value));
  const tbl = (rows) => `<table><thead><tr><th>Stock</th><th>Vol 1Y</th><th>Beta</th><th>VaR 95%</th><th>ES 95%</th><th>Max DD</th></tr></thead><tbody>${rows.map((x) => `<tr class="link" data-sym="${x.symbol}"><td><span class="sym">${x.symbol}</span> <span class="nm">${esc(x.name)}</span></td><td>${pctPlain(x.vol_1y, 0)}</td><td>${num(x.beta)}</td><td>${pctPlain(x.var95_1d)}</td><td>${pctPlain(x.es95_1d)}</td><td>${pct(x.max_dd_1y, 0)}</td></tr>`).join("")}</tbody></table>`;
  root.querySelector("#t-var").innerHTML = tbl(r.highest_var);
  root.querySelector("#t-dd").innerHTML = tbl(r.deepest_drawdown);
  linkRows(root);
}

// ---------------------------------------------------------------- macro

async function viewMacro(root) {
  const series = await api("/api/macro");
  root.innerHTML = `
    <div class="page-head"><div><h1>Macro backdrop</h1><p>Rates, inflation, labor and credit conditions (FRED)</p></div>${seg("range", RANGES.slice(1), "252")}</div>
    <div class="grid cols-4">${series.map((s, i) => `<div class="card"><h3>${esc(s.label)}</h3><div style="font-size:22px;font-weight:600;margin:4px 0 8px">${num(s.latest, 2)}${s.unit === "%" ? "%" : ""}</div><div class="small muted" style="margin-bottom:6px">${s.id} · ${s.latest_date}</div><div class="chart" id="m-${i}"></div></div>`).join("")}</div>`;
  const draw = (range) => series.forEach((s, i) => {
    // Monthly series have ~21x fewer points; convert the trading-day window to calendar time.
    const cutoff = range === "all" ? "" : new Date(Date.now() - (+range / 252) * 365 * 864e5).toISOString().slice(0, 10);
    const idx = s.series.dates.findIndex((d) => d >= cutoff);
    const from = Math.max(0, Math.min(idx < 0 ? 0 : idx, s.series.dates.length - 2));
    lineChart(root.querySelector(`#m-${i}`), {
      series: [{ name: s.label, dates: s.series.dates.slice(from), values: s.series.values.slice(from), color: "--series-1" }],
      height: 140, yFormat: (v) => num(v, Math.abs(v) >= 100 ? 0 : 1), label: s.label,
    });
  });
  draw("252");
  onSeg(root, "range", draw);
}

// ---------------------------------------------------------------- data quality

async function viewQuality(root) {
  const [q, st] = await Promise.all([api("/api/quality", { fresh: true }), api("/api/status", { fresh: true })]);
  const count = (sev) => q.issue_summary.filter((i) => i.severity === sev).reduce((a, i) => a + i.n, 0);
  root.innerHTML = `
    <div class="page-head"><div><h1>Data quality</h1><p>Source health, cross-source reconciliation and ingestion history</p></div>
      <div class="filters" style="margin:0"><select id="mode" aria-label="Ingestion mode"><option value="auto">Auto (live, fall back to demo)</option><option value="live">Live sources only</option><option value="synthetic">Synthetic demo data</option></select><button class="btn primary" id="refresh" ${st.ingest_running ? "disabled" : ""}>${st.ingest_running ? "Refreshing…" : "Refresh data"}</button></div></div>
    <div class="tiles">
      ${tile("Data mode", st.synthetic ? status("warning", "Synthetic demo") : status("good", "Live"))}
      ${tile("Members tracked", st.symbols ?? "–", `${st.trading_days ?? "–"} trading days`)}
      ${tile("Prices confirmed by 2+ sources", pctPlain(q.coverage.multi_source_share, 1))}
      ${tile("Critical issues", count("critical"), "latest run")}
      ${tile("Warnings", count("warning"), `${count("info")} informational`)}
    </div>
    <div class="grid cols-2" style="margin-bottom:16px">
      <div class="card"><div class="card-head"><h2>Sources (latest run)</h2></div><div class="table-wrap"><table><thead><tr><th>Source</th><th class="l">Dataset</th><th>Rows</th><th>Keys</th><th>Errors</th></tr></thead><tbody>${q.sources.map((s) => `<tr><td>${esc(s.source)}</td><td class="l">${s.dataset}</td><td>${s.rows.toLocaleString()}</td><td>${s.symbols}</td><td>${s.errors ? status("warning", s.errors) : "0"}</td></tr>`).join("")}</tbody></table></div></div>
      <div class="card"><div class="card-head"><h2>Checks</h2></div><div class="table-wrap"><table><thead><tr><th>Check</th><th class="l">Severity</th><th>Count</th></tr></thead><tbody>${q.issue_summary.map((i) => `<tr><td>${i.check_name.replace(/_/g, " ")}</td><td class="l">${status(i.severity)}</td><td>${i.n}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">All checks passed.</td></tr>'}</tbody></table></div></div>
    </div>
    <div class="card" style="margin-bottom:16px"><div class="card-head"><div><h2>Issues</h2><div class="sub">Most severe first · up to 200</div></div></div><div class="table-wrap tall"><table><thead><tr><th class="l">Severity</th><th class="l">Check</th><th class="l">Symbol</th><th class="l">Date</th><th class="l">Detail</th></tr></thead><tbody>${q.issues.map((i) => `<tr><td class="l">${status(i.severity)}</td><td class="l">${i.check_name.replace(/_/g, " ")}</td><td class="l">${i.symbol ? `<a href="#/stock/${i.symbol}">${i.symbol}</a>` : "–"}</td><td class="l">${i.date || "–"}</td><td class="l muted">${esc(i.detail)}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="card"><div class="card-head"><h2>Ingestion runs</h2></div><div class="table-wrap"><table><thead><tr><th class="l">Run</th><th class="l">Started (UTC)</th><th class="l">Finished</th><th class="l">Mode</th><th class="l">Status</th></tr></thead><tbody>${q.runs.map((r) => `<tr><td class="l">#${r.id}</td><td class="l">${r.started_at}</td><td class="l">${r.finished_at || "–"}</td><td class="l">${r.mode}</td><td class="l">${status(r.status === "success" ? "good" : r.status === "running" ? "info" : "critical", r.status)}</td></tr>`).join("")}</tbody></table></div></div>`;
  root.querySelector("#refresh").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Refreshing…";
    const r = await fetch(`/api/ingest?mode=${root.querySelector("#mode").value}`, { method: "POST" });
    if (!r.ok) { e.target.textContent = "Refresh failed"; return; }
    const poll = setInterval(async () => {
      const s = await fetch("/api/status").then((x) => x.json());
      if (!s.ingest_running) { clearInterval(poll); cache.clear(); await boot(); route(); }
    }, 3000);
  });
}

// ---------------------------------------------------------------- shell

const ROUTES = { overview: viewOverview, screener: viewScreener, stock: viewStock, sectors: viewSectors, risk: viewRisk, macro: viewMacro, quality: viewQuality };

async function route() {
  const [path, qs] = (location.hash.slice(2) || "overview").split("?");
  const [name, arg] = path.split("/");
  const view = ROUTES[name] || viewOverview;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === (name === "stock" ? "screener" : name)));
  const root = document.getElementById("app");
  root.innerHTML = `<div class="loading">Loading</div>`;
  window.scrollTo(0, 0);
  try {
    await view(root, new URLSearchParams(qs || ""), arg && decodeURIComponent(arg));
  } catch (e) {
    root.innerHTML = `<div class="card error">${esc(e.message)}</div>`;
  }
}

async function boot() {
  const st = await api("/api/status", { fresh: true }).catch(() => null);
  const banner = document.getElementById("banner");
  if (!st || !st.ready) {
    banner.innerHTML = `<div class="banner">No data loaded yet. Run <code>python -m ellie ingest</code>, or use Data quality → Refresh data.</div>`;
    return st;
  }
  document.getElementById("asof").textContent = `Data as of ${st.as_of}`;
  banner.innerHTML = st.synthetic
    ? `<div class="banner" role="status"><b>Demo data.</b> Live market sources were unreachable, so prices, macro series and fundamentals are synthetic (constituents are real). Every model and statistic runs identically on live data.</div>`
    : "";
  return st;
}

function setupSearch() {
  const input = document.getElementById("search"), box = document.getElementById("search-results");
  let hits = [], hl = 0;
  const render = () => {
    box.innerHTML = hits.map((r, i) => `<a href="#/stock/${r.symbol}" class="${i === hl ? "hl" : ""}" role="option"><span class="sym">${r.symbol}</span><span class="nm">${esc(r.name)}</span></a>`).join("");
    box.classList.toggle("open", hits.length > 0);
  };
  input.addEventListener("input", async () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { hits = []; render(); return; }
    const rows = await api("/api/screener").catch(() => []);
    hits = rows.filter((r) => r.symbol.toLowerCase().startsWith(q)).concat(rows.filter((r) => !r.symbol.toLowerCase().startsWith(q) && r.name.toLowerCase().includes(q))).slice(0, 8);
    hl = 0; render();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { hl = Math.min(hl + 1, hits.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { hl = Math.max(hl - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter" && hits[hl]) { location.hash = `#/stock/${hits[hl].symbol}`; input.value = ""; hits = []; render(); input.blur(); }
    else if (e.key === "Escape") { hits = []; render(); }
  });
  input.addEventListener("blur", () => setTimeout(() => { hits = []; render(); }, 150));
}

function setupTheme() {
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ellie-theme", next); } catch (e) { /* storage unavailable */ }
    redrawAll();
    // Heatmaps embed computed colors; re-run the view so they pick up the new theme.
    route();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { redrawAll(); route(); });
}

window.addEventListener("hashchange", route);
setupSearch();
setupTheme();
boot().then(route);
