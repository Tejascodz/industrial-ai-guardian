// Guardian control room: live client.
// All data arrives over one authenticated WebSocket. Every string that came
// from the server is inserted with textContent, never as HTML.

const uPlot = window.uPlot;
const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const MAX_POINTS = 480;
const ANALYZER_LAG = 5; // samples: the product analyzer reports 15 min late
const PRIO = { 1: "critical", 2: "high", 3: "medium", 4: "advisory" };

const app = {
  meta: null, tags: {}, points: [], state: null, control: null,
  selected: "xmeas_9", reveal: false, ws: null, req: 0, pending: new Map(),
  alarmKey: "", charts: {}, built: false,
};

// ----- token: taken from the start-up link once, then kept for this tab only ----
let token = new URLSearchParams(location.search).get("token");
try {
  if (token) sessionStorage.setItem("guardian-token", token);
  else token = sessionStorage.getItem("guardian-token");
} catch { /* storage unavailable: the token lives in memory only */ }
if (location.search) history.replaceState(null, "", "/");

// ----- formatting --------------------------------------------------------------
const UNIT = { degC: "°C", "m3/h": "m³/h", "kPa g": "kPa", "%": "%", kscmh: "kscmh", "kg/h": "kg/h", kW: "kW", "mol%": "mol%" };
function fmt(v) {
  if (v == null || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  const d = a >= 1000 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3;
  return v.toFixed(d);
}
function clock(h) {
  if (h == null) return "–";
  const day = Math.floor(h / 24) + 1;
  const m = Math.round((h % 24) * 60);
  const hh = String(Math.floor(m / 60) % 24).padStart(2, "0");
  const mm = String(m % 60).padStart(2, "0");
  return `Day ${day}, ${hh}:${mm}`;
}
const hhmm = (h) => clock(h).split(", ")[1];
const tagName = (id) => app.tags[id] ? `${app.tags[id].isa} ${app.tags[id].name.toLowerCase()}` : id;
const unitOf = (id) => UNIT[app.tags[id]?.unit] ?? app.tags[id]?.unit ?? "";

function el(tag, attrs = {}, text) {
  const n = tag.startsWith("svg:") ? document.createElementNS(SVG, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text != null) n.textContent = text;
  return n;
}

// ----- connection ------------------------------------------------------------------
function connect(delay = 0) {
  setLink("Connecting", "connecting");
  if (!token) { setLink("No access token: open the link printed by the server", "down"); return; }
  setTimeout(() => {
    const ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
    app.ws = ws;
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = (e) => {
      if (e.code === 1008) { setLink("Access refused: token or origin not accepted", "down"); return; }
      setLink("Reconnecting", "down");
      connect(Math.min(5000, delay * 2 + 500));
    };
  }, delay);
}

function send(cmd) {
  if (!app.ws || app.ws.readyState !== 1) { toast("Not connected to the plant", false); return Promise.resolve(null); }
  const req = ++app.req;
  app.ws.send(JSON.stringify({ ...cmd, req }));
  return new Promise((resolve) => app.pending.set(req, resolve));
}

function onMessage(m) {
  if (m.type === "hello" || m.type === "snapshot") {
    if (m.meta) init(m.meta);
    app.points = (m.series || []).slice(-MAX_POINTS);
    if (m.control) setControl(m.control);
    render(m.state);
  } else if (m.type === "tick") {
    const p = m.state?.point;
    if (p && (!app.points.length || p.t > app.points[app.points.length - 1].t)) {
      app.points.push(p);
      if (app.points.length > MAX_POINTS) app.points.shift();
    }
    render(m.state);
  } else if (m.type === "result") {
    if (m.control) setControl(m.control);
    toast(m.message, m.ok);
    app.pending.get(m.req)?.(m);
    app.pending.delete(m.req);
  } else if (m.type === "audit") {
    renderAudit(m);
    app.pending.get(m.req)?.(m);
    app.pending.delete(m.req);
  }
}

function setLink(text, state) {
  $("linkText").textContent = text;
  $("link").dataset.state = state;
}

function setControl(c) {
  app.control = c;
  app.reveal = Boolean(c.reveal);
  $("reveal").checked = app.reveal;
  const sel = $("speed");
  if (!sel.options.length) {
    for (const v of c.speeds) sel.append(el("option", { value: v }, `${Math.round(v * 180)}× real time`));
  }
  sel.value = String(c.speed);
  $("runBtn").textContent = c.running ? "Pause" : "Run";
  $("runBtn").setAttribute("aria-pressed", String(c.running));
  if (app.state && !app.state.trip) setLink(c.running ? "Live" : "Paused", c.running ? "live" : "paused");
}

// ----- one-time setup ------------------------------------------------------------------
function init(meta) {
  app.meta = meta;
  for (const t of meta.tags) app.tags[t.id] = t;
  if (app.built) return;
  app.built = true;
  buildFlowsheet();
  buildTrendPicker();
  buildCharts();
  buildTrainer();
  wireChrome();
}

// ----- process flow diagram ---------------------------------------------------------------
// Geometry of a simplified Tennessee Eastman flowsheet (viewBox 1180 x 500).
const PIPES = [
  "M20,150 H240", "M20,200 H240", "M20,250 H240", "M240,150 V300", "M240,300 H330",       // fresh feeds
  "M300,70 V300",                                                                          // recycle into reactor feed
  "M450,220 H580", "M660,220 H730",                                                        // reactor -> condenser -> separator
  "M780,150 V70 H668", "M632,70 H300",                                                     // separator vapour -> compressor -> recycle
  "M780,70 V34 H1160",                                                                     // purge
  "M830,300 H875 V190 H920",                                                               // separator liquid -> stripper
  "M960,140 V128 H500 V70",                                                                // stripper overhead joins recycle
  "M1160,300 H1000", "M960,400 V448 H1160",                                                // stream 4 in, product out
];
const UTIL = ["M1160,376 H1000", "M450,345 H640", "M640,372 H450", "M605,290 V236", "M635,236 V290"];
const ARROWS = [[330, 300, 0], [580, 220, 0], [730, 220, 0], [920, 190, 0], [1160, 34, 0], [1160, 448, 0],
  [1000, 300, 180], [1000, 376, 180], [300, 300, 90]];
const LABELS = [
  ["A feed", 20, 142], ["D feed", 20, 192], ["E feed", 20, 242], ["Purge", 1114, 26], ["Product", 1112, 440],
  ["A and C feed", 1080, 318], ["Steam", 1118, 394], ["Recycle", 306, 62], ["Cooling water", 470, 390],
];
const EQUIP = [
  { kind: "rect", x: 330, y: 160, w: 120, h: 230, rx: 46, label: "Reactor", lx: 390, ly: 414 },
  { kind: "rect", x: 580, y: 204, w: 80, h: 32, rx: 6, label: "Condenser", lx: 620, ly: 196 },
  { kind: "rect", x: 730, y: 150, w: 100, h: 180, rx: 34, label: "Separator", lx: 780, ly: 352 },
  { kind: "rect", x: 920, y: 140, w: 80, h: 260, rx: 30, label: "Stripper", lx: 900, ly: 424 },
  { kind: "circle", cx: 650, cy: 70, r: 18, label: "Compressor", lx: 650, ly: 112 },
];
// [x, y, where the reading goes: r(ight), l(eft), b(elow)]
const INST = {
  xmeas_1: [125, 150, "r"], xmeas_2: [125, 200, "r"], xmeas_3: [125, 250, "r"], xmeas_6: [270, 300, "b"],
  xmeas_5: [420, 70, "b"], xmeas_7: [390, 192, "b"], xmeas_9: [390, 264, "b"], xmeas_8: [390, 336, "b"],
  xmeas_21: [600, 318, "r"], xmeas_22: [668, 268, "b"], xmeas_20: [650, 30, "r"], xmeas_13: [780, 100, "r"],
  xmeas_11: [780, 188, "b"], xmeas_12: [780, 264, "b"], xmeas_10: [920, 34, "b"], xmeas_14: [852, 340, "b"],
  xmeas_16: [960, 178, "b"], xmeas_15: [960, 252, "b"], xmeas_18: [960, 326, "b"], xmeas_4: [1060, 275, "r"],
  xmeas_19: [1120, 346, "l"], xmeas_17: [1010, 424, "r"], xmeas_38: [1000, 478, "r"],
};
const VALVES = { xmv_3: [65, 150], xmv_1: [65, 200], xmv_2: [65, 250], xmv_4: [1035, 300], xmv_6: [850, 34],
  xmv_7: [852, 300], xmv_8: [1110, 448], xmv_10: [560, 372], xmv_11: [605, 268] };

function buildFlowsheet() {
  const svg = $("flowsheet");
  for (const d of PIPES) svg.append(el("svg:path", { d, class: "pipe" }));
  for (const d of UTIL) svg.append(el("svg:path", { d, class: "pipe-util" }));
  for (const [x, y, a] of ARROWS) {
    svg.append(el("svg:path", { d: "M0,-4 L7,0 L0,4 Z", class: "arrow", transform: `translate(${x - 7},${y}) rotate(${a} 7 0)` }));
  }
  for (const e of EQUIP) {
    const shape = e.kind === "rect"
      ? el("svg:rect", { x: e.x, y: e.y, width: e.w, height: e.h, rx: e.rx, class: "vessel" })
      : el("svg:circle", { cx: e.cx, cy: e.cy, r: e.r, class: "vessel" });
    svg.append(shape, el("svg:text", { x: e.lx, y: e.ly, class: "equip-label", "text-anchor": "middle" }, e.label));
  }
  svg.append(el("svg:path", { d: "M638,58 L662,64 L662,76 L638,82 Z", class: "pipe" }));
  for (const [t, x, y] of LABELS) svg.append(el("svg:text", { x, y, class: "stream-label" }, t));
  for (const [id, [x, y]] of Object.entries(VALVES)) {
    const g = el("svg:g", { class: "valve", transform: `translate(${x},${y})` });
    g.append(el("svg:path", { d: "M-8,-5 L8,5 L8,-5 L-8,5 Z", class: "vessel" }));
    g.append(el("svg:text", { x: 0, y: 17, class: "stream-label", "text-anchor": "middle", "data-valve": id }, ""));
    svg.append(g);
  }
  for (const [id, [x, y, side]] of Object.entries(INST)) {
    const t = app.tags[id];
    if (!t) continue;
    const [fn, no] = t.isa.split("-");
    const g = el("svg:g", { class: "inst", tabindex: 0, role: "button", "data-tag": id, transform: `translate(${x},${y})` });
    g.append(el("svg:circle", { class: "halo", r: 21 }));
    g.append(el("svg:rect", { class: "box", x: -16, y: -16, width: 32, height: 32 }));
    g.append(el("svg:circle", { class: "disc", r: 14.5 }));
    g.append(el("svg:line", { class: "bar", x1: -14.5, x2: 14.5, y1: 0, y2: 0 }));
    g.append(el("svg:text", { class: "fn", y: -3.5 }, fn));
    g.append(el("svg:text", { class: "no", y: 10 }, no));
    const pos = side === "r" ? { x: 22, y: -1, a: "start" } : side === "l" ? { x: -22, y: -1, a: "end" } : { x: 0, y: 30, a: "middle" };
    const val = el("svg:text", { x: pos.x, y: pos.y, "text-anchor": pos.a });
    val.append(el("svg:tspan", { class: "val" }, "–"), el("svg:tspan", { class: "unit", dx: 3 }, unitOf(id)));
    const virt = el("svg:text", { class: "virt", x: pos.x, y: pos.y + 15, "text-anchor": pos.a }, "");
    g.append(val, virt);
    const pick = () => select(id === "xmeas_38" ? app.selected : id);
    g.addEventListener("click", pick);
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    svg.append(g);
  }
}

function updateFlowsheet(s, p) {
  const gd = s.guardian || {};
  // Only Guardian's suspect gets the ring; tags that merely look odd because
  // their models use the suspect's reading are listed under agreement instead.
  const doubt = new Set(gd.cls === "sensor_fault" && gd.tag ? [gd.tag] : []);
  const alarm = (s.alarms || []).find((a) => a.state.startsWith("active"));
  const truthTags = new Set(app.reveal ? (s.truth || []).flatMap((e) => truthTagsOf(e)) : []);
  for (const g of document.querySelectorAll(".inst")) {
    const id = g.dataset.tag;
    const v = id === "xmeas_38" ? p?.ss?.analyzer : p?.meas?.[id];
    g.querySelector(".val").textContent = fmt(v);
    const isDoubt = doubt.has(id);
    g.dataset.doubt = String(isDoubt);
    g.dataset.selected = String(id === app.selected);
    g.dataset.alarm = alarm && alarm.tag === id ? PRIO[alarm.priority] : "";
    g.dataset.truth = String(truthTags.has(id));
    const showVirt = id !== "xmeas_38" && (isDoubt || id === app.selected) && p?.virtual?.[id] != null;
    g.querySelector(".virt").textContent = showVirt ? `model ${fmt(p.virtual[id])}` : "";
    g.setAttribute("aria-label", `${app.tags[id].isa} ${app.tags[id].name}, ${fmt(v)} ${unitOf(id)}${isDoubt ? ", Guardian doubts this reading" : ""}`);
  }
  for (const t of document.querySelectorAll("[data-valve]")) {
    const v = p?.meas?.[t.dataset.valve];
    t.textContent = v == null ? "" : `${v.toFixed(0)} %`;
  }
}

function truthTagsOf(e) {
  if (e.tag) return [e.tag];
  const map = {
    replay_cooling_small: ["xmeas_9", "xmeas_21"], replay_cooling_large: ["xmeas_9", "xmeas_21"],
    replay_a_feed: ["xmeas_1"], replay_purge: ["xmeas_7", "xmeas_10"], replay_stripper_product: ["xmeas_17"],
  };
  return map[e.preset] || [];
}

// ----- trends ---------------------------------------------------------------------------------
function buildTrendPicker() {
  const sel = $("trendTag");
  for (const t of app.meta.tags.filter((x) => x.kind === "transmitter")) {
    sel.append(el("option", { value: t.id }, `${t.isa}  ${t.name}`));
  }
  sel.value = app.selected;
  sel.addEventListener("change", () => select(sel.value));
}

function select(id) {
  app.selected = id;
  $("trendTag").value = id;
  if (app.state) render(app.state);
}

function tooltipPlugin(rows) {
  let tip;
  return {
    hooks: {
      init: (u) => { tip = el("div", { class: "tip" }); u.over.append(tip); },
      setCursor: (u) => {
        const i = u.cursor.idx;
        if (i == null || u.data[0][i] == null) { tip.style.display = "none"; return; }
        tip.replaceChildren(el("div", {}, clock(u.data[0][i])));
        for (const r of rows) {
          const v = u.data[r.series][i];
          if (v == null || !u.series[r.series].show) continue;
          const row = el("div", { class: "row" });
          const k = el("span", { class: "k" });
          k.style.borderTopColor = r.color();
          const strong = el("strong", {}, `${fmt(v)} ${r.unit()}`);
          row.append(k, strong, el("span", {}, r.label));
          tip.append(row);
        }
        tip.style.display = "block";
        const left = u.cursor.left, w = tip.offsetWidth;
        tip.style.left = `${left + 14 + w > u.over.clientWidth ? left - w - 14 : left + 14}px`;
        tip.style.top = `${Math.max(0, u.cursor.top - 10)}px`;
      },
    },
  };
}

function alarmBands(u) {
  const { ctx } = u;
  const xs = u.data[0];
  const pts = app.points;
  const top = u.bbox.top, h = u.bbox.height;
  ctx.save();
  const colors = { integrity_attack: css("--critical"), process_fault: css("--high"), sensor_fault: css("--guardian"), regime_change: css("--advisory") };
  let i = 0;
  while (i < pts.length) {
    if (!pts[i].alarm) { i++; continue; }
    let j = i;  // one band per stretch with the same diagnosis
    while (j + 1 < pts.length && pts[j + 1].alarm && pts[j + 1].cls === pts[i].cls) j++;
    const x0 = u.valToPos(xs[i], "x", true), x1 = u.valToPos(xs[Math.min(j + 1, xs.length - 1)], "x", true);
    ctx.globalAlpha = 0.13;
    ctx.fillStyle = colors[pts[i].cls] || css("--high");
    ctx.fillRect(x0, top, Math.max(2, x1 - x0), h);
    i = j + 1;
  }
  if (app.reveal && app.state?.truth) {
    ctx.globalAlpha = 0.9;
    ctx.strokeStyle = css("--truth");
    ctx.setLineDash([4, 4]);
    for (const e of app.state.truth) {
      if (e.t < xs[0]) continue;
      const x = u.valToPos(e.t, "x", true);
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + h); ctx.stroke();
    }
  }
  ctx.restore();
}

function axisStyle() {
  return {
    stroke: css("--ink-3"), grid: { stroke: css("--rule-soft"), width: 1 }, ticks: { stroke: css("--rule"), width: 1, size: 4 },
    font: `12px ${css("--f-dense")}`,
  };
}

function buildCharts() {
  const base = (el_, series, rows) => {
    const w = el_.clientWidth || 500;
    return new uPlot({
      width: w, height: 210, series, legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 6 } },
      scales: { x: { time: false } },
      axes: [
        { ...axisStyle(), values: (u, vals) => vals.map((v) => hhmm(v)), space: 70 },
        { ...axisStyle(), size: 58, values: (u, vals) => {
          const step = vals.length > 1 ? Math.abs(vals[1] - vals[0]) : 1;
          const d = Math.min(4, Math.max(0, -Math.floor(Math.log10(step || 1))));
          return vals.map((v) => v.toFixed(d));
        } },
      ],
      hooks: { drawAxes: [alarmBands] },
      plugins: [tooltipPlugin(rows)],
    }, [[], ...series.slice(1).map(() => [])], el_);
  };
  const tagSeries = [
    {},
    { label: "Transmitter", stroke: css("--measured"), width: 1.6 },
    { label: "Guardian model", stroke: css("--guardian"), width: 1.6 },
    { label: "True value", stroke: css("--truth"), width: 1.6, dash: [6, 4], show: false },
  ];
  app.charts.tag = base($("chartTag"), tagSeries, [
    { series: 1, label: "transmitter", color: () => css("--measured"), unit: () => unitOf(app.selected) },
    { series: 2, label: "Guardian model", color: () => css("--guardian"), unit: () => unitOf(app.selected) },
    { series: 3, label: "true value", color: () => css("--truth"), unit: () => unitOf(app.selected) },
  ]);
  const ssSeries = [
    {},
    { label: "Soft sensor", stroke: css("--measured"), width: 1.6 },
    { label: "Protected", stroke: css("--guardian"), width: 1.6 },
    { label: "Analyzer", stroke: css("--ink"), width: 0, paths: () => null, points: { show: true, size: 6, fill: css("--ink"), stroke: css("--ink") } },
  ];
  app.charts.ss = base($("chartSS"), ssSeries, [
    { series: 1, label: "soft sensor", color: () => css("--measured"), unit: () => "mol%" },
    { series: 2, label: "protected", color: () => css("--guardian"), unit: () => "mol%" },
    { series: 3, label: "analyzer", color: () => css("--ink"), unit: () => "mol%" },
  ]);
  legend($("legendTag"), [["Transmitter", "--measured", ""], ["Guardian model (from all other tags)", "--guardian", ""], ["Alarm period, shaded by diagnosis", "--ink-3", "band"]]);
  legend($("legendSS"), [["Soft sensor", "--measured", ""], ["Guardian-protected", "--guardian", ""], ["Analyzer sample", "--ink", "dot"]]);
  new ResizeObserver(() => {
    for (const [k, id] of [["tag", "chartTag"], ["ss", "chartSS"]]) {
      const w = $(id).clientWidth;
      if (w && app.charts[k].width !== w) app.charts[k].setSize({ width: w, height: 210 });
    }
  }).observe($("chartTag").parentElement.parentElement);
}

function legend(ul, items) {
  ul.replaceChildren();
  for (const [label, color, kind] of items) {
    const k = el("span", { class: `key ${kind}` });
    if (kind === "dot" || kind === "band") k.style.background = css(color);
    else k.style.borderTopColor = css(color);
    if (kind === "band") k.style.opacity = "0.45";
    ul.append(el("li", {}, null));
    ul.lastChild.append(k, document.createTextNode(label));
  }
}

function updateCharts() {
  const pts = app.points;
  const x = pts.map((p) => p.t);
  const id = app.selected;
  app.charts.tag.setSeries(3, { show: app.reveal });
  app.charts.tag.setData([x, pts.map((p) => p.meas[id]), pts.map((p) => p.virtual[id]), pts.map((p) => p.true?.[id] ?? null)]);
  const analyzer = pts.map((_, j) => {
    const a = pts[j + ANALYZER_LAG], b = pts[j + ANALYZER_LAG - 1];
    return a && b && a.ss.analyzer !== b.ss.analyzer ? a.ss.analyzer : null;
  });
  const prot = pts.map((p) => (p.ss.status === "reconstructed" ? p.ss.protected : null));
  app.charts.ss.setData([x, pts.map((p) => p.ss.raw), prot, analyzer]);
  $("trendTitle").textContent = `${tagName(id)} (${unitOf(id)})`;
  const items = [["Transmitter", "--measured", ""], ["Guardian model (from all other tags)", "--guardian", ""], ["Alarm period, shaded by diagnosis", "--ink-3", "band"]];
  if (app.reveal) items.push(["True value (trainer)", "--truth", "dash"]);
  if (app.legendReveal !== app.reveal) { legend($("legendTag"), items); app.legendReveal = app.reveal; }
}

// ----- Guardian panel ------------------------------------------------------------------------------
const ICONS = {
  normal: '<circle cx="10" cy="10" r="7.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6.5 10.2 L9 12.6 L13.6 7.6" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  sensor_fault: '<rect x="2" y="2" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="10" cy="10" r="6.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3 2"/>',
  process_fault: '<path d="M10 2.5 L18 17 H2 Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M10 8 V12.2 M10 14.3 V14.9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  regime_change: '<circle cx="10" cy="10" r="7.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10 9 V14 M10 6.2 V6.8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  integrity_attack: '<path d="M10 2 L17 5 V10 C17 14 14 16.8 10 18 C6 16.8 3 14 3 10 V5 Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M10 6.8 V11 M10 13.2 V13.8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
};
const ICON_COLOR = { normal: "--ink-3", sensor_fault: "--guardian", process_fault: "--high", regime_change: "--advisory", integrity_attack: "--critical" };

function updateGuardian(s, p) {
  const g = s.guardian || {};
  const cls = s.trip ? "integrity_attack" : (g.cls || "normal");
  $("verdict").dataset.cls = cls;
  const icon = $("verdictIcon");
  icon.innerHTML = ICONS[cls] || ICONS.normal; // static markup from this file only
  icon.style.color = css(ICON_COLOR[cls] || "--ink-3");
  $("verdictText").textContent = s.trip ? "Plant tripped" : (g.verdict || "Normal operation");
  $("verdictAction").textContent = s.trip ? "The plant's own interlocks shut it down. Restart it from the Trainer panel." : (g.action || "");
  $("since").textContent = g.alarm && g.since != null ? `since ${clock(g.since)}` : "";
  const ul = $("evidence");
  ul.replaceChildren();
  if (g.evidence?.length) for (const e of g.evidence) ul.append(el("li", {}, e));
  else ul.append(el("li", { class: "muted" }, "No deviations beyond their limits."));

  const ss = p?.ss;
  const status = ss?.status || "ok";
  $("ssValue").textContent = ss ? (status === "reconstructed" ? ss.protected : ss.raw).toFixed(3) : "–";
  $("ssStatus").dataset.state = status;
  $("ssStatus").textContent = { ok: "Trusted", reconstructed: "Repaired", untrusted: "Untrusted" }[status];
  $("ssNote").textContent = {
    ok: "Lab-grade analyzer AI-138 reports every 15 min, 15 min late.",
    reconstructed: `Running on Guardian's model value for ${g.isa || "the faulty transmitter"} instead of the transmitter.`,
    untrusted: "The plant has moved away from what the model knows. Wait for the analyzer before acting on this value.",
  }[status];

  const list = $("agreement");
  list.replaceChildren();
  const top = Object.entries(g.health || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const scale = (r) => Math.min(1, Math.log10(1 + r) / Math.log10(21));
  for (const [id, r] of top) {
    const li = el("li", { "data-over": String(r > 1), tabindex: 0, role: "button" });
    li.setAttribute("aria-label", `${app.tags[id]?.isa}: ${r.toFixed(1)} times its limit. Select to trend.`);
    const meter = el("span", { class: "meter" });
    const fill = el("span", { class: "fill" });
    fill.style.width = `${(scale(r) * 100).toFixed(1)}%`;
    const lim = el("span", { class: "limit" });
    lim.style.left = `${(scale(1) * 100).toFixed(1)}%`;
    meter.append(fill, lim);
    li.append(el("span", {}, app.tags[id]?.isa || id), meter, el("span", { class: "num" }, r >= 20 ? ">20" : r.toFixed(1)));
    li.addEventListener("click", () => select(id));
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") select(id); });
    list.append(li);
  }
}

// ----- alarms ----------------------------------------------------------------------------------------
const PRIO_ICON = {
  critical: '<path d="M7 1 L13 7 L7 13 L1 7 Z" fill="currentColor"/>',
  high: '<path d="M7 1.5 L13 12.5 H1 Z" fill="currentColor"/>',
  medium: '<rect x="2" y="2" width="10" height="10" fill="currentColor"/>',
  advisory: '<circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" stroke-width="2"/>',
};
const STATE_TEXT = { active_unacked: "Active, not acknowledged", active_acked: "Active", rtn_unacked: "Cleared, not acknowledged", cleared: "Cleared" };

function updateAlarms(s) {
  const alarms = s.alarms || [];
  const key = JSON.stringify(alarms);
  if (key === app.alarmKey) return;
  app.alarmKey = key;
  const tbody = $("alarmRows");
  tbody.replaceChildren();
  for (const a of alarms) {
    const tr = el("tr", { "data-state": a.state });
    const prio = PRIO[a.priority] || "high";
    const pr = el("span", { class: "prio" });
    const icon = el("svg:svg", { viewBox: "0 0 14 14", "aria-hidden": "true" });
    icon.innerHTML = PRIO_ICON[prio];
    icon.style.color = css(`--${prio}`);
    pr.append(icon, document.createTextNode(a.priorityName));
    const ack = el("td");
    if (a.state.endsWith("unacked")) {
      const b = el("button", { class: "btn btn-quiet", type: "button" }, "Acknowledge");
      b.addEventListener("click", () => send({ cmd: "ack", id: a.id }));
      ack.append(b);
    }
    tr.append(el("td"), el("td", { class: "time" }, clock(a.tOn)), el("td", { class: "tagcell" }, a.isa || "Unit 100"),
      el("td", { class: "msg" }, a.message), el("td", { class: "state-cell" }, STATE_TEXT[a.state] || a.state), ack);
    tr.firstChild.append(pr);
    tbody.append(tr);
  }
  $("alarmEmpty").hidden = alarms.length > 0;
  const unacked = alarms.filter((a) => a.state.endsWith("unacked")).length;
  $("alarmCount").textContent = alarms.length ? `${alarms.length} shown, ${unacked} not acknowledged` : "";
  $("ackAll").disabled = unacked === 0;
}

// ----- render -----------------------------------------------------------------------------------------
function render(s) {
  if (!s || !app.meta) return;
  app.state = s;
  const g = s.guardian || {};
  const active = (s.alarms || []).find((a) => a.state.startsWith("active"));
  if (active && g.cls === "sensor_fault" && g.tag && app.followed !== `${active.id}:${g.tag}`) {
    app.followed = `${active.id}:${g.tag}`;  // bring the suspect into the trend once per diagnosis
    app.selected = g.tag;
    $("trendTag").value = g.tag;
  }
  const p = s.point || app.points[app.points.length - 1];
  $("clock").textContent = clock(s.t);
  const trip = $("trip");
  trip.hidden = !s.trip;
  if (s.trip) {
    trip.textContent = `Plant tripped: ${s.trip} Restart it from the Trainer panel.`;
    setLink("Tripped", "down");
  } else if (app.control) {
    setLink(app.control.running ? "Live" : "Paused", app.control.running ? "live" : "paused");
  }
  $("runBtn").disabled = Boolean(s.trip);
  updateFlowsheet(s, p);
  updateGuardian(s, p);
  updateCharts();
  updateAlarms(s);
  updateTrainerLists(s);
}

// ----- trainer ------------------------------------------------------------------------------------------
function buildTrainer() {
  const m = app.meta;
  const fTag = $("faultTag");
  for (const t of m.tags.filter((x) => x.kind === "transmitter")) fTag.append(el("option", { value: t.id }, `${t.isa}  ${t.name}`));
  fTag.value = "xmeas_18";
  const sizeDefaults = () => {
    const t = app.tags[fTag.value], kind = $("faultKind").value;
    const u = t.faultUnit;
    $("faultMag").value = kind === "stuck" ? "" : String(+(kind === "drift" ? u : kind === "noise" ? u : 2 * u).toPrecision(3));
    $("faultMag").disabled = kind === "stuck";
    $("faultUnit").textContent = unitOf(t.id) + (kind === "drift" ? " per h" : kind === "noise" ? " std. dev." : "");
  };
  fTag.addEventListener("change", sizeDefaults);
  $("faultKind").addEventListener("change", sizeDefaults);
  sizeDefaults();
  $("fFault").addEventListener("submit", (e) => {
    e.preventDefault();
    const kind = $("faultKind").value;
    send({ cmd: "fault", tag: fTag.value, kind, magnitude: kind === "stuck" ? 0 : Number($("faultMag").value) });
  });
  $("clearFaults").addEventListener("click", () => send({ cmd: "clear_faults" }));

  const dist = $("distSel");
  for (const d of m.disturbances) dist.append(el("option", { value: d.id }, `IDV(${d.id}) ${d.label}`));
  dist.value = "4";
  $("fDist").addEventListener("submit", (e) => { e.preventDefault(); send({ cmd: "disturbance", idv: Number(dist.value), on: true }); });

  const atk = $("attackSel");
  for (const a of m.attacks) atk.append(el("option", { value: a.id }, a.label));
  const jitterOk = () => { $("attackJitter").disabled = !atk.value.startsWith("replay"); };
  atk.addEventListener("change", jitterOk);
  jitterOk();
  $("fAttack").addEventListener("submit", (e) => {
    e.preventDefault();
    send({ cmd: "attack", preset: atk.value, jitter: $("attackJitter").checked && !$("attackJitter").disabled });
  });
  $("stopAttacks").addEventListener("click", () => send({ cmd: "stop_attacks" }));

  const sp = $("spSel");
  for (const s of m.setpoints) sp.append(el("option", { value: s.id }, s.label));
  const spInfo = () => {
    const s = m.setpoints.find((x) => x.id === sp.value);
    const cur = app.state?.sp?.[s.id] ?? s.base;
    $("spVal").value = String(cur);
    $("spVal").min = s.min; $("spVal").max = s.max;
    $("spUnit").textContent = s.unit;
    $("spHelp").textContent = s.envelope
      ? `Validated range ${s.envelope[0]}–${s.envelope[1]} ${s.unit}. Allowed ${s.min}–${s.max}.`
      : `Validated only at ${s.base} ${s.unit}. Allowed ${s.min}–${s.max}.`;
  };
  sp.addEventListener("change", spInfo);
  app.spInfo = spInfo;
  $("fSp").addEventListener("submit", (e) => { e.preventDefault(); send({ cmd: "setpoint", name: sp.value, value: Number($("spVal").value) }); });

  // The server decides: until it agrees to reveal, it does not send ground truth at all.
  $("reveal").addEventListener("change", (e) => send({ cmd: "reveal", on: e.target.checked }));
  $("fPlant").addEventListener("submit", (e) => {
    e.preventDefault();
    const seed = Number($("seed").value);
    if (!Number.isInteger(seed) || seed < 1) { toast("Enter a whole number seed from 1 to 1000000", false); return; }
    app.points = [];
    send({ cmd: "reset", seed });
  });
}

function updateTrainerLists(s) {
  if (!$("seed").value) $("seed").value = String((s.seed || 7) + 1);
  if (app.spInfo && !app.spPrimed) { app.spInfo(); app.spPrimed = true; }
  const dist = $("distActive");
  dist.replaceChildren();
  for (const e of (s.truth || []).filter((x) => x.kind === "disturbance")) {
    const li = el("li", {}, null);
    li.append(el("span", {}, e.label));
    const b = el("button", { class: "btn btn-quiet", type: "button" }, "Stop");
    b.addEventListener("click", () => send({ cmd: "disturbance", idv: e.idv, on: false }));
    li.append(b);
    dist.append(li);
  }
  const truth = $("truthList");
  truth.replaceChildren();
  if (app.reveal) {
    const items = s.truth || [];
    if (!items.length) truth.append(el("li", { class: "muted" }, "Nothing injected right now."));
    for (const e of items) {
      const li = el("li");
      const span = el("span");
      span.append(el("span", { class: "t" }, clock(e.t)), document.createTextNode(e.label));
      li.append(span);
      truth.append(li);
    }
  }
}

// ----- audit ------------------------------------------------------------------------------------------------
function describeAudit(r) {
  const e = r.event || {};
  if (e.alarm) return `Alarm ${e.alarm.replaceAll("_", " ")}: ${e.message}${e.isa ? ` (${e.isa})` : ""}`;
  if (e.action === "trainer") return `Trainer: ${e.label || e.kind.replaceAll("_", " ")}`;
  if (e.action === "session_start") return `Plant started (seed ${e.seed})`;
  if (e.action) return `Operator: ${e.action}`;
  return JSON.stringify(e);
}

function renderAudit(m) {
  const chain = $("chain");
  chain.dataset.ok = String(m.ok);
  chain.textContent = m.ok
    ? `Chain verified: every record is signed and linked to the one before it (${m.path}).`
    : `Chain broken at record ${m.firstBad}: the log was edited after it was written.`;
  const list = $("auditList");
  list.replaceChildren();
  for (const r of m.records) {
    const li = el("li");
    const body = el("div");
    body.append(el("div", { class: "what" }, describeAudit(r)), el("div", { class: "hash" }, `${new Date(r.ts).toLocaleString()}  ${r.hash.slice(0, 16)}…`));
    li.append(el("span", { class: "seq" }, `#${r.seq}`), body);
    list.append(li);
  }
}

// ----- chrome: header, drawers, toasts -------------------------------------------------------------------------
function wireChrome() {
  $("runBtn").addEventListener("click", () => send({ cmd: "run", running: !app.control?.running }));
  $("speed").addEventListener("change", (e) => send({ cmd: "speed", value: Number(e.target.value) }));
  $("ackAll").addEventListener("click", () => send({ cmd: "ack", id: null }));
  $("verifyChain").addEventListener("click", () => send({ cmd: "audit" }));
  for (const tab of document.querySelectorAll("[data-drawer]")) {
    tab.addEventListener("click", () => toggleDrawer(tab.dataset.drawer));
  }
  for (const b of document.querySelectorAll("[data-close]")) {
    b.addEventListener("click", () => toggleDrawer(null));
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleDrawer(null); });
}

function toggleDrawer(name) {
  for (const tab of document.querySelectorAll("[data-drawer]")) {
    const open = tab.dataset.drawer === name && tab.getAttribute("aria-expanded") !== "true";
    tab.setAttribute("aria-expanded", String(open));
    const d = $(`drawer-${tab.dataset.drawer}`);
    d.hidden = !open;
    if (open) {
      d.querySelector("h2").setAttribute("tabindex", "-1");
      d.querySelector("h2").focus();
      if (name === "audit") send({ cmd: "audit" });
    }
  }
}

function toast(text, ok = true) {
  const t = el("div", { class: "toast", "data-ok": String(ok) }, text);
  $("toasts").append(t);
  setTimeout(() => t.remove(), 3200);
}

connect();
