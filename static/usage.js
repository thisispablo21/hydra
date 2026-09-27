// Visual layer for the /usage dashboard.
//
// Form choices follow the data's job, not taste:
//  - one total to lead with        -> KPI tile, not a one-bar chart
//  - cost over time, one series    -> columns with a hover readout
//  - ranked magnitude, >7 classes  -> table with an inline length encoding, ONE hue
//  - part-to-whole, 4 parts        -> stacked bar, categorical, legend + direct labels
//
// Categorical slots are assigned per entity in a fixed order and never cycled or
// reordered by rank. Days with no activity are drawn as zeros rather than skipped,
// otherwise the time axis silently compresses idle stretches.

const RANGES = [
    { label: "7d", days: 7 },
    { label: "30d", days: 30 },
    { label: "90d", days: 90 },
    { label: "All", days: null },
];
const GROUPS = ["day", "project", "model", "agent"];

// Fixed slot order; colors follow the entity, never its size.
const COMPOSITION = [
    { key: "cache_read", label: "Cache read", css: "--series-1" },
    { key: "cache_write", label: "Cache write", css: "--series-2" },
    { key: "output", label: "Output", css: "--series-3" },
    { key: "input", label: "Input", css: "--series-4" },
];

let activeRange = RANGES[1];
let activeInstance = null;   // null = every machine
let activeHarness = null;    // null = every harness
let lastData = null;

const $ = (id) => document.getElementById(id);
const cssVar = (name) =>
    getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(name).trim();

function fmtCost(n) {
    const v = Number(n || 0);
    if (v >= 1000) return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 0 });
    return "$" + v.toFixed(2);
}
function fmtTokens(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n || 0);
}
function rowTokens(r) {
    return r.input_tokens + r.output_tokens + r.cache_read_tokens +
        r.cache_write_5m_tokens + r.cache_write_1h_tokens;
}
function isoDay(d) { return d.toISOString().slice(0, 10); }

// Axis ticks land on 1/2/5 x 10^n so the reader gets $50/$100/$150, never
// $47.21/$94.42 (max/4, which is what naive fractions of the peak produce).
function niceTicks(max, count) {
    if (!(max > 0)) return [0, 1];
    const mag = Math.pow(10, Math.floor(Math.log10(max / count)));
    const norm = max / count / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    const ticks = [];
    for (let v = 0; v < max - step / 1e6; v += step) ticks.push(v);
    ticks.push(ticks[ticks.length - 1] + step);
    return ticks;
}
function fmtAxis(v) {
    if (v >= 1000) return "$" + (v / 1000).toFixed(v % 1000 ? 1 : 0) + "k";
    return "$" + Math.round(v);
}

// --- range picker ---------------------------------------------------------
function renderRanges() {
    $("range-picker").innerHTML = RANGES.map((r) =>
        `<span class="uz-chip${r === activeRange ? " active" : ""}" data-range="${escHtml(r.label)}">${escHtml(r.label)}</span>`
    ).join("");
    $("range-picker").querySelectorAll("[data-range]").forEach((el) => {
        el.onclick = () => {
            activeRange = RANGES.find((r) => r.label === el.dataset.range) || activeRange;
            renderRanges();
            refresh();
        };
    });
}

// --- machine filter ------------------------------------------------------
// Hidden entirely on a single-machine fleet: a filter with one option is noise.
// The machine list is fetched WITHOUT the instance filter applied, so picking
// one never hides the others and you can always get back to "All".
function renderMachines(rows) {
    const picker = $("machine-picker");
    if (rows.length < 2) {
        picker.hidden = true;
        picker.innerHTML = "";
        const had = activeInstance !== null;
        activeInstance = null;
        return had;
    }
    let reset = false;
    if (activeInstance && !rows.some((r) => r.key === activeInstance)) {
        activeInstance = null;   // selection fell outside the current window
        reset = true;
    }
    picker.hidden = false;
    const chip = (key, label, on) =>
        `<span class="uz-chip${on ? " active" : ""}" data-machine="${escHtml(key)}">${escHtml(label)}</span>`;
    picker.innerHTML =
        chip("", "All machines", !activeInstance) +
        rows.map((r) => chip(r.key, `${r.key} · ${fmtCost(r.cost_usd)}`, activeInstance === r.key)).join("");
    picker.querySelectorAll("[data-machine]").forEach((el) => {
        el.onclick = () => {
            activeInstance = el.dataset.machine || null;
            refresh();
        };
    });
    return reset;
}

// --- harness filter ------------------------------------------------------
function renderHarnesses(rows) {
    const picker = $("harness-picker");
    if (rows.length < 2) {
        picker.hidden = true;
        picker.innerHTML = "";
        const had = activeHarness !== null;
        activeHarness = null;
        return had;
    }
    let reset = false;
    if (activeHarness && !rows.some((r) => r.key === activeHarness)) {
        activeHarness = null;
        reset = true;
    }
    picker.hidden = false;
    const chip = (key, label, on) =>
        `<span class="uz-chip${on ? " active" : ""}" data-harness="${escHtml(key)}">${escHtml(label)}</span>`;
    picker.innerHTML =
        chip("", "All harnesses", !activeHarness) +
        rows.map((r) => chip(r.key, `${r.key} · ${fmtCost(r.cost_usd)}`, activeHarness === r.key)).join("");
    picker.querySelectorAll("[data-harness]").forEach((el) => {
        el.onclick = () => {
            activeHarness = el.dataset.harness || null;
            refresh();
        };
    });
    return reset;
}

// --- KPI row -----------------------------------------------------------
function renderKpis(totals, dayRows) {
    const days = dayRows.length || 1;
    const tiles = [
        { v: fmtCost(totals.cost_usd), l: "Total cost", lead: true },
        { v: fmtCost(totals.cost_usd / days), l: "Per active day" },
        { v: fmtTokens(rowTokens(totals)), l: "Tokens" },
        { v: fmtTokens(totals.output_tokens), l: "Output tokens" },
        { v: totals.messages.toLocaleString(), l: "API messages" },
    ];
    $("kpis").innerHTML = tiles.map((t) =>
        `<div class="uz-kpi${t.lead ? " lead" : ""}">
            <span class="uz-kpi-value">${escHtml(t.v)}</span>
            <span class="uz-kpi-label">${escHtml(t.l)}</span>
         </div>`).join("");
}


// --- daily chart ----------------------------------------------------------
function densify(rows) {
    // Fill gaps so idle stretches read as idle instead of vanishing.
    if (!rows.length) return [];
    const byDay = new Map(rows.map((r) => [r.key, r]));
    const sorted = rows.map((r) => r.key).sort();
    const start = new Date(sorted[0] + "T00:00:00Z");
    const end = new Date(sorted[sorted.length - 1] + "T00:00:00Z");
    const out = [];
    for (let d = new Date(start); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
        const key = isoDay(d);
        const hit = byDay.get(key);
        out.push({ key, cost: hit ? hit.cost_usd : 0, tokens: hit ? rowTokens(hit) : 0, has: !!hit });
    }
    return out;
}

function renderDaily(rows) {
    const host = $("chart-daily");
    const series = densify(rows);
    if (!series.length) {
        host.innerHTML = '<p class="empty-state">No activity in this range.</p>';
        return;
    }
    // Render at measured pixel size rather than scaling a fixed viewBox: a
    // stretched viewBox distorts label text and corner radii non-uniformly.
    const W = Math.max(320, host.clientWidth);
    const H = Math.max(120, host.clientHeight);
    const padL = 46, padR = 8, padT = 12, padB = 22;
    const iw = W - padL - padR, ih = H - padT - padB;
    const ticks = niceTicks(Math.max(...series.map((d) => d.cost), 1), 4);
    const max = ticks[ticks.length - 1];
    // Band scale, not a point scale: columns are centred in their own slot, so
    // the first one cannot straddle the axis line and sit on the tick labels.
    const band = iw / series.length;
    const x = (i) => padL + band * (i + 0.5);
    const y = (v) => padT + ih - (v / max) * ih;

    // Recessive grid; four ticks is enough to read a level off.
    const grid = ticks.map((t) =>
        `<line x1="${padL}" x2="${W - padR}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"
               stroke="${cssVar("--grid")}" stroke-width="1"/>
         <text x="${padL - 6}" y="${(y(t) + 3.5).toFixed(1)}" text-anchor="end"
               font-size="10" fill="${cssVar("--muted")}">${escHtml(fmtAxis(t))}</text>`).join("");

    const bw = Math.max(1.5, band - 2);
    const marks = series.map((d, i) => {
        const h = Math.max(d.cost > 0 ? 1.5 : 0, padT + ih - y(d.cost));
        return `<rect x="${(x(i) - bw / 2).toFixed(1)}" y="${(padT + ih - h).toFixed(1)}"
                      width="${bw.toFixed(1)}" height="${h.toFixed(1)}"
                      rx="${Math.min(2, bw / 2).toFixed(1)}" fill="${cssVar("--bar")}"/>`;
    }).join("");

    const step = Math.max(1, Math.ceil(series.length / 6));
    const xlabels = series.map((d, i) => (i % step === 0 || i === series.length - 1)
        ? `<text x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="10"
                 fill="${cssVar("--muted")}">${escHtml(d.key.slice(5))}</text>` : "").join("");

    host.innerHTML = `
        <svg viewBox="0 0 ${W} ${H}" role="img"
             aria-label="Daily cost, ${series.length} days">
            ${grid}${marks}
            <line x1="${padL}" x2="${W - padR}" y1="${padT + ih}" y2="${padT + ih}"
                  stroke="${cssVar("--axis")}" stroke-width="1"/>
            ${xlabels}
            <line id="uz-cross" x1="0" x2="0" y1="${padT}" y2="${padT + ih}"
                  stroke="${cssVar("--ink-2")}" stroke-width="1" opacity="0"/>
        </svg>
        <div class="uz-tip" id="uz-tip"></div>`;

    const svg = host.querySelector("svg");
    const tip = $("uz-tip");
    const cross = $("uz-cross");
    svg.addEventListener("mousemove", (ev) => {
        const box = svg.getBoundingClientRect();
        let i = Math.floor((ev.clientX - box.left - padL) / band);
        i = Math.max(0, Math.min(series.length - 1, i));
        const d = series[i];
        cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
        cross.setAttribute("opacity", "0.5");
        tip.classList.add("on");
        tip.style.left = `${x(i)}px`;
        tip.style.top = `${y(d.cost)}px`;
        tip.innerHTML = `<span class="uz-tip-k">${escHtml(d.key)}</span> · ` +
            `${escHtml(fmtCost(d.cost))} · ${escHtml(fmtTokens(d.tokens))} tok`;
    });
    svg.addEventListener("mouseleave", () => {
        tip.classList.remove("on");
        cross.setAttribute("opacity", "0");
    });

    const active = series.filter((d) => d.has).length;
    $("chart-note").textContent = `${active} active of ${series.length} days`;
}

// --- ranked tables --------------------------------------------------------
function renderRanked(elId, rows, limit) {
    const el = $(elId);
    if (!rows.length) { el.innerHTML = '<p class="empty-state">Nothing here yet.</p>'; return; }
    let shown = rows;
    if (limit && rows.length > limit) {
        const rest = rows.slice(limit);
        const other = rest.reduce((a, r) => ({
            key: `Other (${rest.length})`,
            cost_usd: a.cost_usd + r.cost_usd,
            tokens: a.tokens + rowTokens(r),
            unpriced_messages: a.unpriced_messages + r.unpriced_messages,
        }), { key: "", cost_usd: 0, tokens: 0, unpriced_messages: 0 });
        shown = rows.slice(0, limit).map((r) => ({ ...r, tokens: rowTokens(r) })).concat([other]);
    } else {
        shown = rows.map((r) => ({ ...r, tokens: rowTokens(r) }));
    }
    const max = Math.max(...shown.map((r) => r.cost_usd), 0.0001);
    el.innerHTML = shown.map((r) => {
        const pct = Math.max(1, (r.cost_usd / max) * 100);
        const flag = r.unpriced_messages
            ? ` <span class="uz-warning">${r.unpriced_messages} unpriced</span>` : "";
        return `<div class="uz-row" title="${escHtml(r.key)}">
            <span class="uz-row-key">${escHtml(r.key)}${flag}</span>
            <span class="uz-track"><span class="uz-fill" style="width:${pct.toFixed(1)}%"></span></span>
            <span class="uz-num-2">${escHtml(fmtTokens(r.tokens))}</span>
            <span class="uz-num">${escHtml(fmtCost(r.cost_usd))}</span>
        </div>`;
    }).join("");
}

// --- composition ----------------------------------------------------------
function compositionParts(totals) {
    const c = totals.cost_components;
    return {
        cost: {
            cache_read: c.cache_read,
            cache_write: c.cache_write_5m + c.cache_write_1h,
            output: c.output,
            input: c.input,
        },
        tokens: {
            cache_read: totals.cache_read_tokens,
            cache_write: totals.cache_write_5m_tokens + totals.cache_write_1h_tokens,
            output: totals.output_tokens,
            input: totals.input_tokens,
        },
    };
}

function stackedBar(title, parts, fmt) {
    const total = Object.values(parts).reduce((a, b) => a + b, 0) || 1;
    const segs = COMPOSITION.map((s) => {
        const v = parts[s.key] || 0;
        const pct = (v / total) * 100;
        // Name + share only when the segment can hold it; a bare share below
        // that, nothing below that again. Prevents "Output 1…" clipping in the
        // half-width panel while keeping the wide layout fully labelled.
        const text = pct >= 18 ? `${s.label} ${pct.toFixed(0)}%` : pct >= 7 ? `${pct.toFixed(0)}%` : "";
        const label = text ? `<span class="uz-seg-label">${escHtml(text)}</span>` : "";
        return `<span class="uz-seg" style="width:${pct}%;background:${cssVar(s.css)}"
                      title="${escHtml(s.label)}: ${escHtml(fmt(v))} (${pct.toFixed(1)}%)">${label}</span>`;
    }).join("");
    return `<div class="uz-comp">
        <div class="uz-comp-head"><span>${escHtml(title)}</span><span>${escHtml(fmt(total))}</span></div>
        <div class="uz-comp-bar">${segs}</div>
    </div>`;
}

function renderComposition(totals) {
    const p = compositionParts(totals);
    const legend = COMPOSITION.map((s) =>
        `<span class="uz-legend-item"><span class="uz-swatch" style="background:${cssVar(s.css)}"></span>${escHtml(s.label)}</span>`
    ).join("");
    $("composition").innerHTML =
        stackedBar("Tokens", p.tokens, fmtTokens) +
        stackedBar("Cost", p.cost, fmtCost) +
        `<div class="uz-legend">${legend}</div>`;
}

// --- orchestration --------------------------------------------------------
function sinceParam() {
    if (!activeRange.days) return null;
    return isoDay(new Date(Date.now() - activeRange.days * 86400 * 1000));
}

async function refresh() {
    const since = sinceParam();
    const window_ = since ? `&since=${since}` : "";
    const instanceScope = activeInstance ? `&instance=${encodeURIComponent(activeInstance)}` : "";
    const harnessScope = activeHarness ? `&harness=${encodeURIComponent(activeHarness)}` : "";
    const scoped = window_ + instanceScope + harnessScope;
    const get = async (query) => {
        const r = await apiFetch(`${API}/usage/summary?${query}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    };
    try {
        // Each filter list is scoped by the other filter, never by itself.
        const [machines, harnesses, ...res] = await Promise.all([
            get(`group_by=instance${window_}${harnessScope}`),
            get(`group_by=harness${window_}${instanceScope}`),
            ...GROUPS.map((g) => get(`group_by=${g}${scoped}`)),
        ]);
        const d = Object.fromEntries(GROUPS.map((g, i) => [g, res[i]]));
        lastData = d;

        const resetMachine = renderMachines(machines.rows);
        const resetHarness = renderHarnesses(harnesses.rows);
        if (resetMachine || resetHarness) return refresh();
        renderKpis(d.day.totals, d.day.rows);
        renderDaily(d.day.rows);
        renderComposition(d.day.totals);
        renderRanked("table-project", d.project.rows, 8);
        renderRanked("table-model", d.model.rows, 0);
        renderRanked("table-agent", d.agent.rows, 6);

        const warn = $("unpriced-warning");
        const un = d.day.unpriced_models;
        const known = d.day.known_unpriced_models || [];
        warn.hidden = !un.length && !known.length;
        warn.innerHTML = (un.length
            ? `<p class="uz-warning">No rate known for ${un.map(escHtml).join(", ")} —
                tokens counted, cost excluded. Add them to server/pricing.py.</p>` : "")
            + (known.length
            ? `<p class="uz-note">${known.map(escHtml).join(", ")} ${known.length > 1 ? "are" : "is"}
                currently unpriced.</p>` : "");
    } catch (err) {
        $("kpis").innerHTML = `<p class="empty-state">Failed to load: ${escHtml(err.message)}</p>`;
    }
}

function startUsage() {
    renderRanges();
    refresh();
    let t;
    window.addEventListener("resize", () => {
        clearTimeout(t);
        t = setTimeout(() => { if (lastData) renderDaily(lastData.day.rows); }, 150);
    });
}
