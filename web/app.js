/* 盯盘量化面板 —— 与数据服务同源部署，调用 POST /call */
const LS = { base: "sa_base", key: "sa_key" };
const cfg = {
  base: localStorage.getItem(LS.base) || window.location.origin,
  key: localStorage.getItem(LS.key) || "",
};

const $ = (id) => document.getElementById(id);
const toast = (msg, type = "") => {
  const t = $("toast");
  t.textContent = msg; t.className = "toast " + type;
  setTimeout(() => (t.className = "toast hidden"), 2600);
};

async function call(fn, params = {}) {
  const res = await fetch(cfg.base.replace(/\/$/, "") + "/call", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": cfg.key },
    body: JSON.stringify({ function: fn, params }),
  });
  const body = await res.json();
  if (!res.ok || body.ok === false) {
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return body.data;
}

async function health() {
  const res = await fetch(cfg.base.replace(/\/$/, "") + "/health", {
    headers: { "X-API-Key": cfg.key },
  });
  return res.json();
}

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "weights") loadWeights();
    if (btn.dataset.tab === "backtest") loadBacktest();
    if (btn.dataset.tab === "precompute") loadPrecompute();
    if (btn.dataset.tab === "sentiment") loadSentiment();
  });
});

/* ---------- settings ---------- */
$("settingsBtn").onclick = () => {
  $("cfg-base").value = cfg.base;
  $("cfg-key").value = cfg.key;
  $("cfg-status").textContent = "";
  $("settings").classList.remove("hidden");
};
$("settings").addEventListener("click", (e) => { if (e.target.id === "settings") $("settings").classList.add("hidden"); });
function reloadActiveTab() {
  const active = document.querySelector(".tab.active");
  const t = active && active.dataset.tab;
  if (t === "weights") loadWeights();
  else if (t === "backtest") loadBacktest(true);
  else if (t === "precompute") loadPrecompute(true);
  else if (t === "sentiment") { _sentLoaded = false; loadSentiment(); }
}

$("cfg-save").onclick = async () => {
  cfg.base = $("cfg-base").value.trim() || window.location.origin;
  cfg.key = $("cfg-key").value.trim();
  localStorage.setItem(LS.base, cfg.base);
  localStorage.setItem(LS.key, cfg.key);
  // 保存后校验一次连通
  try {
    const h = await health();
    if (h.status === "ok") {
      $("settings").classList.add("hidden");
      toast("已连接：功能数 " + h.functions, "ok");
      reloadActiveTab();
      return;
    }
  } catch (e) { /* fallthrough */ }
  toast("已保存，但连接校验失败，请检查基址/Key", "bad");
};
$("cfg-test").onclick = async () => {
  cfg.base = $("cfg-base").value.trim() || window.location.origin;
  cfg.key = $("cfg-key").value.trim();
  const s = $("cfg-status");
  s.textContent = "连接中…"; s.className = "status";
  try {
    const h = await health();
    s.textContent = `连通 ✓ 交易日=${h.trade_open} 功能数=${h.functions} 版本=${h.data_version}`;
    s.className = "status ok";
  } catch (e) { s.textContent = "连接失败：" + e.message; s.className = "status bad"; }
};

/* ---------- 量化选股 ---------- */
$("q-run").onclick = async () => {
  const btn = $("q-run"); btn.disabled = true;
  const industries = $("q-industries").value.trim();
  const params = { top_n: Number($("q-topn").value) || 30 };
  if (industries) params.industries = industries.split(/[，,]/).map((s) => s.trim()).filter(Boolean);
  $("q-result").innerHTML = '<div class="empty">运行中…</div>';
  try {
    const d = await call("screen_quant", params);
    const rows = d.candidates || [];
    $("q-meta").textContent = `${d.trade_date || ""} · ${rows.length} 只`;
    $("q-result").innerHTML = renderCandidates(rows);
  } catch (e) { $("q-result").innerHTML = ""; toast("选股失败：" + e.message, "bad"); }
  btn.disabled = false;
};

function renderTable(rows) {
  const cols = Object.keys(rows[0]);
  const head = cols.map((c) => `<th>${c}</th>`).join("");
  const body = rows.map((r) => "<tr>" + cols.map((c) => {
    let v = r[c];
    // 标的单元格：名称在上、代码在下
    if (v && typeof v === "object" && "code" in v) {
      return `<td class="cell-ticker"><b>${v.name || "-"}</b><span>${v.code || ""}</span></td>`;
    }
    let cls = "";
    if (typeof v === "number") { if (c === "综合分" || c === "score") cls = v >= 0 ? "pos" : "neg"; v = Number.isInteger(v) ? v : v.toFixed(3); }
    return `<td class="${cls}">${v ?? ""}</td>`;
  }).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// 量化选股候选 → 中文列 + 标的(名称/代码) 单元格
const QUANT_COLMAP = [
  ["mom_12_1", "12-1动量"], ["reversal_1m", "1月反转"], ["trend_ma", "均线多头"],
  ["high_52w", "距52周高"], ["low_ivol", "低波动"], ["low_turnover", "低换手"],
  ["vol_confirm", "量能"],
];
function renderCandidates(cands) {
  if (!cands || !cands.length) return '<div class="empty">无候选</div>';
  const rows = cands.map((r) => {
    const row = { 标的: { name: r.name, code: r.code }, 现价: r.price };
    QUANT_COLMAP.forEach(([k, cn]) => { if (k in r) row[cn] = r[k]; });
    row["综合分"] = r.score;
    return row;
  });
  return renderTable(rows);
}

/* ---------- 轻量 SVG 图表（无外部依赖） ---------- */
// 折线图：points = [{label, value}]；opts = {min, max, color, unit}
function svgLine(points, opts = {}) {
  if (!points || !points.length) return '<div class="empty">无数据</div>';
  const W = 640, H = 220, padL = 42, padR = 16, padT = 16, padB = 36;
  const color = opts.color || "#3b6cf6";
  const unit = opts.unit || "";
  const vals = points.map((p) => p.value);
  let min = opts.min != null ? opts.min : Math.min(...vals);
  let max = opts.max != null ? opts.max : Math.max(...vals);
  if (max === min) { max += 1; min -= 1; }
  const span = max - min;
  const n = points.length;
  const xAt = (i) => padL + (W - padL - padR) * (n === 1 ? 0.5 : i / (n - 1));
  const yAt = (v) => padT + (H - padT - padB) * (1 - (v - min) / span);
  // 网格线 + Y 轴刻度（5 档）
  let grid = "", yTicks = "";
  for (let g = 0; g <= 4; g++) {
    const v = min + (span * g) / 4;
    const y = yAt(v);
    grid += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="#eef1f7" stroke-width="1"/>`;
    yTicks += `<text x="${padL - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="ax">${v.toFixed(0)}</text>`;
  }
  const dPath = points.map((p, i) => `${i ? "L" : "M"}${xAt(i).toFixed(1)} ${yAt(p.value).toFixed(1)}`).join(" ");
  const area = `${dPath} L${xAt(n - 1).toFixed(1)} ${yAt(min).toFixed(1)} L${xAt(0).toFixed(1)} ${yAt(min).toFixed(1)} Z`;
  const dots = points.map((p, i) =>
    `<circle cx="${xAt(i).toFixed(1)}" cy="${yAt(p.value).toFixed(1)}" r="3" fill="${color}"><title>${p.label}: ${p.value}${unit}</title></circle>`).join("");
  // X 轴标签：点多时抽稀
  const step = Math.ceil(n / 8);
  const xLabels = points.map((p, i) =>
    (i % step === 0 || i === n - 1)
      ? `<text x="${xAt(i).toFixed(1)}" y="${H - 12}" text-anchor="middle" class="ax">${p.label}</text>` : "").join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" preserveAspectRatio="xMidYMid meet">
    ${grid}${yTicks}
    <path d="${area}" fill="${color}" fill-opacity="0.08"/>
    <path d="${dPath}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}${xLabels}
  </svg>`;
}

// 柱状图：items = [{label, value}]；支持正负（以 0 为基线）
function svgBars(items, opts = {}) {
  if (!items || !items.length) return '<div class="empty">无数据</div>';
  const unit = opts.unit || "";
  const W = 640, H = 240, padL = 42, padR = 16, padT = 16, padB = 48;
  const vals = items.map((d) => d.value);
  let max = Math.max(0, ...vals);
  let min = Math.min(0, ...vals);
  if (max === min) { max += 1; }
  const span = max - min;
  const n = items.length;
  const bandW = (W - padL - padR) / n;
  const barW = Math.min(46, bandW * 0.6);
  const yAt = (v) => padT + (H - padT - padB) * (1 - (v - min) / span);
  const zeroY = yAt(0);
  let grid = "", yTicks = "";
  for (let g = 0; g <= 4; g++) {
    const v = min + (span * g) / 4;
    const y = yAt(v);
    grid += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="#eef1f7" stroke-width="1"/>`;
    yTicks += `<text x="${padL - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="ax">${v.toFixed(1)}</text>`;
  }
  const bars = items.map((d, i) => {
    const cx = padL + bandW * (i + 0.5);
    const y = yAt(Math.max(0, d.value));
    const h = Math.abs(yAt(d.value) - zeroY);
    const fill = d.value >= 0 ? "#ef4444" : "#16a34a";
    return `<rect x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(1, h).toFixed(1)}" rx="4" fill="${fill}"><title>${d.label}: ${d.value}${unit}</title></rect>
      <text x="${cx.toFixed(1)}" y="${(d.value >= 0 ? y - 5 : y + h + 13).toFixed(1)}" text-anchor="middle" class="ax bold">${d.value}${unit}</text>
      <text x="${cx.toFixed(1)}" y="${H - 14}" text-anchor="middle" class="ax">${d.label}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" preserveAspectRatio="xMidYMid meet">
    ${grid}${yTicks}
    <line x1="${padL}" y1="${zeroY.toFixed(1)}" x2="${W - padR}" y2="${zeroY.toFixed(1)}" stroke="#c9d2e3" stroke-width="1"/>
    ${bars}
  </svg>`;
}

/* ---------- 情绪温度 ---------- */
let _sentLoaded = false;
function loadSentiment() {
  if (_sentLoaded || !cfg.key) return;
  _sentLoaded = true;
  syncSentimentWindow();
  runSentiment();
}

async function syncSentimentWindow() {
  try {
    const c = await call("get_sentiment_config", {});
    $("s-window").value = c.window;
    $("s-window").min = c.range?.[0] ?? 3;
    $("s-window").max = c.range?.[1] ?? 30;
  } catch (e) { /* 忽略 */ }
}

$("s-window-save").onclick = async () => {
  const w = Number($("s-window").value);
  const st = $("s-window-status");
  if (!(w >= 3 && w <= 30)) { st.textContent = "窗口须 3-30"; return; }
  st.textContent = "保存中…";
  try {
    const r = await call("set_sentiment_config", { window: w });
    if (r.applied) { st.textContent = `已应用窗口 ${r.window}`; runSentiment(); }
    else st.textContent = r.error || "保存失败";
  } catch (e) { st.textContent = "保存失败：" + e.message; }
};

$("s-run").onclick = runSentiment;
async function runSentiment() {
  const btn = $("s-run"); btn.disabled = true;
  const date = $("s-date").value.trim();
  const days = Number($("s-days").value) || 15;
  $("s-result").innerHTML = '<div class="empty">读取中…</div>';
  $("s-trend").innerHTML = '<div class="empty">读取中…</div>';
  // 单日温度 + 指标分解
  try {
    const d = await call("sentiment_temperature", date ? { date } : {});
    if (d.error) throw new Error(d.error);
    setGauge(d.temperature, d.level);
    $("s-meta").textContent = `${d.date} · 窗口 ${d.window_dates?.length || 0} 日`;
    $("s-breadth").textContent = d.breadth ? `上涨 ${d.breadth.adv} 家 · 下跌 ${d.breadth.dec} 家` : "";
    const inds = d.indicators || {};
    $("s-ranges").innerHTML = renderRanges(inds, d.weights || {});
    const rows = Object.keys(inds).map((k) => ({
      指标: factorLabel(k), 权重: d.weights?.[k], 今值: inds[k].raw_today,
      窗口低: inds[k].window_min, 窗口均值: inds[k].window_mean, 窗口高: inds[k].window_max,
      较均值: inds[k].vs_mean, 子分: inds[k].sub_score,
    }));
    $("s-result").innerHTML = rows.length ? renderTable(rows) : '<div class="empty">无数据</div>';
  } catch (e) { $("s-result").innerHTML = ""; toast("情绪读取失败：" + e.message, "bad"); }
  // 多日温度走势（market_timing 返回温度序列）
  try {
    const params = { days };
    if (date) params.date = date;
    const t = await call("market_timing", params);
    if (t.error) throw new Error(t.error);
    const series = (t.recent || []).map((x) => ({ label: fmtDate(x.date), value: x.temperature }));
    $("s-trend").innerHTML = svgLine(series, { min: 0, max: 100, color: "#f59e0b" });
    $("s-trend-meta").textContent = `${series.length} 个交易日`;
    renderTiming(t);
  } catch (e) {
    $("s-trend").innerHTML = '<div class="empty">走势读取失败：' + e.message + "</div>";
    $("s-timing").innerHTML = '<div class="empty">择时读取失败</div>';
  }
  btn.disabled = false;
};

function renderTiming(t) {
  const hint = t.buy_weight_hint ?? 1;
  const hintCls = hint > 1 ? "pos" : (hint < 1 ? "neg" : "");
  $("s-timing").innerHTML = `
    <div class="timing-badges">
      <div class="badge"><small>最新温度</small><b>${t.latest_temperature ?? "--"}</b></div>
      <div class="badge"><small>连续冰点</small><b>${t.cold_streak ?? 0} 日</b></div>
      <div class="badge"><small>连续高热</small><b>${t.hot_streak ?? 0} 日</b></div>
      <div class="badge"><small>出手买入权重</small><b class="${hintCls}">×${hint}</b></div>
    </div>
    <div class="timing-stance">${t.stance || ""}</div>`;
}

function fmtDate(d) {
  const s = String(d || "");
  return s.length === 8 ? `${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
}

function setGauge(temp, level) {
  const L = 251; // 半圆弧长近似
  const off = L * (1 - Math.max(0, Math.min(100, temp)) / 100);
  $("s-arc").style.strokeDashoffset = off;
  $("s-temp").textContent = temp;
  $("s-level").textContent = level || "情绪温度";
}

// 指标对比看板：每个指标画「低—均—高」轨道 + 今值位置
function renderRanges(inds, weights) {
  const keys = Object.keys(inds);
  if (!keys.length) return '<div class="empty">无数据</div>';
  const clamp = (x) => Math.max(0, Math.min(100, x));
  const pct = (v, lo, hi) => (hi > lo ? clamp(((v - lo) / (hi - lo)) * 100) : 50);
  const legend = `<div class="range-legend">
    <span><i class="lg-track"></i> 窗口低→高</span>
    <span><i class="lg-mean"></i> 均值</span>
    <span><i class="lg-dot"></i> 今值</span></div>`;
  const rows = keys.map((k) => {
    const d = inds[k];
    const lo = d.window_min, hi = d.window_max, mean = d.window_mean, cur = d.raw_today;
    const tp = pct(cur, lo, hi), mp = pct(mean, lo, hi);
    const vm = d.vs_mean ?? 0;
    const vmCls = vm > 0 ? "pos" : (vm < 0 ? "neg" : "");
    const icon = k === "index_kline"
      ? '<svg class="k-ic" viewBox="0 0 24 24" aria-hidden="true"><line x1="8" y1="2" x2="8" y2="22" stroke="currentColor" stroke-width="1.5"/><rect x="4.5" y="7" width="7" height="9" rx="1" fill="currentColor"/><line x1="17" y1="4" x2="17" y2="20" stroke="currentColor" stroke-width="1.5"/><rect x="13.5" y="9" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'
      : "";
    return `<div class="range-row">
      <div class="range-name"><span class="rn-txt factor-link" data-fkey="${k}">${icon}${factorLabel(k)} ⓘ</span><small>权重 ${weights[k] ?? "-"}</small></div>
      <div class="range-track" title="今值 ${cur} ｜ 低 ${lo} ｜ 均 ${mean} ｜ 高 ${hi}">
        <div class="range-mean" style="left:${mp}%"></div>
        <div class="range-dot" style="left:${tp}%"></div>
        <span class="range-end lo">${lo}</span>
        <span class="range-end hi">${hi}</span>
      </div>
      <div class="range-sub"><b>${d.sub_score}</b><small class="${vmCls}">较均 ${vm >= 0 ? "+" : ""}${vm}</small></div>
    </div>`;
  }).join("");
  return legend + rows;
}

/* ---------- 回测复盘 ---------- */
const HZ_ORDER = ["1d", "3d", "7d", "30d"];
const HZ_LABEL = { "1d": "1日", "3d": "3日", "7d": "7日", "30d": "30日" };

$("b-run").onclick = () => loadBacktest(true);

let _btLoaded = false;
async function loadBacktest(force = false) {
  if (_btLoaded && !force) return;
  _btLoaded = true;
  const setLoading = (id) => ($(id).innerHTML = '<div class="empty">加载中…</div>');
  ["b-auto-ret", "b-auto-win", "b-driver", "b-pred-acc", "b-detail"].forEach(setLoading);
  $("b-hints").innerHTML = '<div class="empty">加载中…</div>';

  // 选股回测
  try {
    const d = await call("selection_backtest", {});
    $("b-meta").textContent = `共登记 ${d.total_selections ?? 0} 条`;
    const auto = (d.by_category_return && d.by_category_return.auto) || null;
    const autoExcess = (d.by_category_excess && d.by_category_excess.auto) || null;

    if (auto) {
      const ret = HZ_ORDER.filter((h) => auto[h]).map((h) => ({ label: HZ_LABEL[h], value: auto[h].avg_pct }));
      $("b-auto-ret").innerHTML = svgBars(ret, { unit: "%" });
      const win = HZ_ORDER.filter((h) => auto[h]).map((h) => ({ label: HZ_LABEL[h], value: auto[h].win_rate }));
      $("b-auto-win").innerHTML = svgBars(win, { unit: "%" });
    } else {
      $("b-auto-ret").innerHTML = '<div class="empty">暂无自动选股样本（需先经 log_selection 登记并满持有期）</div>';
      $("b-auto-win").innerHTML = '<div class="empty">—</div>';
    }

    // 分驱动 30 日超额
    const drv = d.auto_by_driver_excess || {};
    const drvItems = Object.keys(drv)
      .filter((k) => drv[k] && drv[k]["30d"])
      .map((k) => ({ label: k, value: drv[k]["30d"].avg_pct }));
    $("b-driver").innerHTML = drvItems.length ? svgBars(drvItems, { unit: "%" }) : '<div class="empty">30 日样本不足</div>';

    // 调参建议
    const hints = d.tuning_hints || [];
    $("b-hints").innerHTML = hints.length
      ? '<ul class="hint-list">' + hints.map((h) => `<li>${h}</li>`).join("") + "</ul>"
      : '<div class="empty">暂无建议</div>';

    // 明细
    const details = (d.details || []).slice().reverse();
    $("b-detail-meta").textContent = `${details.length} 条`;
    const rows = details.map((r) => ({
      日期: r.date, 代码: r.code, 名称: r.name, 类别: r.category, 驱动: r.driver,
      分数: r.score,
      "1日": r.returns_pct?.["1"], "3日": r.returns_pct?.["3"],
      "7日": r.returns_pct?.["7"], "30日": r.returns_pct?.["30"],
    }));
    $("b-detail").innerHTML = rows.length ? renderTable(rows) : '<div class="empty">无明细</div>';
  } catch (e) {
    ["b-auto-ret", "b-auto-win", "b-driver", "b-detail"].forEach((id) => ($(id).innerHTML = ""));
    $("b-hints").innerHTML = "";
    toast("选股回测加载失败：" + e.message, "bad");
  }

  // 预判回测
  try {
    const d = await call("predictions_backtest", {});
    $("b-pred-meta").textContent = d.trade_date ? `${d.trade_date} · ${d.correct}/${d.total} 命中` : "";
    const acc = d.accuracy_by_driver || {};
    const items = Object.keys(acc)
      .filter((k) => acc[k] != null)
      .map((k) => ({ label: k, value: acc[k] }));
    if (d.accuracy_pct != null) items.unshift({ label: "总体", value: d.accuracy_pct });
    $("b-pred-acc").innerHTML = items.length
      ? svgBars(items, { unit: "%" })
      : '<div class="empty">当日无可回测预判（predictions.jsonl 为空或未满交易日）</div>';
  } catch (e) {
    $("b-pred-acc").innerHTML = '<div class="empty">预判回测加载失败：' + e.message + "</div>";
  }
}

/* ---------- 权重配置 ---------- */
async function loadWeights() {
  const box = $("w-models");
  box.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const d = await call("get_factor_config", {});
    const models = d.models || {};
    box.innerHTML = "";
    Object.keys(models).forEach((m) => box.appendChild(modelBlock(m, models[m])));
  } catch (e) { box.innerHTML = ""; toast("加载配置失败：" + e.message, "bad"); }
}

const MODEL_LABEL = { stock: "个股量化 (screen_quant)", sector: "板块轮动 (screen_sector)", trend: "趋势选股 (screen_trend)", sentiment: "情绪温度指标" };

// 因子/指标 英文字段 -> 中文含义
const FACTOR_LABEL = {
  // 个股 / 趋势
  mom_12_1: "12-1 动量（中期趋势）",
  reversal_1m: "1 个月反转（短期超跌反弹）",
  trend_ma: "均线多头排列强度",
  high_52w: "距 52 周高点接近度",
  low_ivol: "低特质波动",
  low_turnover: "低换手",
  vol_confirm: "量能确认（温和放量）",
  // 板块
  sec_mom_12_1: "板块 12-1 动量",
  sec_mom_20d: "板块 20 日动量",
  sec_mom_5d: "板块 5 日动量",
  sec_vol_confirm: "板块量能确认",
  sec_low_vol: "板块低波动",
  // 情绪温度
  adv_dec_ratio: "涨跌家数比（上涨占比）",
  limit_updown: "涨跌停家数（涨停占比）",
  sector_ratio: "板块涨跌比（上涨板块占比）",
  turnover: "大盘成交额（量能）",
  index_mom: "大盘指数动量",
  avg_price_mom: "平均股价指数（全市场平均涨幅）",
  index_kline: "大盘K线形态（收盘强弱）",
};
const factorLabel = (f) => FACTOR_LABEL[f] || f;

// 因子/指标 详细介绍
const FACTOR_DESC = {
  mom_12_1: "过去约 252 个交易日、剔除最近 21 日的累计收益（12-1 动量）。剔除最近 1 个月是为避开短期反转干扰。值越大代表中期趋势越强。属趋势因子，正向。",
  reversal_1m: "最近 21 个交易日收益取负。A 股短期常呈反转：近月跌得多的，未来一段时间反弹概率更高。值越大（近月越弱）越有反弹预期。属情绪/反转因子，正向。",
  trend_ma: "均线多头排列强度：价格>MA20、MA20>MA60 各计 1 分，再叠加相对 MA60 的乖离。值越大趋势越确认。属趋势因子，正向。",
  high_52w: "当前价 / 过去 252 日最高价，衡量距 52 周高点的接近度（52 周高点因子）。越接近新高，强者恒强概率越高。正向。",
  low_ivol: "近 60 日日收益标准差取负（低特质波动）。低波动异象：波动越低、风险调整后收益越优。值越大（波动越低）越好。正向。",
  low_turnover: "换手率取负。高换手往往对应过度交易/情绪过热，未来收益偏低；低换手更稳健。值越大（换手越低）越好。正向。",
  vol_confirm: "近 5 日均量 / 前 20 日均量，衡量温和放量（已截断防爆量）。适度放量确认趋势。正向。",
  sec_mom_12_1: "板块指数的 12-1 中期动量。A 股行业层面动量为正（板块轮动有延续性）。正向。",
  sec_mom_20d: "板块指数近 20 个交易日动量，捕捉近端趋势延续。正向。",
  sec_mom_5d: "板块指数近 5 个交易日动量，反映短期情绪热度延续。正向。",
  sec_vol_confirm: "板块量能确认（近 5 日 / 前 20 日均量），放量上行更可信。正向。",
  sec_low_vol: "板块近 60 日波动取负，稳健趋势优于暴涨暴跌。正向。",
  adv_dec_ratio: "全市场上涨家数 /（上涨+下跌家数），衡量赚钱效应广度。越高情绪越热。",
  limit_updown: "涨停家数 /（涨停+跌停家数），情绪极值指标。越高越亢奋。",
  index_kline: "当天大盘（沪深300）K 线形态：0.5×收盘在日内高低区间的位置 + 0.5×阳阴实体占比，0~1。收在高位、大阳线得分高；收在低位、阴线得分低。反映当日多空强弱。",
  sector_ratio: "上涨板块数 /（上涨+下跌板块数），衡量热点扩散广度。越高越热。",
  turnover: "全市场成交额（量能）。放量代表资金活跃、情绪升温。",
  index_mom: "大盘指数当日涨跌幅（动量）。正向反映当日强弱。",
  avg_price_mom: "全市场个股平均涨跌幅（以涨幅锚定，非绝对均价）。反映“平均一只票”的当日表现。越高越热。",
};

function showFactorInfo(key) {
  $("fm-title").textContent = factorLabel(key);
  $("fm-key").textContent = key;
  $("fm-body").textContent = FACTOR_DESC[key] || "暂无该因子的详细说明。";
  $("factor-modal").classList.remove("hidden");
}
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-fkey]");
  if (el) showFactorInfo(el.getAttribute("data-fkey"));
});
$("fm-close").onclick = () => $("factor-modal").classList.add("hidden");
$("factor-modal").addEventListener("click", (e) => { if (e.target.id === "factor-modal") $("factor-modal").classList.add("hidden"); });

function modelBlock(model, info) {
  const wrap = document.createElement("div");
  wrap.className = "model-block";
  const factors = info.canonical_factors || Object.keys(info.weights || {});
  const grid = factors.map((f) => {
    const v = info.weights?.[f] ?? 0;
    return `<div class="weight-item"><label title="点击查看因子说明">
      <span class="factor-link" data-fkey="${f}">${factorLabel(f)} <span class="fkey">${f}</span> ⓘ</span></label>
      <input type="number" step="0.01" min="0" max="1" data-f="${f}" value="${v}" /></div>`;
  }).join("");
  wrap.innerHTML = `
    <div class="model-head">
      <span class="model-name">${MODEL_LABEL[model] || model}</span>
      <span class="model-sum">来源：${info.source}</span>
    </div>
    <div class="weight-grid">${grid}</div>
    <div class="model-actions">
      <button class="btn-primary" data-save="${model}">保存</button>
      <span class="model-sum" data-sum>和：--</span>
    </div>`;
  const inputs = () => [...wrap.querySelectorAll("input[data-f]")];
  const sumEl = wrap.querySelector("[data-sum]");
  const refreshSum = () => {
    const s = inputs().reduce((a, i) => a + (parseFloat(i.value) || 0), 0);
    sumEl.textContent = "和：" + s.toFixed(3);
    sumEl.className = "model-sum " + (Math.abs(s - 1) <= 0.01 ? "ok" : "bad");
  };
  inputs().forEach((i) => i.addEventListener("input", refreshSum));
  refreshSum();
  wrap.querySelector("[data-save]").onclick = async () => {
    const weights = {};
    inputs().forEach((i) => (weights[i.dataset.f] = parseFloat(i.value) || 0));
    try {
      const r = await call("set_factor_weights", { model, weights });
      if (r.applied) { toast(`已保存 ${model} 权重`, "ok"); loadWeights(); }
      else {
        let msg = r.error || "保存失败";
        if (r.missing?.length) msg += "；缺失:" + r.missing.join(",");
        if (r.unexpected?.length) msg += "；多余:" + r.unexpected.join(",");
        toast(msg, "bad");
        loadWeights(); // 刷新为最新规范因子列表
      }
    } catch (e) { toast("保存失败：" + e.message, "bad"); }
  };
  return wrap;
}

/* ---------- 预计算状态 ---------- */
let _pcLoaded = false;
async function loadPrecompute(force = false) {
  if (_pcLoaded && !force) return;
  _pcLoaded = true;
  $("pc-chart").innerHTML = '<div class="empty">加载中…</div>';
  $("pc-table").innerHTML = '<div class="empty">加载中…</div>';
  try {
    const d = await call("precompute_status", { limit: 30 });
    const cov = (d.coverage || []).slice().reverse(); // 升序便于折线
    $("pc-meta").textContent = d.latest_date ? `最新覆盖日 ${d.latest_date} · 共 ${cov.length} 日` : "暂无预计算数据";
    const series = cov.map((r) => ({ label: fmtDate(r.trade_date), value: r.count }));
    $("pc-chart").innerHTML = series.length ? svgLine(series, { min: 0, color: "#16a34a" })
      : '<div class="empty">daily_factors 为空，请运行预计算</div>';
    const rows = (d.coverage || []).map((r) => ({ 交易日: r.trade_date, 覆盖股票数: r.count }));
    $("pc-table").innerHTML = rows.length ? renderTable(rows) : '<div class="empty">无数据</div>';
  } catch (e) {
    $("pc-chart").innerHTML = ""; $("pc-table").innerHTML = "";
    toast("预计算状态加载失败：" + e.message, "bad");
  }
}

$("pc-refresh").onclick = () => loadPrecompute(true);
$("pc-run").onclick = async () => {
  const btn = $("pc-run"); btn.disabled = true;
  const s = $("pc-status"); s.textContent = "预计算中（首次较慢，请稍候）…"; s.className = "status";
  try {
    const d = await call("precompute_daily_factors", {});
    const dates = d.dates_computed || [];
    const n = dates.length ? d.stocks_per_date[dates[0]] : 0;
    s.textContent = `完成：${dates.join(",")} 写入 ${n} 只`; s.className = "status ok";
    loadPrecompute(true);
  } catch (e) { s.textContent = "失败：" + e.message; s.className = "status bad"; }
  btn.disabled = false;
};

/* 首屏：未配置 key 时自动弹出设置框，引导填写 */
if (!cfg.key) {
  setTimeout(() => {
    $("cfg-base").value = cfg.base;
    $("cfg-key").value = "";
    $("cfg-status").textContent = "请填入 service/.env 里的 API_KEY 后保存";
    $("cfg-status").className = "status";
    $("settings").classList.remove("hidden");
  }, 300);
}
