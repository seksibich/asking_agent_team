/* 盯盘量化面板 —— 与数据服务同源部署，调用 POST /call */
const LS = { base: "sa_base", key: "sa_key" };
const cfg = {
  base: localStorage.getItem(LS.base) || window.location.origin,
  key: localStorage.getItem(LS.key) || "",
};

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[ch]));
const toast = (msg, type = "") => {
  const t = $("toast");
  t.textContent = msg; t.className = "toast " + type;
  setTimeout(() => (t.className = "toast hidden"), 2600);
};

async function call(fn, params = {}) {
  if (!_authReady || !cfg.key) {
    openLogin("请先输入有效的服务 Key");
    throw new Error("请先登录");
  }
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

/* ---------- 角色 / 权限（管理员 vs 用户） ---------- */
// 未通过 Key 验证前禁止调用功能，服务端鉴权作为最终安全边界
const ADMIN_ONLY_TABS = new Set(["portfolio", "precompute"]);
let _role = "user";
let _authReady = false;
let _selectionsLoaded = false;
let _portfolioLoaded = false;
let _portfolioRows = [];
let _portfolioSelected = null;
let _portfolioSearchTimer = null;
let _portfolioSearchSeq = 0;
const isAdmin = () => _role === "admin";

function setAppLocked(locked) {
  document.body.classList.toggle("auth-locked", locked);
}

function openLogin(message = "请输入服务 Key") {
  const modal = $("login-modal");
  if (!modal) return;
  setAppLocked(true);
  $("login-base").value = cfg.base;
  $("login-key").value = "";
  $("login-status").textContent = message;
  $("login-status").className = "status";
  modal.classList.remove("hidden");
  setTimeout(() => $("login-key")?.focus(), 0);
}

async function login() {
  const base = $("login-base").value.trim() || window.location.origin;
  const key = $("login-key").value.trim();
  const status = $("login-status");
  if (!key) {
    status.textContent = "请输入服务 Key";
    status.className = "status bad";
    $("login-key").focus();
    return;
  }
  status.textContent = "验证中…";
  status.className = "status";
  try {
    const res = await fetch(base.replace(/\/$/, "") + "/whoami", {
      headers: { "X-API-Key": key },
    });
    const body = await res.json();
    if (!res.ok || !["admin", "user"].includes(body.role)) {
      throw new Error(body.error || "Key 无效或无权访问");
    }
    cfg.base = base;
    cfg.key = key;
    localStorage.setItem(LS.base, cfg.base);
    localStorage.setItem(LS.key, cfg.key);
    _role = body.role;
    _authReady = true;
    $("login-modal").classList.add("hidden");
    setAppLocked(false);
    applyRoleUI();
    toast(`登录成功：${isAdmin() ? "管理员" : "访客"}`, "ok");
  } catch (e) {
    _authReady = false;
    _role = "user";
    setAppLocked(true);
    applyRoleUI();
    status.textContent = "登录失败：" + e.message;
    status.className = "status bad";
    $("login-modal").classList.remove("hidden");
  }
}

async function refreshRole() {
  try {
    const res = await fetch(cfg.base.replace(/\/$/, "") + "/whoami", {
      headers: { "X-API-Key": cfg.key },
    });
    const body = await res.json();
    if (res.ok && ["admin", "user"].includes(body.role)) {
      _role = body.role;
      _authReady = true;
    } else {
      _role = "user";
      _authReady = false;
    }
  } catch (e) {
    _role = "user";
    _authReady = false;
  }
  setAppLocked(!_authReady);
  if (_authReady) $("login-modal").classList.add("hidden");
  applyRoleUI();
  if (!_authReady) openLogin("Key 无效或服务不可用，请重新输入");
  return _role;
}

// 依据角色隐藏管理员入口，并保留服务端鉴权作为最终安全边界
function applyRoleUI() {
  const admin = isAdmin();
  document.querySelectorAll(".admin-only").forEach((el) => {
    el.classList.toggle("hidden", !admin);
  });
  // 角色切换时若当前正停留在管理员 Tab，回到普通选股页
  const activeTab = document.querySelector(".tab.active");
  if (!admin && activeTab && ADMIN_ONLY_TABS.has(activeTab.dataset.tab)) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    const fallbackTab = document.querySelector('.tab[data-tab="quant"]');
    fallbackTab?.classList.add("active");
    $("tab-quant")?.classList.add("active");
  }
  // 情绪归一窗口：访客不可修改，但仍可调整展示日期和走势天数
  const sw = $("s-window"), sws = $("s-window-save");
  if (sw) sw.disabled = !admin;
  if (sws) { sws.disabled = !admin; sws.title = admin ? "" : "访客不能修改归一化窗口"; }
  // 选股来源：关注和持仓仅管理员可见；服务端仍是最终安全边界
  const category = $("sl-category");
  category?.querySelectorAll("[data-admin-only-category]").forEach((option) => {
    option.hidden = !admin;
    option.disabled = !admin;
  });
  if (!admin && category && ["watch", "holding"].includes(category.value)) {
    category.value = "";
  }
  if (!admin) {
    _selectionsLoaded = false;
    _selectionRows = [];
    _selectionData = {};
    _selectionTag = "";
    _portfolioLoaded = false;
    _portfolioRows = [];
    _portfolioSelected = null;
    if ($("sl-meta")) $("sl-meta").textContent = "";
    if ($("sl-list-meta")) $("sl-list-meta").textContent = "";
    if ($("sl-tag-meta")) $("sl-tag-meta").textContent = "";
    if ($("sl-tags")) $("sl-tags").innerHTML = '<span class="selection-tag-empty">查询后生成标签统计</span>';
    if ($("sl-clear-tag")) $("sl-clear-tag").disabled = true;
    if ($("sl-result")) $("sl-result").innerHTML = '<div class="empty">进入页面后自动加载</div>';
    if ($("pf-search-results")) $("pf-search-results").classList.add("hidden");
    if ($("pf-list")) $("pf-list").innerHTML = '<div class="empty">仅管理员可查看自选</div>';
  }
  // 回测结果对管理员和访客均可查看，但访客结果由服务端排除关注和持仓
  const br = $("b-run");
  if (br) { br.disabled = false; br.title = "加载当前权限可见的回测结果"; }
  // 顶部角色徽标
  const badge = $("role-badge");
  if (badge) {
    badge.textContent = admin ? "管理员" : "访客";
    badge.className = "role-badge " + (admin ? "admin" : "user");
  }
  // 权重配置页若已渲染，禁用其输入与保存
  document.querySelectorAll("#w-models input[data-f]").forEach((i) => (i.disabled = !admin));
  document.querySelectorAll("#w-models [data-save]").forEach((b) => {
    b.disabled = !admin; b.title = admin ? "" : "访客不可修改权重";
  });
  // 访客 Key 管理面板（仅管理员，且设置弹窗打开时）
  refreshUserKeysPanel();
}

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (!_authReady) {
      openLogin("请先输入有效的服务 Key");
      return;
    }
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "weights") loadWeights();
    if (btn.dataset.tab === "backtest") loadBacktest();
    if (btn.dataset.tab === "precompute") loadPrecompute();
    if (btn.dataset.tab === "sentiment") loadSentiment();
    if (btn.dataset.tab === "industry") loadIndustry();
    if (btn.dataset.tab === "selections") loadSelections(true);
    if (btn.dataset.tab === "portfolio") loadPortfolio();
  });
});

/* ---------- login ---------- */
$("login-submit").onclick = login;
$("login-key").addEventListener("keydown", (e) => {
  if (e.key === "Enter") login();
});

/* ---------- settings ---------- */
$("settingsBtn").onclick = () => {
  if (!_authReady) {
    openLogin("请先输入有效的服务 Key");
    return;
  }
  $("cfg-base").value = cfg.base;
  $("cfg-key").value = cfg.key;
  $("cfg-status").textContent = "";
  $("settings").classList.remove("hidden");
  refreshUserKeysPanel();
};

/* ---------- 访客 Key 管理（管理员） ---------- */
// 调用 /admin/* 端点（非 /call 通道）
async function adminApi(path, method = "GET", body = null) {
  const opt = { method, headers: { "X-API-Key": cfg.key } };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(cfg.base.replace(/\/$/, "") + path, opt);
  const data = await res.json();
  if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`);
  return data.data !== undefined ? data.data : data;
}

function refreshUserKeysPanel() {
  const box = $("userkeys");
  if (!box) return;
  const settingsOpen = !$("settings").classList.contains("hidden");
  if (isAdmin() && cfg.key && settingsOpen) { box.classList.remove("hidden"); loadUserKeys(); }
  else box.classList.add("hidden");
}

async function loadUserKeys() {
  const list = $("uk-list");
  list.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const d = await adminApi("/admin/user-keys");
    const keys = d.keys || [];
    if (!keys.length) { list.innerHTML = '<div class="empty">暂无访客 Key，点上方按钮生成</div>'; return; }
    list.innerHTML = keys.map((k) => `
      <div class="uk-item ${k.disabled ? "off" : ""}">
        <div class="uk-info">
          <b>${esc(k.label || "访客")}</b>${k.disabled ? '<span class="uk-tag">已停用</span>' : ""}
          <code class="uk-key">${esc(k.key || "")}</code>
          <small>${esc(k.created_at || "")}</small>
        </div>
        <div class="uk-ops">
          <button class="btn-ghost" data-uk-copy="${esc(k.key || "")}">复制</button>
          <button class="btn-ghost" data-uk-toggle="${esc(k.id || "")}">${k.disabled ? "启用" : "停用"}</button>
          <button class="btn-ghost uk-del" data-uk-del="${esc(k.id || "")}">删除</button>
        </div>
      </div>`).join("");
  } catch (e) { list.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>"; }
}

$("uk-create").onclick = async () => {
  const label = $("uk-label").value.trim();
  try {
    await adminApi("/admin/user-keys", "POST", { label });
    $("uk-label").value = "";
    toast("已生成访客 Key", "ok");
    loadUserKeys();
  } catch (e) { toast("生成失败：" + e.message, "bad"); }
};

$("uk-list").addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-uk-copy]");
  const tog = e.target.closest("[data-uk-toggle]");
  const del = e.target.closest("[data-uk-del]");
  if (copy) {
    try { await navigator.clipboard.writeText(copy.getAttribute("data-uk-copy")); toast("Key 已复制", "ok"); }
    catch (err) { toast("复制失败，请手动选择", "bad"); }
  } else if (tog) {
    try { await adminApi("/admin/user-keys/toggle", "POST", { id: tog.getAttribute("data-uk-toggle") }); loadUserKeys(); }
    catch (err) { toast("操作失败：" + err.message, "bad"); }
  } else if (del) {
    if (!confirm("删除后该访客 Key 立即失效，确认删除？")) return;
    try { await adminApi("/admin/user-keys/delete", "POST", { id: del.getAttribute("data-uk-del") }); loadUserKeys(); }
    catch (err) { toast("删除失败：" + err.message, "bad"); }
  }
});
$("settings").addEventListener("click", (e) => { if (e.target.id === "settings") $("settings").classList.add("hidden"); });
function reloadActiveTab() {
  const active = document.querySelector(".tab.active");
  const t = active && active.dataset.tab;
  if (t === "weights") loadWeights();
  else if (t === "backtest") loadBacktest(true);
  else if (t === "precompute") loadPrecompute(true);
  else if (t === "sentiment") { _sentLoaded = false; loadSentiment(); }
  else if (t === "industry") loadIndustry(true);
  else if (t === "selections") loadSelections(true);
  else if (t === "portfolio") loadPortfolio(true);
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
      await refreshRole();
      toast(`已连接：功能数 ${h.functions}（${isAdmin() ? "管理员" : "访客"}）`, "ok");
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
    await refreshRole();
    s.textContent = `连通 ✓ 角色=${isAdmin() ? "管理员" : "访客"} 交易日=${h.trade_open} 功能数=${h.functions} 版本=${h.data_version}`;
    s.className = "status ok";
  } catch (e) { s.textContent = "连接失败：" + e.message; s.className = "status bad"; }
};

/* ---------- 量化选股 ---------- */
const splitSearchTerms = (value) => value.split(/[，,]/).map((s) => s.trim()).filter(Boolean);
const quantHelpModal = $("quant-help-modal");
$("q-help").onclick = () => quantHelpModal.classList.remove("hidden");
$("quant-help-close").onclick = () => quantHelpModal.classList.add("hidden");
quantHelpModal.addEventListener("click", (e) => {
  if (e.target === quantHelpModal) quantHelpModal.classList.add("hidden");
});

const BOARD_CN = { main: "沪深主板", star: "科创板", gem: "创业板" };

$("q-run").onclick = async () => {
  const btn = $("q-run"); btn.disabled = true;
  const stockNames = $("q-stock-names").value.trim();
  const industries = $("q-industries").value.trim();
  const boardInputs = [...document.querySelectorAll('#q-boards input[type="checkbox"]')];
  const boards = boardInputs.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value);
  if (!boards.length) { toast("请至少选择一个市场范围", "bad"); btn.disabled = false; return; }
  const params = { top_n: Number($("q-topn").value) || 30 };
  if (stockNames) params.stock_names = splitSearchTerms(stockNames);
  else if (industries) params.industries = splitSearchTerms(industries);
  if (boards.length < boardInputs.length) params.boards = boards; // 全选等价于省略参数
  $("q-result").innerHTML = '<div class="empty">运行中…</div>';
  try {
    const d = await call("screen_quant", params);
    const rows = d.candidates || [];
    const scope = d.filter_type === "stock_names" ? "个股" : d.filter_type === "industries" ? "板块" : "全市场";
    const boardText = Array.isArray(d.boards) && d.boards.length && d.boards.length < Object.keys(BOARD_CN).length
      ? " · " + d.boards.map((board) => BOARD_CN[board] || board).join("/") : "";
    $("q-meta").textContent = `${d.trade_date || ""} · ${scope}${boardText} · ${rows.length} 只`;
    renderCandidates(rows);
    if (!rows.length && d.note) toast(d.note, "bad");
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

// 量化选股候选：中文列 + 标的(名称/代码) + 最新行情 + 可排序
let _quantRows = [];
let _quantSort = { key: "score", dir: "desc" };
const QUANT_COLS = [
  { key: "__ticker", label: "标的", sort: "code" },
  { key: "last", label: "最新价" },
  { key: "chg", label: "当日涨幅", pct: true },
  { key: "ret5", label: "近5日", pct: true },
  { key: "industry_name", label: "所属行业" },
  { key: "industry_strength", label: "行业强度" },
  { key: "industry_score", label: "行业原始分" },
  { key: "mom_12_1", label: "12-1动量" },
  { key: "reversal_1m", label: "1月反转" },
  { key: "trend_ma", label: "均线多头" },
  { key: "high_52w", label: "距52周高" },
  { key: "low_ivol", label: "低波动" },
  { key: "low_turnover", label: "低换手" },
  { key: "vol_confirm", label: "量能" },
  // 候选因子（后端仅在权重≠0 时返回，届时自动出现在列表）
  { key: "mom_6_1", label: "6-1动量" },
  { key: "max_lottery", label: "彩票效应⁻" },
  { key: "downside_vol", label: "下行波动⁻" },
  { key: "amihud_illiq", label: "非流动性" },
  { key: "small_size", label: "小市值" },
  { key: "value_bm", label: "账面市值比" },
  { key: "earnings_yield", label: "盈利收益率" },
  { key: "score", label: "综合分" },
];
const QUANT_ALWAYS = ["__ticker", "last", "chg", "ret5", "score"];

function renderCandidates(cands) {
  _quantRows = cands || [];
  _quantSort = { key: "score", dir: "desc" };
  drawQuant();
}

function drawQuant() {
  const box = $("q-result");
  if (!_quantRows.length) { box.innerHTML = '<div class="empty">无候选</div>'; return; }
  const cols = QUANT_COLS.filter((c) => QUANT_ALWAYS.includes(c.key) || _quantRows.some((r) => c.key in r));
  const { key, dir } = _quantSort;
  const val = (r) => (key === "code" ? (r.code || "") : r[key]);
  const sorted = [..._quantRows].sort((a, b) => {
    let va = val(a), vb = val(b);
    if (va == null) va = -Infinity;
    if (vb == null) vb = -Infinity;
    if (va < vb) return dir === "asc" ? -1 : 1;
    if (va > vb) return dir === "asc" ? 1 : -1;
    return 0;
  });
  const head = cols.map((c) => {
    const sk = c.sort || c.key;
    const arrow = sk === key ? (dir === "asc" ? " ▲" : " ▼") : "";
    return `<th class="sortable" data-sort="${sk}" title="点击排序">${c.label}${arrow}</th>`;
  }).join("");
  const body = sorted.map((r) => "<tr>" + cols.map((c) => {
    if (c.key === "__ticker") return `<td class="cell-ticker"><b>${r.name || "-"}</b><span>${r.code || ""}</span></td>`;
    let v = r[c.key], cls = "";
    if (typeof v === "number") {
      if (c.pct || c.key === "score") cls = v >= 0 ? "pos" : "neg";
      v = c.pct ? (v >= 0 ? "+" : "") + v.toFixed(2) + "%" : (Number.isInteger(v) ? v : v.toFixed(3));
    }
    return `<td class="${cls}">${v == null ? "-" : v}</td>`;
  }).join("") + "</tr>").join("");
  box.innerHTML = `<table class="sortable-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

$("q-result").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const k = th.getAttribute("data-sort");
  if (_quantSort.key === k) _quantSort.dir = _quantSort.dir === "asc" ? "desc" : "asc";
  else _quantSort = { key: k, dir: k === "code" ? "asc" : "desc" };
  drawQuant();
});

/* ---------- 行业量化分析 ---------- */
let _industryLoaded = false;
async function loadIndustry(force = false) {
  if (_industryLoaded && !force) return;
  _industryLoaded = true;
  const box = $("i-result");
  box.innerHTML = '<div class="empty">加载行业评分中…</div>';
  try {
    const d = await call("screen_sector", { top_n: Number($("i-topn").value) || 31 });
    const sectors = d.sectors || [];
    $("i-meta").textContent = `${d.trade_date || ""} · ${d.data_source === "persisted" ? "盘后持久化" : "实时计算"} · ${sectors.length} 个行业`;
    const rows = sectors.map((r, index) => ({
      排名: index + 1, 行业: r.name, 代码: r.code,
      行业强度: r.percentile == null ? null : r.percentile * 100,
      综合分: r.score, "12-1动量": r.sec_mom_12_1,
      "20日动量": r.sec_mom_20d, "5日动量": r.sec_mom_5d,
      量能确认: r.sec_vol_confirm, 低波动: r.sec_low_vol,
    }));
    box.innerHTML = rows.length ? renderTable(rows) : '<div class="empty">暂无行业评分，请先在盘后运行因子预计算</div>';
  } catch (e) {
    box.innerHTML = '<div class="empty">行业评分加载失败：' + esc(e.message) + "</div>";
  }
}
$("i-run").onclick = () => loadIndustry(true);

/* ---------- 量化选股看板 ---------- */
const CATEGORY_LABEL = { auto: "每日自动", manual: "用户触发", watch: "关注", holding: "持仓" };
const fmtMaybe = (value, digits = 2) => value == null ? "—" : Number(value).toFixed(digits);
const pctText = (value) => value == null ? "—" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
let _selectionRows = [];
let _selectionData = {};
let _selectionTag = "";
let _selectionSort = "grouped";

const selectionScore = (row) => {
  const value = row.score_percentile ?? row.score;
  const score = Number(value);
  return Number.isFinite(score) ? score : null;
};
const selectionScoreText = (row) => {
  const score = selectionScore(row);
  return score == null ? "—" : (score >= 0 && score <= 1 ? (score * 100).toFixed(1) : score.toFixed(2));
};

function selectionTagsFor(row) {
  let labels = Array.isArray(row.tags)
    ? row.tags.map((tag) => String(tag || "").trim()).filter(Boolean)
    : [];
  if (!labels.length) {
    labels = [row.market_role, row.hotspot, row.driver]
      .map((tag) => String(tag || "").trim())
      .filter((tag) => tag && !["未标注", "非主线", "未分类"].includes(tag));
  }
  labels = [...new Set(labels)];
  if (!labels.length) labels = ["未分类"];
  return labels.map((label) => ({
    key: `tag:${label}`,
    label,
    leader: label === "龙头",
    core: label === "核心",
    unclassified: label === "未分类",
  }));
}

const selectionPriority = (row) => {
  const labels = selectionTagsFor(row).map((tag) => tag.label);
  if (labels.includes("龙头")) return 0;
  if (labels.includes("核心") || String(row.market_role || "").trim() === "核心") return 1;
  return 2;
};
const isCoreSelection = (row) => selectionPriority(row) < 2;

function renderSelectionTags() {
  const counts = new Map();
  _selectionRows.forEach((row) => selectionTagsFor(row).forEach((tag) => {
    const current = counts.get(tag.key) || { ...tag, count: 0 };
    current.count += 1;
    counts.set(tag.key, current);
  }));
  if (_selectionTag && !counts.has(_selectionTag)) _selectionTag = "";
  const tags = [...counts.values()].sort((a, b) =>
    Number(b.leader) - Number(a.leader)
    || Number(b.core) - Number(a.core)
    || Number(a.unclassified) - Number(b.unclassified)
    || b.count - a.count
    || a.label.localeCompare(b.label, "zh-CN"));
  $("sl-tags").innerHTML = tags.length ? tags.map((tag) =>
    `<button type="button" class="selection-tag ${tag.leader ? "leader" : ""} ${tag.core ? "core" : ""} ${tag.unclassified ? "unclassified" : ""} ${_selectionTag === tag.key ? "active" : ""}" data-selection-tag="${esc(tag.key)}">${esc(tag.label)} <b>${tag.count}</b></button>`
  ).join("") : '<span class="selection-tag-empty">当前结果没有可统计标签</span>';
  const active = counts.get(_selectionTag);
  $("sl-tag-meta").textContent = active ? `已选：${active.label}` : `${_selectionRows.length} 只股票`;
  $("sl-clear-tag").disabled = !_selectionTag;
}

function sortedSelectionRows(rows, grouped = false) {
  return [...rows].sort((a, b) => {
    if (grouped) {
      const priorityOrder = selectionPriority(a) - selectionPriority(b);
      if (priorityOrder) return priorityOrder;
    }
    if (!grouped && _selectionSort === "latest") {
      const dateOrder = String(b.selected_at || b.logged_at || b.date || "").localeCompare(String(a.selected_at || a.logged_at || a.date || ""));
      if (dateOrder) return dateOrder;
    } else {
      const direction = !grouped && _selectionSort === "score-asc" ? 1 : -1;
      const aScore = selectionScore(a);
      const bScore = selectionScore(b);
      const left = aScore == null ? (direction > 0 ? Infinity : -Infinity) : aScore;
      const right = bScore == null ? (direction > 0 ? Infinity : -Infinity) : bScore;
      const scoreOrder = direction * (left - right);
      if (scoreOrder) return scoreOrder;
    }
    const dateOrder = String(b.date || "").localeCompare(String(a.date || ""));
    if (dateOrder) return dateOrder;
    return Number(a.screening_rank ?? Number.MAX_SAFE_INTEGER) - Number(b.screening_rank ?? Number.MAX_SAFE_INTEGER);
  });
}

function selectionCard(r) {
  const priority = selectionPriority(r);
  const priorityClass = priority === 0 ? "leader" : (priority === 1 ? "core" : "");
  const sinceCls = r.since_selection_pct == null ? "" : (r.since_selection_pct >= 0 ? "pos" : "neg");
  const chgCls = r.latest_chg_pct == null ? "" : (r.latest_chg_pct >= 0 ? "pos" : "neg");
  const amountYi = r.amount == null ? "—" : `${(Number(r.amount) / 100000).toFixed(2)} 亿`;
  const factorEntries = Object.entries(r.factors || {}).filter(([key]) => key !== "_meta");
  const factors = r.factor_error
    ? `因子快照缺失：${esc(r.factor_error)}`
    : (factorEntries.length
      ? factorEntries.map(([key, value]) => `${esc(factorLabel(key))}：${typeof value === "number" ? value.toFixed(4) : esc(typeof value === "object" ? JSON.stringify(value) : value)}`).join(" ｜ ")
      : "未保存量化因子快照");
  const rowTags = selectionTagsFor(r).slice(0, 4).map((tag) =>
    `<span class="selection-row-tag ${tag.leader ? "leader" : ""} ${tag.core ? "core" : ""} ${tag.unclassified ? "unclassified" : ""}">${esc(tag.label)}</span>`).join("");
  const rank = r.screening_rank == null ? "—" : `#${esc(r.screening_rank)}`;
  const deleteButton = isAdmin()
    ? `<button type="button" class="selection-delete" data-selection-delete="${esc(r.id)}">永久删除</button>` : "";
  return `<details class="selection-item ${priorityClass}" data-selection-id="${esc(r.id)}">
    <summary class="selection-summary">
      <div class="selection-ticker"><b class="${priorityClass ? "core-name" : ""}">${esc(r.name || "-")}</b><span>${esc(r.code)}</span></div>
      <div class="selection-stat"><small>选股评分</small><b>${selectionScoreText(r)}</b></div>
      <div class="selection-stat"><small>选股后</small><b class="${sinceCls}">${pctText(r.since_selection_pct)}</b></div>
      <div class="selection-stat selection-latest"><small>最新价</small><b>${fmtMaybe(r.latest_price)}</b></div>
      <div class="selection-row-tags">${rowTags}</div>
      <div class="selection-date">${esc(r.date)}<small>${esc(CATEGORY_LABEL[r.category] || r.category)}</small></div>
      <span class="selection-expand" aria-hidden="true"></span>
    </summary>
    <div class="selection-detail-body">
      <div class="selection-detail-grid">
        <div><small>选股价</small><b>${fmtMaybe(r.selected_price)}</b></div>
        <div><small>最新价</small><b>${fmtMaybe(r.latest_price)}</b></div>
        <div><small>实时涨幅</small><b class="${chgCls}">${pctText(r.latest_chg_pct)}</b></div>
        <div><small>换手率</small><b>${pctText(r.turnover_rate)}</b></div>
        <div><small>最近成交额</small><b>${amountYi}</b></div>
        <div><small>筛选排名</small><b>${rank}</b></div>
      </div>
      <div class="selection-context"><b>核心事件：</b>${esc(r.core_event || r.event || "未标注")}</div>
      <div class="selection-context"><b>入选理由：</b>${esc(r.reason || "未填写")}</div>
      <div class="selection-detail-footer">
        <div class="factor-snapshot-wrap">
          <details class="factor-snapshot"><summary>查看因子快照</summary><p>${factors}</p></details>
          <div class="selection-audit">选股 ${esc(r.selected_at || r.logged_at || "—")} · 行情 ${esc(r.latest_quote_time || "—")} · 原始分 ${fmtMaybe(r.score_raw, 4)} · 运行 ${esc(r.screening_run_id || "legacy")}</div>
        </div>
        ${deleteButton}
      </div>
    </div>
  </details>`;
}

function renderSelectionRows() {
  const filtered = _selectionTag
    ? _selectionRows.filter((row) => selectionTagsFor(row).some((tag) => tag.key === _selectionTag))
    : _selectionRows;
  const grouped = _selectionSort === "grouped";
  $("sl-list-meta").textContent = `显示 ${filtered.length} / 查询 ${_selectionRows.length}${grouped ? " · 聚合展示" : " · 全局排序"}`;
  const quoteLabel = _selectionData.mixed_quote_dates
    ? `${_selectionData.quote_trade_date_min || "?"}–${_selectionData.quote_trade_date_max || "?"}（混合行情日）`
    : (_selectionData.quote_trade_date || "行情不可用");
  const quoteErrors = Array.isArray(_selectionData.quote_errors) ? _selectionData.quote_errors : [];
  $("sl-meta").textContent = `${quoteLabel} · ${_selectionRows.length} 条${quoteErrors.length ? ` · 行情错误 ${quoteErrors.length} 条` : ""}`;
  $("sl-meta").title = quoteErrors.join("\n");
  $("sl-refreshed").textContent = _selectionData.refreshed_at ? `最近刷新 ${_selectionData.refreshed_at}` : "尚未刷新";
  if (!filtered.length) {
    $("sl-result").innerHTML = `<div class="empty">${_selectionRows.length ? "当前标签下没有股票" : "没有符合条件的选股记录"}</div>`;
    return;
  }
  if (!grouped) {
    $("sl-result").innerHTML = sortedSelectionRows(filtered).map(selectionCard).join("");
    return;
  }

  const groupByDate = _selectionData.group_by === "date";
  const groups = new Map();
  filtered.forEach((row) => {
    const key = groupByDate ? String(row.date || "日期未知") : String(row.primary_theme || "未分类");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  });
  const entries = [...groups.entries()];
  entries.sort((a, b) => {
    if (groupByDate) return b[0].localeCompare(a[0]);
    const bestA = sortedSelectionRows(a[1], true)[0];
    const bestB = sortedSelectionRows(b[1], true)[0];
    return selectionPriority(bestA) - selectionPriority(bestB)
      || (selectionScore(bestB) ?? -Infinity) - (selectionScore(bestA) ?? -Infinity)
      || a[0].localeCompare(b[0], "zh-CN");
  });
  $("sl-result").innerHTML = entries.map(([name, groupRows]) =>
    `<section class="selection-group"><div class="selection-group-head"><b>${esc(name)}</b><span>${groupRows.length} 只</span></div><div class="selection-group-list">${sortedSelectionRows(groupRows, true).map(selectionCard).join("")}</div></section>`
  ).join("");
}

function drawSelections(data) {
  _selectionData = data || {};
  _selectionRows = Array.isArray(data?.rows) ? data.rows : [];
  _selectionTag = "";
  if (!$("sl-from").value && data?.date_from) $("sl-from").value = String(data.date_from).slice(0, 10);
  if (!$("sl-to").value && data?.date_to) $("sl-to").value = String(data.date_to).slice(0, 10);
  renderSelectionTags();
  renderSelectionRows();
}

async function loadSelections(force = false) {
  if (_selectionsLoaded && !force) return;
  _selectionsLoaded = true;
  $("sl-result").innerHTML = '<div class="empty">加载选股记录并刷新实时行情…</div>';
  const refreshButton = $("sl-refresh-quotes");
  refreshButton.disabled = true;
  refreshButton.textContent = "刷新中…";
  const params = { limit: 500 };
  const from = $("sl-from").value.replaceAll("-", "");
  const to = $("sl-to").value.replaceAll("-", "");
  const hotspot = $("sl-hotspot").value.trim();
  const category = $("sl-category").value;
  if (from) params.date_from = from;
  if (to) params.date_to = to;
  if (hotspot) params.hotspot = hotspot;
  if (category) params.category = category;
  try { drawSelections(await call("selection_dashboard", params)); }
  catch (e) {
    _selectionsLoaded = false;
    _selectionRows = [];
    _selectionData = {};
    renderSelectionTags();
    $("sl-list-meta").textContent = "";
    $("sl-refreshed").textContent = "刷新失败";
    $("sl-result").innerHTML = '<div class="empty">选股看板加载失败：' + esc(e.message) + "</div>";
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "刷新行情";
  }
}
$("sl-run").onclick = () => loadSelections(true);
$("sl-refresh-quotes").onclick = () => loadSelections(true);
$("sl-tags").addEventListener("click", (e) => {
  const tag = e.target.closest("[data-selection-tag]");
  if (!tag) return;
  const key = tag.getAttribute("data-selection-tag");
  _selectionTag = _selectionTag === key ? "" : key;
  renderSelectionTags();
  renderSelectionRows();
});
$("sl-clear-tag").onclick = () => {
  _selectionTag = "";
  renderSelectionTags();
  renderSelectionRows();
};
$("sl-sort").onchange = (e) => {
  _selectionSort = e.target.value;
  renderSelectionRows();
};
$("sl-result").addEventListener("click", async (e) => {
  const button = e.target.closest("[data-selection-delete]");
  if (!button) return;
  if (!isAdmin()) { toast("仅管理员可删除选股记录", "bad"); return; }
  const id = Number(button.getAttribute("data-selection-delete"));
  const row = _selectionRows.find((item) => Number(item.id) === id);
  if (!row) { toast("记录已不在当前列表，请重新查询", "bad"); return; }
  const title = `${row.name || "未命名"}（${row.code}）`;
  if (!confirm(`高风险操作：将永久删除 ${title} 在 ${row.date} 的选股记录，并一并删除两版关联收益。历史回测快照会保留，但本操作不可恢复。\n\n确认继续？`)) return;
  const input = prompt(`最后确认：请输入股票代码 ${row.code}`);
  if (input == null) return;
  if (input.trim().toUpperCase() !== String(row.code || "").trim().toUpperCase()) {
    toast("股票代码不匹配，已取消删除", "bad");
    return;
  }
  button.disabled = true;
  button.textContent = "删除中…";
  try {
    const result = await adminApi("/admin/selections/delete", "POST", { id, confirm_code: input });
    _selectionRows = _selectionRows.filter((item) => Number(item.id) !== id);
    renderSelectionTags();
    renderSelectionRows();
    const counts = result.deleted_counts || {};
    toast(`已删除选股记录，关联收益 ${Number(counts.selection_forward_returns_v2 || 0) + Number(counts.selection_forward_returns || 0)} 条`, "ok");
  } catch (err) {
    button.disabled = false;
    button.textContent = "永久删除";
    toast("删除失败：" + err.message, "bad");
  }
});

/* ---------- 管理员自选（关注与持仓） ---------- */
function setPortfolioTypeFields() {
  const holding = $("pf-type").value === "holding";
  $("pf-cost").disabled = !holding;
  $("pf-lots").disabled = !holding;
  if (!holding) { $("pf-cost").value = ""; $("pf-lots").value = ""; }
}

function resetPortfolioForm() {
  _portfolioSelected = null;
  _portfolioSearchSeq += 1;
  $("pf-search").value = "";
  $("pf-search-results").innerHTML = "";
  $("pf-search-results").classList.add("hidden");
  $("pf-selected").innerHTML = "尚未选择股票";
  $("pf-selected").classList.add("empty-compact");
  $("pf-type").value = "watch";
  $("pf-cost").value = "";
  $("pf-lots").value = "";
  $("pf-note").value = "";
  $("pf-save").disabled = true;
  $("pf-status").textContent = "";
  $("pf-status").className = "status";
  setPortfolioTypeFields();
}

function selectPortfolioStock(stock, existing = null) {
  _portfolioSelected = { code: stock.code, name: stock.name };
  $("pf-search").value = `${stock.name} ${stock.code}`;
  $("pf-selected").innerHTML = `<b>${esc(stock.name)}</b><code>${esc(stock.code)}</code>`;
  $("pf-selected").classList.remove("empty-compact");
  $("pf-search-results").classList.add("hidden");
  $("pf-save").disabled = false;
  $("pf-status").textContent = "";
  $("pf-status").className = "status";
  if (existing) {
    $("pf-type").value = existing.type;
    $("pf-cost").value = existing.cost_price ?? "";
    $("pf-lots").value = existing.lots ?? "";
    $("pf-note").value = existing.note || "";
  }
  setPortfolioTypeFields();
}

function renderPortfolio(data) {
  _portfolioRows = Array.isArray(data?.rows) ? data.rows : [];
  $("pf-version").textContent = `版本 ${data?.portfolio_version || "--"}`;
  $("pf-holding-count").textContent = String(data?.holding_count ?? _portfolioRows.filter((row) => row.type === "holding").length);
  $("pf-watch-count").textContent = String(data?.watch_count ?? _portfolioRows.filter((row) => row.type === "watch").length);
  $("pf-total-count").textContent = String(_portfolioRows.length);
  if (!_portfolioRows.length) {
    $("pf-list").innerHTML = '<div class="empty">暂无自选，请先搜索股票并选择添加</div>';
    return;
  }
  const rows = [..._portfolioRows].sort((a, b) =>
    Number(b.type === "holding") - Number(a.type === "holding") || String(a.code).localeCompare(String(b.code)));
  $("pf-list").innerHTML = rows.map((row) => {
    const holding = row.type === "holding";
    const details = holding
      ? `<span><small>持仓成本</small><b>${fmtMaybe(row.cost_price, 4)}</b></span><span><small>持仓手数</small><b>${esc(row.lots)} 手</b></span><span><small>对应股数</small><b>${esc(row.shares)} 股</b></span>`
      : '<span><small>状态</small><b>持续关注</b></span>';
    return `<article class="portfolio-item ${holding ? "holding" : "watch"}" data-portfolio-code="${esc(row.code)}">
      <div class="portfolio-item-main">
        <div class="portfolio-ticker"><b>${esc(row.name)}</b><code>${esc(row.code)}</code></div>
        <span class="portfolio-type ${holding ? "holding" : "watch"}">${holding ? "持仓" : "关注"}</span>
        <div class="portfolio-item-metrics">${details}</div>
        <div class="portfolio-item-time"><small>最近更新</small><span>${esc(row.updated_at || "—")}</span></div>
      </div>
      <div class="portfolio-item-note">${esc(row.note || "暂无备注")}</div>
      <div class="portfolio-item-actions"><button type="button" class="btn-ghost" data-portfolio-edit="${esc(row.code)}">编辑</button><button type="button" class="portfolio-remove" data-portfolio-remove="${esc(row.code)}">移除</button></div>
    </article>`;
  }).join("");
}

async function loadPortfolio(force = false) {
  if (!isAdmin() || (_portfolioLoaded && !force)) return;
  _portfolioLoaded = true;
  $("pf-list").innerHTML = '<div class="empty">正在读取当前自选…</div>';
  try { renderPortfolio(await call("portfolio_get", {})); }
  catch (e) {
    _portfolioLoaded = false;
    $("pf-list").innerHTML = `<div class="empty">自选加载失败：${esc(e.message)}</div>`;
  }
}

async function searchPortfolioStocks() {
  const seq = ++_portfolioSearchSeq;
  const query = $("pf-search").value.trim();
  const box = $("pf-search-results");
  if (!query) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = '<div class="portfolio-search-message">搜索中…</div>';
  try {
    const data = await call("portfolio_stock_search", { query, limit: 12 });
    if (seq !== _portfolioSearchSeq || query !== $("pf-search").value.trim()) return;
    const rows = data.rows || [];
    box.innerHTML = rows.length ? rows.map((row) =>
      `<button type="button" class="portfolio-search-option" data-pf-code="${esc(row.code)}" data-pf-name="${esc(row.name)}"><span><b>${esc(row.name)}</b><code>${esc(row.code)}</code></span><small>${esc(row.industry || row.market || "行业未标注")}</small></button>`
    ).join("") : '<div class="portfolio-search-message">没有匹配股票，请换名称或代码片段</div>';
  } catch (e) {
    if (seq !== _portfolioSearchSeq) return;
    box.innerHTML = `<div class="portfolio-search-message bad">搜索失败：${esc(e.message)}</div>`;
  }
}

$("pf-type").onchange = setPortfolioTypeFields;
$("pf-reset").onclick = resetPortfolioForm;
$("pf-refresh").onclick = () => loadPortfolio(true);
$("pf-search-btn").onclick = searchPortfolioStocks;
$("pf-search").addEventListener("input", () => {
  _portfolioSearchSeq += 1;
  _portfolioSelected = null;
  $("pf-selected").textContent = "请从搜索结果中选择股票";
  $("pf-selected").classList.add("empty-compact");
  $("pf-save").disabled = true;
  $("pf-status").textContent = "";
  $("pf-status").className = "status";
  clearTimeout(_portfolioSearchTimer);
  _portfolioSearchTimer = setTimeout(searchPortfolioStocks, 280);
});
$("pf-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); clearTimeout(_portfolioSearchTimer); searchPortfolioStocks(); }
});
$("pf-search-results").addEventListener("click", (e) => {
  const option = e.target.closest("[data-pf-code]");
  if (!option) return;
  selectPortfolioStock({ code: option.getAttribute("data-pf-code"), name: option.getAttribute("data-pf-name") });
});
$("pf-save").onclick = async () => {
  if (!isAdmin()) { toast("仅管理员可修改自选", "bad"); return; }
  if (!_portfolioSelected) { toast("请先搜索并选择股票", "bad"); return; }
  const type = $("pf-type").value;
  const item = { ..._portfolioSelected, type, note: $("pf-note").value.trim() };
  if (type === "holding") {
    item.cost_price = Number($("pf-cost").value);
    item.lots = Number($("pf-lots").value);
    if (!(item.cost_price > 0) || !(Number.isInteger(item.lots) && item.lots > 0)) {
      toast("持仓必须填写大于0的成本和整数手数", "bad"); return;
    }
  }
  const button = $("pf-save");
  button.disabled = true;
  $("pf-status").className = "status";
  $("pf-status").textContent = "正在保存…";
  try {
    const data = await call("portfolio_upload", { items: [item], source: "web-admin" });
    renderPortfolio(data);
    resetPortfolioForm();
    toast(data.changed ? "自选已保存，数据版本已更新" : "内容无变化，无需更新", "ok");
  } catch (e) {
    button.disabled = false;
    $("pf-status").textContent = "保存失败：" + e.message;
    $("pf-status").className = "status bad";
  }
};
$("pf-list").addEventListener("click", async (e) => {
  const edit = e.target.closest("[data-portfolio-edit]");
  const remove = e.target.closest("[data-portfolio-remove]");
  const code = (edit || remove)?.getAttribute(edit ? "data-portfolio-edit" : "data-portfolio-remove");
  if (!code) return;
  const row = _portfolioRows.find((item) => item.code === code);
  if (!row) { toast("记录已变化，请刷新", "bad"); return; }
  if (edit) {
    selectPortfolioStock({ code: row.code, name: row.name }, row);
    $("pf-search").scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (!confirm(`确认从自选中移除 ${row.name}（${row.code}）？该操作会更新自选数据版本。`)) return;
  remove.disabled = true;
  try {
    const data = await call("portfolio_upload", { items: [{ code: row.code, deleted: true }], source: "web-admin" });
    renderPortfolio(data);
    if (_portfolioSelected?.code === row.code) resetPortfolioForm();
    toast("已从自选移除", "ok");
  } catch (err) { remove.disabled = false; toast("移除失败：" + err.message, "bad"); }
});
setPortfolioTypeFields();

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
  setSentimentDefaultDate().then(() => Promise.all([syncSentimentWindow(), runSentiment()]));
}

async function setSentimentDefaultDate() {
  const input = $("s-date");
  if (!input || input.value.trim()) return;
  try {
    const h = await health();
    const serverDate = String(h.date || "").trim();
    if (/^\d{8}$/.test(serverDate) && h.trade_open !== false) {
      input.value = `${serverDate.slice(0, 4)}-${serverDate.slice(4, 6)}-${serverDate.slice(6, 8)}`;
    }
  } catch (e) { /* 健康检查失败时保留空值，由服务端选择最近交易日 */ }
}

async function syncSentimentWindow() {
  try {
    const c = await call("get_sentiment_config", {});
    $("s-window").value = c.window;
    $("s-window").min = c.range?.[0] ?? 3;
    $("s-window").max = c.range?.[1] ?? 30;
    $("s-current-window").textContent = `归一窗口：${c.window} 日`;
  } catch (e) { /* 看板读取仍可继续 */ }
}

function openSentimentSettings() {
  $("s-window-status").textContent = "";
  $("s-window-save").disabled = !isAdmin();
  $("s-window-save").title = isAdmin() ? "保存服务端归一窗口并刷新" : "访客不能修改归一窗口";
  $("sentiment-settings").classList.remove("hidden");
}

$("s-settings-btn").onclick = openSentimentSettings;
$("s-settings-cancel").onclick = () => $("sentiment-settings").classList.add("hidden");
$("sentiment-settings").addEventListener("click", (e) => {
  if (e.target.id === "sentiment-settings") $("sentiment-settings").classList.add("hidden");
});
$("s-refresh").onclick = () => {
  $("sentiment-settings").classList.add("hidden");
  runSentiment();
};

$("s-window-save").onclick = async () => {
  if (!isAdmin()) { toast("访客不可修改归一化窗口", "bad"); return; }
  const w = Number($("s-window").value);
  const st = $("s-window-status");
  if (!(w >= 3 && w <= 30)) { st.textContent = "窗口须 3-30"; return; }
  st.textContent = "保存中…";
  try {
    const r = await call("set_sentiment_config", { window: w });
    if (r.applied) {
      st.textContent = `已应用窗口 ${r.window}`;
      $("s-current-window").textContent = `归一窗口：${r.window} 日`;
      $("sentiment-settings").classList.add("hidden");
      runSentiment();
    } else st.textContent = r.error || "保存失败";
  } catch (e) { st.textContent = "保存失败：" + e.message; }
};

async function runSentiment() {
  const refreshBtn = $("s-refresh");
  refreshBtn.disabled = true;
  const date = $("s-date").value.trim().replaceAll("-", "");
  const days = Number($("s-days").value) || 15;
  $("s-result").innerHTML = '<div class="empty">读取中…</div>';
  $("s-trend").innerHTML = '<div class="empty">读取中…</div>';
  $("s-extreme-summary").innerHTML = '<div class="empty">读取中…</div>';
  $("s-extreme-trend").innerHTML = '<div class="empty">读取中…</div>';
  $("s-extreme-fill").style.height = "0%";
  $("s-extreme-value").textContent = "--";
  $("s-extreme-level").textContent = "极端指数";
  // 单日温度 + 指标分解
  try {
    const d = await call("sentiment_temperature", date ? { date } : {});
    if (d.error) throw new Error(d.error);
    setGauge(d.temperature, d.level);
    $("s-current-date").textContent = `日期：${d.date || "最近交易日"}`;
    $("s-current-window").textContent = `归一窗口：${d.window_size || $("s-window").value || "--"} 日`;
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
  // 固定 7 日归一的情绪极端指数（不读取情绪窗口配置）
  try {
    const params = { days };
    if (date) params.date = date;
    const e = await call("sentiment_extreme_index", params);
    if (e.error) throw new Error(e.error);
    const series = (e.recent || []).map((x) => ({ label: fmtDate(x.date), value: x.extreme_index }));
    $("s-extreme-trend").innerHTML = svgLine(series, { min: 0, max: 100, color: "#ef4444" });
    $("s-extreme-meta").textContent = `${series.length} 个交易日 · 固定 ${e.window_size || 7} 日归一`;
    renderExtreme(e);
  } catch (e) {
    $("s-extreme-summary").innerHTML = '<div class="empty">极端指数读取失败：' + e.message + "</div>";
    $("s-extreme-trend").innerHTML = '<div class="empty">极端指数走势读取失败</div>';
  }
  refreshBtn.disabled = false;
};

function renderExtreme(e) {
  const amplitude = e.components?.amplitude || {};
  const volume = e.components?.volume_shrink || {};
  const value = Math.max(0, Math.min(100, Number(e.extreme_index) || 0));
  const color = value >= 80 ? "#ef4444" : (value >= 60 ? "#f97316" : (value >= 40 ? "#f59e0b" : "#3b82f6"));
  $("s-extreme-fill").style.height = `${value}%`;
  $("s-extreme-fill").style.background = color;
  $("s-extreme-bulb").style.background = color;
  $("s-extreme-value").textContent = e.extreme_index ?? "--";
  $("s-extreme-level").textContent = e.level || "极端指数";
  $("s-extreme-summary").innerHTML = `
    <div class="timing-badges">
      <div class="badge"><small>市场振幅</small><b>${amplitude.raw_today ?? "--"}%</b></div>
      <div class="badge"><small>振幅 7 日归一</small><b>${amplitude.normalized_7d ?? "--"}</b></div>
      <div class="badge"><small>缩量 7 日归一</small><b>${volume.normalized_7d ?? "--"}</b></div>
    </div>
    <div class="timing-stance">${e.selection_bias || ""}</div>`;
}

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
    const icon = ["index_body", "index_amp", "avg_price_body", "avg_price_amp"].includes(k)
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
      : '<div class="empty">当日无可回测预判（数据库中无记录或样本尚未成熟）</div>';
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
    if (!isAdmin()) {
      const tip = document.createElement("p");
      tip.className = "hint";
      tip.textContent = "当前为访客，权重仅供查看，不可修改。";
      box.prepend(tip);
    }
    applyRoleUI();
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
  industry_strength: "行业强度（申万一级行业评分分位）",
  // 候选因子（默认权重 0）
  mom_6_1: "6-1 动量（中期趋势）",
  max_lottery: "MAX 彩票效应（反向）",
  downside_vol: "下行波动率（反向）",
  amihud_illiq: "Amihud 非流动性",
  small_size: "规模因子（小市值）",
  value_bm: "账面市值比 B/M（价值）",
  earnings_yield: "盈利收益率 E/P（价值）",
  // 板块
  sec_mom_12_1: "板块 12-1 动量",
  sec_mom_20d: "板块 20 日动量",
  sec_mom_5d: "板块 5 日动量",
  sec_vol_confirm: "板块量能确认",
  sec_low_vol: "板块低波动",
  // 情绪温度
  adv_dec_ratio: "涨跌家数比（上涨占比）",
  limit_up: "涨停家数（越多越热）",
  limit_down: "跌停家数（越多越冷，反向计分）",
  sector_ratio: "板块涨跌比（上涨板块占比）",
  turnover: "大盘成交额（量能）",
  index_mom: "大盘指数动量",
  avg_price_mom: "平均股价指数（全市场平均涨幅）",
  index_body: "大盘指数实体长度（百分点）",
  index_amp: "大盘指数振幅方向（百分点）",
  avg_price_body: "平均股价指数实体长度（百分点）",
  avg_price_amp: "平均股价指数振幅方向（百分点）",
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
  industry_strength: "所属申万一级行业的每日量化评分横截面分位。行业评分由 12-1、20 日、5 日动量、量能确认和低波动综合得到；越接近 1 代表行业趋势排名越靠前，用于让个股筛选顺应行业轮动。",
  mom_6_1: "6-1 中期动量：过去约 126 个交易日、剔除最近 21 日的累计收益（Jegadeesh-Titman 1993）。比 12-1 更贴近中短期趋势延续，与 12-1 互补。默认权重 0，需要时启用。",
  max_lottery: "MAX 彩票效应（Bali, Cakici & Whitelaw 2011）：过去 21 日最大单日涨幅取负。高“博彩性”（近期出现暴涨）的个股因投资者偏好而被高估、未来收益偏低，故取负对齐为越大越好。默认权重 0。",
  downside_vol: "下行波动率（Ang, Chen & Xing 2006）：近 60 日仅负收益部分的标准差，取负。下行风险越低越优（低下行波动溢价）。区别于总波动，只惩罚亏损端波动。默认权重 0。",
  amihud_illiq: "Amihud 非流动性（2002）：近 20 日 mean(|日收益| / 成交额)。衡量单位成交额推动价格的幅度，越大越不流动。学术上存在非流动性溢价（长周期正向）；但本项目偏短线交易、更看重流动性，需谨慎，故默认权重 0。",
  small_size: "规模因子（Fama-French SMB 1993）：−ln(流通市值)。市值越小值越大，捕捉小市值溢价。A 股小市值波动大、需结合流动性与风险控制，默认权重 0。",
  value_bm: "账面市值比 B/M = 1/PB（Fama-French HML）：越高代表越“便宜”的价值股，捕捉价值溢价。默认权重 0。",
  earnings_yield: "盈利收益率 E/P = 1/PE_TTM：估值/质量类价值因子，盈利收益率越高越便宜。本项目 PE 仅作风险背景（权重通常 0），默认权重 0。",
  sec_mom_12_1: "板块指数的 12-1 中期动量。A 股行业层面动量为正（板块轮动有延续性）。正向。",
  sec_mom_20d: "板块指数近 20 个交易日动量，捕捉近端趋势延续。正向。",
  sec_mom_5d: "板块指数近 5 个交易日动量，反映短期情绪热度延续。正向。",
  sec_vol_confirm: "板块量能确认（近 5 日 / 前 20 日均量），放量上行更可信。正向。",
  sec_low_vol: "板块近 60 日波动取负，稳健趋势优于暴涨暴跌。正向。",
  adv_dec_ratio: "全市场上涨家数 /（上涨+下跌家数），衡量赚钱效应广度。越高情绪越热。",
  limit_up: "全市场涨停家数，情绪亢奋度的正向指标。越多越热，正向计分。",
  limit_down: "全市场跌停家数，恐慌度指标。越多越冷，**反向计分**（跌停越多，子分越低，拉低温度）。",
  sector_ratio: "上涨板块数 /（上涨+下跌板块数），衡量热点扩散广度。越高越热。",
  turnover: "全市场成交额（量能）。放量代表资金活跃、情绪升温。",
  index_mom: "大盘指数当日涨跌幅（动量）。正向反映当日强弱。",
  avg_price_mom: "全市场个股平均涨跌幅（以涨幅锚定，非绝对均价）。反映“平均一只票”的当日表现。越高越热。",
  index_body: "大盘（沪深300）当日实体长度，按百分点位口径 (收盘-开盘)/前收×100。阳线为正、阴线为负，绝对值=实体长度。长阳线高分、长阴线低分、短实体贴近中性（分歧小）。低权重情绪因子。",
  index_amp: "大盘（沪深300）当日振幅方向信号，按百分点位口径 (下影线-上影线)/前收×100。振幅越大代表分歧越大；长下影线（抄底/支撑）→高分，长上影线（抛压）→低分，小振幅/短影线→中性。低权重情绪因子。",
  avg_price_body: "平均股价指数当日实体长度（全市场个股 OHLC 等权平均构造“平均一只票”K线），百分点位口径。语义同大盘实体：长阳高分、长阴低分、短实体中性。低权重情绪因子。",
  avg_price_amp: "平均股价指数当日振幅方向信号（全市场平均K线），百分点位口径。语义同大盘振幅：长下影高分、长上影低分、小振幅中性，振幅大表分歧大。低权重情绪因子。",
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
      <span class="factor-link fl-stack" data-fkey="${f}"><span class="fl-cn">${factorLabel(f)} ⓘ</span><span class="fkey">${f}</span></span></label>
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
    if (!isAdmin()) { toast("访客不可修改权重", "bad"); return; }
    const weights = {};
    inputs().forEach((i) => (weights[i.dataset.f] = parseFloat(i.value) || 0));
    try {
      const r = await call("set_factor_weights", { model, weights, actor: "user", reason: "管理员在权重配置页手工调整" });
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

/* ---------- 预计算后台任务与质量看板 ---------- */
let _pcLoaded = false;
let _pcLoading = false;
let _pcPollTimer = null;
const PC_ACTIVE = new Set(["queued", "running"]);
const PC_STATUS = {
  queued: "等待执行", running: "运行中", success: "成功",
  partial: "部分完成", failed: "失败", skipped: "已跳过",
};

function schedulePrecomputePoll(active) {
  if (_pcPollTimer) clearTimeout(_pcPollTimer);
  _pcPollTimer = active && isAdmin()
    ? setTimeout(() => loadPrecompute(true), 1500) : null;
}

function renderPrecomputeTask(task) {
  const box = $("pc-task");
  const btn = $("pc-run");
  if (!task) {
    box.innerHTML = '<div class="empty">尚无任务记录，运行后可在这里持续查看进度</div>';
    btn.disabled = false;
    $("pc-status").textContent = "";
    return false;
  }
  const active = PC_ACTIVE.has(task.status);
  const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
  const label = PC_STATUS[task.status] || task.status || "未知";
  const params = task.params || {};
  const mode = params.full ? "全量补算" : "增量预计算";
  const count = task.total_count ? `${task.completed_count || 0} / ${task.total_count}` : "待确定";
  const error = task.error ? `<div class="pc-message bad">${esc(task.error)}</div>` : "";
  box.innerHTML = `
    <div class="pc-task-head">
      <div><div class="pc-task-title">${esc(mode)}<span class="pc-state ${esc(task.status)}">${esc(label)}</span></div>
        <code class="pc-task-id">任务 ${esc(task.job_id)}</code></div>
      <span class="meta">${esc(task.started_at || "")}</span>
    </div>
    <div class="pc-progress-row"><div class="pc-progress"><i style="width:${progress}%"></i></div><span class="pc-progress-value">${progress}%</span></div>
    <div class="pc-task-grid">
      <div class="pc-task-stat"><small>当前阶段</small><b>${esc(task.stage || "—")}</b></div>
      <div class="pc-task-stat"><small>当前交易日</small><b>${esc(task.current_date || "—")}</b></div>
      <div class="pc-task-stat"><small>日期进度</small><b>${esc(count)}</b></div>
      <div class="pc-task-stat"><small>最近心跳</small><b>${esc(task.heartbeat_at || "—")}</b></div>
    </div>
    <div class="pc-message">${esc(task.message || "等待任务更新")}</div>${error}`;
  btn.disabled = active;
  const status = $("pc-status");
  status.textContent = active ? `${label} · ${progress}%` : `${label}${task.finished_at ? ` · ${task.finished_at}` : ""}`;
  status.className = "status " + (task.status === "success" ? "ok" : task.status === "failed" ? "bad" : "");
  return active;
}

function renderPrecompute(d) {
  const cov = (d.coverage || []).slice().reverse();
  const latest = d.latest_date ? `最新覆盖日 ${d.latest_date}` : "暂无覆盖数据";
  const usable = d.latest_usable_date ? ` · 最新可用 ${d.latest_usable_date}` : "";
  $("pc-meta").textContent = `${latest}${usable} · 因子版 ${d.factor_version || "—"}`;
  const series = cov.map((row) => ({ label: fmtDate(row.trade_date), value: row.count }));
  $("pc-chart").innerHTML = series.length ? svgLine(series, { min: 0, color: "#16a34a" })
    : '<div class="empty">暂无因子数据，请运行预计算</div>';

  const runs = d.runs || [];
  if (!runs.length) {
    $("pc-table").innerHTML = '<div class="empty">暂无计算质量记录</div>';
  } else {
    const rows = runs.map((run) => {
      const label = PC_STATUS[run.status] || run.status || "未知";
      return `<tr><td>${esc(run.trade_date)}</td>
        <td><span class="pc-quality ${esc(run.status)}">${esc(label)}</span></td>
        <td>${((Number(run.coverage_ratio) || 0) * 100).toFixed(1)}%</td>
        <td>${esc(run.computed_count ?? 0)} / ${esc(run.universe_count ?? 0)}</td>
        <td>${renderErrorCell(run)}</td>
        <td>${esc(run.finished_at || "—")}</td></tr>`;
    }).join("");
    $("pc-table").innerHTML = `<table><thead><tr><th>交易日</th><th>质量</th><th>覆盖率</th><th>已计算 / 股票池</th><th>异常</th><th>完成时间</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  return renderPrecomputeTask(d.task);
}

// 异常列只展示摘要（条数 + 截断预览），完整明细点击「查看」时按需拉取
function renderErrorCell(run) {
  const previewArr = Array.isArray(run.errors_preview) ? run.errors_preview
    : (Array.isArray(run.errors) ? run.errors : []);
  const count = Number(
    run.error_count ?? (Array.isArray(run.errors) ? run.errors.length : 0)
  ) || 0;
  if (!count) return "—";
  let preview = previewArr.join("；");
  const truncated = run.errors_truncated || preview.length > 60 || count > previewArr.length;
  if (preview.length > 60) preview = preview.slice(0, 60);
  const text = preview ? esc(preview) + (truncated ? "…" : "") : `${count} 条异常`;
  return `<div class="pc-err-cell"><span class="pc-err-preview" title="点击查看完整异常">${text}</span>`
    + `<button type="button" class="btn-link pc-err-btn" data-date="${esc(run.trade_date)}">查看 ${count} 条</button></div>`;
}

async function openPrecomputeErrors(date) {
  const modal = $("pc-error-modal");
  const body = $("pc-error-body");
  if (!modal || !body) return;
  $("pc-error-date").textContent = date || "";
  body.innerHTML = '<div class="empty">正在加载异常明细…</div>';
  modal.classList.remove("hidden");
  try {
    const d = await call("precompute_run_errors", { trade_date: date });
    const errors = d.errors || [];
    if (!errors.length) {
      body.innerHTML = '<div class="empty">该交易日无异常记录</div>';
      return;
    }
    const list = errors.map((e, i) =>
      `<li><span class="pc-err-idx">${i + 1}</span><span>${esc(e)}</span></li>`).join("");
    body.innerHTML = `<div class="pc-err-meta">状态 ${esc(d.status || "—")} · 共 ${errors.length} 条 · 完成 ${esc(d.finished_at || "—")}</div>`
      + `<ol class="pc-err-list">${list}</ol>`;
  } catch (e) {
    body.innerHTML = `<div class="pc-message bad">加载失败：${esc(e.message)}</div>`;
  }
}

async function loadPrecompute(force = false) {
  if (!isAdmin() || _pcLoading || (_pcLoaded && !force)) return;
  _pcLoading = true;
  if (!_pcLoaded) {
    $("pc-chart").innerHTML = '<div class="empty">加载中…</div>';
    $("pc-table").innerHTML = '<div class="empty">加载中…</div>';
  }
  try {
    const d = await call("precompute_status", { limit: 30 });
    _pcLoaded = true;
    schedulePrecomputePoll(renderPrecompute(d));
  } catch (e) {
    _pcLoaded = false;
    schedulePrecomputePoll(false);
    toast("预计算状态加载失败：" + e.message, "bad");
  } finally {
    _pcLoading = false;
  }
}

$("pc-refresh").onclick = () => loadPrecompute(true);

// 异常列「查看」按钮：事件委托到表格容器，点击时按日拉取完整异常
$("pc-table").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".pc-err-btn");
  if (btn) openPrecomputeErrors(btn.dataset.date);
});
(function bindPrecomputeErrorModal() {
  const modal = $("pc-error-modal");
  if (!modal) return;
  const close = () => modal.classList.add("hidden");
  const closeBtn = $("pc-error-close");
  if (closeBtn) closeBtn.onclick = close;
  modal.addEventListener("click", (ev) => { if (ev.target === modal) close(); });
})();
$("pc-run").onclick = async () => {
  const btn = $("pc-run");
  const status = $("pc-status");
  btn.disabled = true;
  status.textContent = "正在创建后台任务…";
  status.className = "status";
  try {
    const data = await call("precompute_daily_factors", {});
    status.textContent = data.already_running ? "已有任务运行中，已接入其进度" : "后台任务已启动";
    _pcLoaded = false;
    await loadPrecompute(true);
  } catch (e) {
    status.textContent = "启动失败：" + e.message;
    status.className = "status bad";
    btn.disabled = false;
  }
};

/* 首屏：没有本地 Key 时必须先登录；已有 Key 仍需重新向服务端验证 */
applyRoleUI();
if (!cfg.key) {
  openLogin("请输入服务 Key 后继续");
} else {
  refreshRole();
}
