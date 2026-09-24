// Minimal SVG chart kit: line/area with crosshair, horizontal bars, scatter, heatmap.
// Colors come from CSS custom properties so light/dark themes stay in one place.

const NS = "http://www.w3.org/2000/svg";
const registry = new Set();

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
const color = (c) => (c && c.startsWith("--") ? cssVar(c) : c);

function svgEl(tag, attrs = {}, parent) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

// Re-render every live chart on resize or theme change.
const ro = new ResizeObserver((entries) => {
  for (const e of entries) {
    const c = [...registry].find((r) => r.el === e.target);
    if (c && Math.abs(c.width - e.contentRect.width) > 1) c.draw();
  }
});
export function redrawAll() {
  for (const c of registry) {
    if (!document.body.contains(c.el)) { registry.delete(c); ro.unobserve(c.el); continue; }
    c.draw();
  }
}
function mount(el, drawFn) {
  const entry = { el, width: 0, draw: () => { entry.width = el.clientWidth; drawFn(); } };
  for (const r of registry) if (r.el === el) { registry.delete(r); ro.unobserve(el); }
  registry.add(entry);
  ro.observe(el);
  entry.draw();
}

// ---------------------------------------------------------------- scales & ticks

function niceStep(span, count) {
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  return (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
}
function niceTicks(min, max, count = 5) {
  if (min === max) { min -= 1; max += 1; }
  const step = niceStep(max - min, count);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
  return { lo, hi, ticks };
}
const linear = (d0, d1, r0, r1) => (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function dateTicks(t0, t1, width) {
  const days = (t1 - t0) / 864e5;
  const maxTicks = Math.max(2, Math.floor(width / 80));
  const stepsM = [1, 2, 3, 6, 12, 24];
  const out = [];
  if (days < 45) {
    const stepD = Math.ceil(days / maxTicks);
    const d = new Date(t0); d.setUTCHours(0, 0, 0, 0);
    for (let t = d.getTime(); t <= t1; t += stepD * 864e5) if (t >= t0) out.push({ t, label: `${MONTHS[new Date(t).getUTCMonth()]} ${new Date(t).getUTCDate()}` });
    return out;
  }
  const months = days / 30.4;
  const step = stepsM.find((s) => months / s <= maxTicks) || 24;
  const d = new Date(t0);
  let y = d.getUTCFullYear(), m = d.getUTCMonth() + 1;
  if (m > 11) { m = 0; y++; }
  while (m % step !== 0 && step <= 12) { m++; if (m > 11) { m = 0; y++; } }
  for (;;) {
    const t = Date.UTC(y, m, 1);
    if (t > t1) break;
    const label = step >= 12 || m === 0 ? String(y) : `${MONTHS[m]}${step >= 3 ? " " + String(y).slice(2) : ""}`;
    out.push({ t, label });
    m += step; while (m > 11) { m -= 12; y++; }
  }
  return out;
}
const parseDate = (s) => Date.parse(s + "T00:00:00Z");
const fmtDate = (t) => { const d = new Date(t); return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`; };

function tooltip(el) {
  let tt = el.querySelector(":scope > .tooltip");
  if (!tt) { tt = document.createElement("div"); tt.className = "tooltip"; el.appendChild(tt); }
  return {
    show(html, x, y) {
      tt.innerHTML = html;
      tt.classList.add("show");
      const w = tt.offsetWidth, h = tt.offsetHeight, W = el.clientWidth;
      let left = x + 14; if (left + w > W) left = x - w - 14; if (left < 0) left = 0;
      tt.style.left = `${left}px`;
      tt.style.top = `${Math.max(0, y - h / 2)}px`;
    },
    hide() { tt.classList.remove("show"); },
  };
}

function legendHtml(items) {
  return `<div class="legend">${items.map((i) => `<span><span class="swatch ${i.kind || "line"}" style="background:${color(i.color)}"></span>${i.name}</span>`).join("")}</div>`;
}

// ---------------------------------------------------------------- line / area

/**
 * opts: {
 *   series: [{name, dates, values, color}],
 *   bands:  [{name, dates, lo, hi, color}],   // shaded ranges (forecast intervals)
 *   height, yFormat, area (fill under the first series), zeroLine, legend, yMin, yMax
 * }
 */
export function lineChart(el, opts) {
  const height = opts.height || 260;
  const yFmt = opts.yFormat || ((v) => v.toLocaleString());
  const series = opts.series.map((s) => ({ ...s, t: s.dates.map(parseDate) }));
  const bands = (opts.bands || []).map((b) => ({ ...b, t: b.dates.map(parseDate) }));

  mount(el, () => {
    el.innerHTML = "";
    const showLegend = opts.legend ?? (series.length + bands.length > 1);
    if (showLegend) {
      el.insertAdjacentHTML("beforeend", legendHtml([
        ...series.map((s) => ({ name: s.name, color: s.color })),
        ...bands.map((b) => ({ name: b.name, color: b.color, kind: "band" })),
      ]));
    }
    const W = el.clientWidth || 600;
    let ymin = Infinity, ymax = -Infinity, tmin = Infinity, tmax = -Infinity;
    for (const s of series) s.values.forEach((v, i) => { if (v == null) return; ymin = Math.min(ymin, v); ymax = Math.max(ymax, v); tmin = Math.min(tmin, s.t[i]); tmax = Math.max(tmax, s.t[i]); });
    for (const b of bands) b.t.forEach((t, i) => { ymin = Math.min(ymin, b.lo[i]); ymax = Math.max(ymax, b.hi[i]); tmin = Math.min(tmin, t); tmax = Math.max(tmax, t); });
    if (opts.zeroLine) { ymin = Math.min(ymin, 0); ymax = Math.max(ymax, 0); }
    if (opts.yMin != null) ymin = opts.yMin;
    if (opts.yMax != null) ymax = opts.yMax;
    const pad = (ymax - ymin) * 0.04;
    const { lo, hi, ticks } = niceTicks(opts.yMin != null ? ymin : ymin - pad, opts.yMax != null ? ymax : ymax + pad, height < 180 ? 3 : 5);
    const labelW = Math.max(...ticks.map((t) => yFmt(t).length)) * 6.6 + 10;
    const m = { l: labelW, r: 12, t: 8, b: 24 };
    const x = linear(tmin, tmax, m.l, W - m.r);
    const y = linear(lo, hi, height - m.b, m.t);

    const svg = svgEl("svg", { width: W, height, role: "img", "aria-label": opts.label || series.map((s) => s.name).join(", ") }, el);
    const grid = cssVar("--grid"), axis = cssVar("--axis");
    for (const t of ticks) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: t === 0 && opts.zeroLine ? axis : grid, "stroke-width": 1, "shape-rendering": "crispEdges" }, svg);
      const tx = svgEl("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", class: "axis-label" }, svg);
      tx.textContent = yFmt(t);
    }
    svgEl("line", { x1: m.l, x2: W - m.r, y1: height - m.b, y2: height - m.b, stroke: axis, "shape-rendering": "crispEdges" }, svg);
    for (const dt of dateTicks(tmin, tmax, W - m.l - m.r)) {
      const tx = svgEl("text", { x: x(dt.t), y: height - 6, "text-anchor": "middle", class: "axis-label" }, svg);
      tx.textContent = dt.label;
    }

    for (const b of bands) {
      const top = b.t.map((t, i) => `${x(t)},${y(b.hi[i])}`);
      const bot = b.t.map((t, i) => `${x(t)},${y(b.lo[i])}`).reverse();
      svgEl("polygon", { points: [...top, ...bot].join(" "), fill: color(b.color), "fill-opacity": b.opacity ?? 0.14 }, svg);
    }
    series.forEach((s, si) => {
      const pts = [];
      s.values.forEach((v, i) => { if (v != null) pts.push([x(s.t[i]), y(v)]); });
      if (!pts.length) return;
      if (opts.area && si === 0) {
        const base = y(opts.zeroLine ? 0 : lo);
        svgEl("path", { d: `M${pts[0][0]},${base}L${pts.map((p) => p.join(",")).join("L")}L${pts[pts.length - 1][0]},${base}Z`, fill: color(s.color), "fill-opacity": 0.1 }, svg);
      }
      svgEl("path", { d: "M" + pts.map((p) => p.join(",")).join("L"), fill: "none", stroke: color(s.color), "stroke-width": s.width || 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    });

    // Direct label at the end of a single highlighted series.
    if (opts.endLabel && series[0]?.values.length) {
      const s = series[0], i = s.values.length - 1;
      svgEl("circle", { cx: x(s.t[i]), cy: y(s.values[i]), r: 4, fill: color(s.color), stroke: cssVar("--surface"), "stroke-width": 2 }, svg);
    }

    // Crosshair + tooltip.
    const allT = [...new Set([...series.flatMap((s) => s.t), ...bands.flatMap((b) => b.t)])].sort((a, b) => a - b);
    const cross = svgEl("line", { y1: m.t, y2: height - m.b, stroke: axis, "stroke-width": 1, visibility: "hidden" }, svg);
    const dots = [...series, ...bands].map((s) => svgEl("circle", { r: 4, fill: color(s.color), stroke: cssVar("--surface"), "stroke-width": 2, visibility: "hidden" }, svg));
    const tip = tooltip(el);
    const overlay = svgEl("rect", { x: m.l, y: 0, width: W - m.l - m.r, height, fill: "transparent" }, svg);
    const legendOffset = showLegend ? el.querySelector(".legend").offsetHeight + 8 : 0;
    const onMove = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
      const tv = tmin + ((px - m.l) / (W - m.l - m.r)) * (tmax - tmin);
      let lo2 = 0, hi2 = allT.length - 1;
      while (hi2 - lo2 > 1) { const mid = (lo2 + hi2) >> 1; if (allT[mid] < tv) lo2 = mid; else hi2 = mid; }
      const t = Math.abs(allT[lo2] - tv) < Math.abs(allT[hi2] - tv) ? allT[lo2] : allT[hi2];
      cross.setAttribute("x1", x(t)); cross.setAttribute("x2", x(t)); cross.setAttribute("visibility", "visible");
      const rows = [];
      [...series, ...bands].forEach((s, k) => {
        const i = s.t.indexOf(t);
        const dot = dots[k];
        if (i < 0) { dot.setAttribute("visibility", "hidden"); return; }
        if (s.values) {
          const v = s.values[i];
          if (v == null) { dot.setAttribute("visibility", "hidden"); return; }
          dot.setAttribute("cx", x(t)); dot.setAttribute("cy", y(v)); dot.setAttribute("visibility", "visible");
          rows.push(`<div class="tt-row"><span><span class="swatch line" style="background:${color(s.color)}"></span>${s.name}</span><span>${yFmt(v, true)}</span></div>`);
        } else {
          dot.setAttribute("visibility", "hidden");
          rows.push(`<div class="tt-row"><span><span class="swatch band" style="background:${color(s.color)}"></span>${s.name}</span><span>${yFmt(s.lo[i], true)} – ${yFmt(s.hi[i], true)}</span></div>`);
        }
      });
      tip.show(`<div class="tt-title">${fmtDate(t)}</div>${rows.join("")}`, x(t), legendOffset + height / 3);
    };
    const onLeave = () => { cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); tip.hide(); };
    overlay.addEventListener("mousemove", onMove);
    overlay.addEventListener("touchmove", onMove, { passive: true });
    overlay.addEventListener("mouseleave", onLeave);
    overlay.addEventListener("touchend", onLeave);
  });
}

// ---------------------------------------------------------------- horizontal bars

function barPath(x0, x1, yTop, h, r) {
  // Rounded on the data end, square at the baseline.
  const dir = x1 >= x0 ? 1 : -1;
  const len = Math.abs(x1 - x0);
  r = Math.min(r, len, h / 2);
  const xe = x1 - dir * r;
  const sweep = dir > 0 ? 1 : 0;
  return `M${x0},${yTop}H${xe}A${r},${r} 0 0 ${sweep} ${x1},${yTop + r}V${yTop + h - r}A${r},${r} 0 0 ${sweep} ${xe},${yTop + h}H${x0}Z`;
}

/** rows: [{label, value, href?}] — diverging colors by sign. */
export function barChart(el, rows, opts = {}) {
  const fmt = opts.format || ((v) => v.toFixed(2));
  mount(el, () => {
    el.innerHTML = "";
    const W = el.clientWidth || 500;
    const band = 30, bar = 18;
    const narrow = W < 460;
    const labelW = narrow ? 110 : 170;
    const valueW = 58;
    const height = rows.length * band + 8;
    const vals = rows.map((r) => r.value ?? 0);
    const min = Math.min(0, ...vals), max = Math.max(0, ...vals);
    const x = linear(min, max, labelW + (min < 0 ? valueW : 0), W - (max > 0 ? valueW : 8));
    const svg = svgEl("svg", { width: W, height, role: "img", "aria-label": opts.label || "bar chart" }, el);
    const tip = tooltip(el);
    svgEl("line", { x1: x(0), x2: x(0), y1: 0, y2: height - 8, stroke: cssVar("--axis"), "shape-rendering": "crispEdges" }, svg);
    rows.forEach((r, i) => {
      const yTop = i * band + (band - bar) / 2;
      const v = r.value ?? 0;
      const lab = svgEl("text", { x: 0, y: yTop + bar / 2 + 4, class: "axis-label", style: `fill:${cssVar("--text-secondary")};font-size:12px` }, svg);
      lab.textContent = narrow && r.label.length > 16 ? r.label.slice(0, 15) + "…" : r.label;
      const path = svgEl("path", { d: barPath(x(0), x(v), yTop, bar, 4), fill: cssVar(v >= 0 ? "--div-pos" : "--div-neg") }, svg);
      const val = svgEl("text", { x: v >= 0 ? x(v) + 6 : x(v) - 6, y: yTop + bar / 2 + 4, "text-anchor": v >= 0 ? "start" : "end", class: "axis-label", style: `fill:${cssVar("--text-primary")};font-size:12px` }, svg);
      val.textContent = fmt(v);
      const hit = svgEl("rect", { x: 0, y: i * band, width: W, height: band, fill: "transparent", style: r.onClick ? "cursor:pointer" : "" }, svg);
      hit.addEventListener("mousemove", () => { path.setAttribute("fill-opacity", 0.8); tip.show(`<div class="tt-title">${r.label}</div><div class="tt-row"><span>${opts.valueName || "Value"}</span><span>${fmt(v)}</span></div>${r.extra || ""}`, x(v), yTop); });
      hit.addEventListener("mouseleave", () => { path.removeAttribute("fill-opacity"); tip.hide(); });
      if (r.onClick) hit.addEventListener("click", r.onClick);
    });
  });
}

/** Simple vertical-ish importance bars (single series, one hue). */
export function rankBars(el, rows, opts = {}) {
  const fmt = opts.format || ((v) => v.toFixed(2));
  mount(el, () => {
    el.innerHTML = "";
    const W = el.clientWidth || 400, band = 24, bar = 14, labelW = 110, valueW = 48;
    const max = Math.max(...rows.map((r) => r.value), 1e-9);
    const x = linear(0, max, labelW, W - valueW);
    const svg = svgEl("svg", { width: W, height: rows.length * band, role: "img", "aria-label": opts.label || "ranking" }, el);
    rows.forEach((r, i) => {
      const yTop = i * band + (band - bar) / 2;
      const lab = svgEl("text", { x: 0, y: yTop + bar / 2 + 4, class: "axis-label", style: `fill:${cssVar("--text-secondary")};font-size:12px` }, svg);
      lab.textContent = r.label;
      svgEl("path", { d: barPath(x(0), Math.max(x(0) + 1, x(r.value)), yTop, bar, 4), fill: cssVar("--series-1") }, svg);
      const val = svgEl("text", { x: x(r.value) + 6, y: yTop + bar / 2 + 4, class: "axis-label", style: `fill:${cssVar("--text-primary")}` }, svg);
      val.textContent = fmt(r.value);
    });
  });
}

// ---------------------------------------------------------------- scatter

/** points: [{x, y, label, highlight}] ; highlighted points use --series-1, others recede. */
export function scatter(el, points, opts = {}) {
  const fx = opts.xFormat || ((v) => v.toFixed(2));
  const fy = opts.yFormat || ((v) => v.toFixed(2));
  mount(el, () => {
    el.innerHTML = "";
    const W = el.clientWidth || 600, H = opts.height || 340;
    const pts = points.filter((p) => p.x != null && p.y != null);
    const xs = niceTicks(Math.min(...pts.map((p) => p.x)), Math.max(...pts.map((p) => p.x)), 6);
    const ys = niceTicks(Math.min(0, ...pts.map((p) => p.y)), Math.max(...pts.map((p) => p.y)), 5);
    const labelW = Math.max(...ys.ticks.map((t) => fy(t).length)) * 6.6 + 10;
    const m = { l: labelW, r: 12, t: 8, b: 40 };
    const x = linear(xs.lo, xs.hi, m.l, W - m.r);
    const y = linear(ys.lo, ys.hi, H - m.b, m.t);
    const svg = svgEl("svg", { width: W, height: H, role: "img", "aria-label": opts.label || "scatter" }, el);
    const grid = cssVar("--grid");
    for (const t of ys.ticks) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: t === 0 ? cssVar("--axis") : grid, "shape-rendering": "crispEdges" }, svg);
      svgEl("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", class: "axis-label" }, svg).textContent = fy(t);
    }
    for (const t of xs.ticks) svgEl("text", { x: x(t), y: H - m.b + 16, "text-anchor": "middle", class: "axis-label" }, svg).textContent = fx(t);
    svgEl("text", { x: (m.l + W - m.r) / 2, y: H - 4, "text-anchor": "middle", class: "axis-label" }, svg).textContent = opts.xName || "";
    const surface = cssVar("--surface");
    const ordered = [...pts].sort((a, b) => (a.highlight ? 1 : 0) - (b.highlight ? 1 : 0));
    const anyHi = pts.some((p) => p.highlight);
    for (const p of ordered) {
      svgEl("circle", {
        cx: x(p.x), cy: y(p.y), r: p.highlight ? 5 : 4,
        fill: cssVar(p.highlight || !anyHi ? "--series-1" : "--muted-mark"),
        "fill-opacity": p.highlight || !anyHi ? 0.85 : 0.6,
        stroke: surface, "stroke-width": 2,
      }, svg);
    }
    const ring = svgEl("circle", { r: 7, fill: "none", stroke: cssVar("--text-primary"), "stroke-width": 1.5, visibility: "hidden" }, svg);
    const tip = tooltip(el);
    const overlay = svgEl("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent", style: opts.onClick ? "cursor:pointer" : "" }, svg);
    let current = null;
    overlay.addEventListener("mousemove", (ev) => {
      const r = svg.getBoundingClientRect();
      const mx = ev.clientX - r.left, my = ev.clientY - r.top;
      let best = null, bd = 18 * 18;
      for (const p of pts) { const d = (x(p.x) - mx) ** 2 + (y(p.y) - my) ** 2; if (d < bd) { bd = d; best = p; } }
      current = best;
      if (!best) { ring.setAttribute("visibility", "hidden"); tip.hide(); return; }
      ring.setAttribute("cx", x(best.x)); ring.setAttribute("cy", y(best.y)); ring.setAttribute("visibility", "visible");
      tip.show(`<div class="tt-title">${best.label}</div><div class="tt-row"><span>${opts.xName}</span><span>${fx(best.x)}</span></div><div class="tt-row"><span>${opts.yName}</span><span>${fy(best.y)}</span></div>`, x(best.x), y(best.y));
    });
    overlay.addEventListener("mouseleave", () => { ring.setAttribute("visibility", "hidden"); tip.hide(); current = null; });
    if (opts.onClick) overlay.addEventListener("click", () => current && opts.onClick(current));
  });
}

// ---------------------------------------------------------------- heatmap (HTML grid)

function hexToRgb(h) { h = h.replace("#", ""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); }
function mix(a, b, t) { const A = hexToRgb(a), B = hexToRgb(b); return `rgb(${A.map((v, i) => Math.round(v + (B[i] - v) * t)).join(",")})`; }
function luminance(rgb) { const [r, g, b] = rgb.match(/\d+/g).map(Number).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; }

/** Diverging fill: gray midpoint -> blue (positive) / red (negative). */
export function divergingColor(v, maxAbs) {
  if (v == null || !isFinite(v)) return cssVar("--surface-2");
  const t = Math.min(1, Math.abs(v) / (maxAbs || 1));
  return mix(cssVar("--div-mid"), cssVar(v >= 0 ? "--div-pos" : "--div-neg"), t);
}
export function inkFor(bg) {
  if (!bg.startsWith("rgb")) return cssVar("--text-primary");
  return luminance(bg) > 0.36 ? "#0b0b0b" : "#ffffff";
}

/** rows: labels, cols: labels, values[r][c]; diverging around 0 with symmetric domain. */
/** Sequential fill: one hue, light (low) -> dark (high). */
export function sequentialColor(v, lo, hi) {
  if (v == null || !isFinite(v)) return cssVar("--surface-2");
  const t = Math.max(0, Math.min(1, (v - lo) / (hi - lo || 1)));
  return mix(cssVar("--seq-lo"), cssVar("--seq-hi"), t);
}

export function heatmap(el, { rows, cols, values, labels, format, maxAbs, domain, rowWidth = "minmax(90px, 170px)", onRowClick, legendLabels }) {
  mount(el, () => {
    const flat = values.flat().filter((v) => v != null && isFinite(v));
    const M = maxAbs ?? Math.max(...flat.map(Math.abs), 1e-9);
    const minCol = el.clientWidth < 480 ? 34 : 44;
    let html = `<div class="heat-scroll"><div class="heat" style="grid-template-columns:${rowWidth} repeat(${cols.length}, minmax(${minCol}px, 1fr))">`;
    html += `<div></div>` + cols.map((c) => `<div class="hhead">${c}</div>`).join("");
    rows.forEach((r, i) => {
      html += `<div class="hrow" title="${r}" ${onRowClick ? `data-row="${i}" style="cursor:pointer"` : ""}>${r}</div>`;
      cols.forEach((c, j) => {
        const v = values[i][j];
        const bg = domain ? sequentialColor(v, domain[0], domain[1]) : divergingColor(v, M);
        const txt = labels ? labels[i][j] : v == null ? "–" : format(v);
        html += `<div class="hcell" style="background:${bg};color:${inkFor(bg)}" title="${r} · ${c}: ${txt}">${txt}</div>`;
      });
    });
    html += `</div></div>`;
    const [lo, hi] = legendLabels || (domain ? domain.map(format) : [format(-M), format(M)]);
    const grad = domain ? `${cssVar("--seq-lo")}, ${cssVar("--seq-hi")}` : `${cssVar("--div-neg")}, ${cssVar("--div-mid")}, ${cssVar("--div-pos")}`;
    html += `<div class="scale"><span>${lo}</span><span class="bar" style="background:linear-gradient(90deg, ${grad})"></span><span>${hi}</span></div>`;
    el.innerHTML = html;
    if (onRowClick) el.querySelectorAll("[data-row]").forEach((d) => d.addEventListener("click", () => onRowClick(+d.dataset.row)));
  });
}
