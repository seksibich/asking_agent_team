/* 盯盘量化面板 —— 与数据服务同源部署，调用 POST /call */
const LS = { base: "sa_base" };
const SS = { key: "sa_session_key" };
const cfg = {
  base: localStorage.getItem(LS.base) || window.location.origin,
  // 访问凭据只保留当前标签页会话，关闭后自动清除。
  key: sessionStorage.getItem(SS.key) || "",
};
let _marketHealth = {};

function rememberConnection() {
  localStorage.setItem(LS.base, cfg.base);
  sessionStorage.setItem(SS.key, cfg.key);
}

function acceptHealth(value) {
  if (value && typeof value === "object") _marketHealth = value;
  return value;
}

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[ch]));

// 统一拒绝空值、布尔值及非有限数，避免 Number(null) 被误显示为 0。
const finiteNumber = (value) => {
  if (value == null || value === "" || typeof value === "boolean") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
};
const finiteNumberText = (value, { digits = 2, trim = false, sign = false, suffix = "", fallback = "—" } = {}) => {
  const number = finiteNumber(value);
  if (number == null) return fallback;
  let text = number.toFixed(digits);
  if (trim) text = text.replace(/\.?0+$/, "");
  return `${sign && number >= 0 ? "+" : ""}${text}${suffix}`;
};
const finiteNumberClass = (value) => {
  const number = finiteNumber(value);
  return number == null ? "" : (number >= 0 ? "pos" : "neg");
};
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
  acceptHealth(body.health);
  if (!res.ok || body.ok === false) {
    throw new Error(body.error || `请求失败（${res.status}）`);
  }
  return body.data;
}

async function health() {
  const res = await fetch(cfg.base.replace(/\/$/, "") + "/health", {
    headers: { "X-API-Key": cfg.key },
  });
  const body = await res.json();
  return acceptHealth(body);
}

/* ---------- 角色 / 权限（管理员 vs 用户） ---------- */
// 未通过 Key 验证前禁止调用功能，服务端鉴权作为最终安全边界
const ADMIN_ONLY_TABS = new Set(["portfolio", "precompute", "quant-watch"]);
let _role = "user";
let _authReady = false;
let _initialTabSelected = false;
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
    acceptHealth(body.health);
    if (!res.ok || !["admin", "user"].includes(body.role)) {
      throw new Error(body.error || "访问凭据无效或无权访问");
    }
    cfg.base = base;
    cfg.key = key;
    rememberConnection();
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
    acceptHealth(body.health);
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
  if (_authReady && !_initialTabSelected) {
    activateTab(admin ? "quant-watch" : "sentiment");
    _initialTabSelected = true;
  }
  // 角色切换时若当前正停留在管理员页，回到首个普通业务页。
  const activeTab = document.querySelector(".tab.active");
  if (_authReady && !admin && activeTab && ADMIN_ONLY_TABS.has(activeTab.dataset.tab)) {
    activateTab("sentiment");
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
    stopQuantWatchSocket();
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
function activateTab(tabName) {
  if (!_authReady) return;
  const button = document.querySelector(`.tab[data-tab="${tabName}"]`);
  const panel = $("tab-" + tabName);
  if (!button || !panel || button.classList.contains("hidden")) return;
  if (tabName !== "quant-watch") stopQuantWatchSocket();
  document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  panel.classList.add("active");
  if (tabName === "weights") loadWeights();
  if (tabName === "backtest") loadBacktest();
  if (tabName === "precompute") loadPrecompute();
  if (tabName === "quant-watch") loadQuantWatch(true);
  // 情绪与行业均为时效敏感页面：每次进入都重新请求；情绪连续交易时段额外强刷盘中快照。
  if (tabName === "sentiment") loadSentiment(true, true);
  if (tabName === "industry") loadIndustry(true);
  if (tabName === "selections") loadSelections(true);
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (!_authReady) {
      openLogin("请先输入有效的服务 Key");
      return;
    }
    activateTab(btn.dataset.tab);
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
  if ($("uk-created-key")) $("uk-created-key").textContent = "";
  $("uk-created")?.classList.add("hidden");
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
          <code class="uk-key">${esc(k.masked_key || "已隐藏")}</code>
          <small>${esc(k.created_at || "")}</small>
        </div>
        <div class="uk-ops">
          <button class="btn-ghost" data-uk-toggle="${esc(k.id || "")}">${k.disabled ? "启用" : "停用"}</button>
          <button class="btn-ghost uk-del" data-uk-del="${esc(k.id || "")}">删除</button>
        </div>
      </div>`).join("");
  } catch (e) { list.innerHTML = '<div class="empty">加载失败：' + esc(e.message) + "</div>"; }
}

$("uk-create").onclick = async () => {
  const label = $("uk-label").value.trim();
  try {
    const data = await adminApi("/admin/user-keys", "POST", { label });
    const rawKey = String(data.item?.key || "");
    $("uk-label").value = "";
    $("uk-created-key").textContent = rawKey;
    $("uk-created").classList.toggle("hidden", !rawKey);
    toast("访客 Key 已生成，请立即保存", "ok");
    loadUserKeys();
  } catch (e) { toast("生成失败：" + e.message, "bad"); }
};

$("uk-created-copy").onclick = async () => {
  const rawKey = $("uk-created-key").textContent;
  if (!rawKey) return;
  try { await navigator.clipboard.writeText(rawKey); toast("完整 Key 已复制", "ok"); }
  catch (error) { toast("复制失败，请手动选择", "bad"); }
};

$("uk-list").addEventListener("click", async (e) => {
  const tog = e.target.closest("[data-uk-toggle]");
  const del = e.target.closest("[data-uk-del]");
  if (tog) {
    try { await adminApi("/admin/user-keys/toggle", "POST", { id: tog.getAttribute("data-uk-toggle") }); loadUserKeys(); }
    catch (err) { toast("操作失败：" + err.message, "bad"); }
  } else if (del) {
    if (!confirm("删除后该访客 Key 立即失效，确认删除？")) return;
    try { await adminApi("/admin/user-keys/delete", "POST", { id: del.getAttribute("data-uk-del") }); loadUserKeys(); }
    catch (err) { toast("删除失败：" + err.message, "bad"); }
  }
});
$("settings").addEventListener("click", (e) => { if (e.target.id === "settings") $("settings").classList.add("hidden"); });
function showConfigSection(section) {
  if (section === "portfolio" && !isAdmin()) return;
  $("tab-weights")?.classList.toggle("active", section === "weights");
  $("tab-portfolio")?.classList.toggle("active", section === "portfolio");
  if (section === "weights") loadWeights();
  if (section === "portfolio") loadPortfolio(true);
}
$("cfg-weights-portfolio").onclick = () => showConfigSection("portfolio");
$("cfg-portfolio-weights").onclick = () => showConfigSection("weights");

function reloadActiveTab() {
  const active = document.querySelector(".tab.active");
  const t = active && active.dataset.tab;
  if (t === "weights" && $("tab-portfolio")?.classList.contains("active")) loadPortfolio(true);
  else if (t === "weights") loadWeights();
  else if (t === "backtest") loadBacktest(true);
  else if (t === "precompute") loadPrecompute(true);
  else if (t === "quant-watch") loadQuantWatch(true);
  else if (t === "sentiment") loadSentiment(true);
  else if (t === "industry") loadIndustry(true);
  else if (t === "selections") loadSelections(true);
  else if (t === "portfolio") loadPortfolio(true);
}

async function resetActiveTab() {
  const tab = document.querySelector(".tab.active")?.dataset.tab;
  if (!tab) return;
  if (tab === "quant") {
    $("q-stock-names").value = "";
    $("q-industries").value = "";
    $("q-topn").value = "30";
    document.querySelectorAll('#q-boards input[type="checkbox"]').forEach((input) => { input.checked = true; });
    _quantRows = [];
    _quantSort = { key: "score", dir: "desc" };
    $("q-meta").textContent = "";
    $("q-result").innerHTML = '<div class="empty">点击「运行量化选股」查看结果</div>';
    quantHelpModal.classList.add("hidden");
  } else if (tab === "quant-watch") {
    $("qw-mode").value = "market";
    $("qw-interval").value = "60";
    $("qw-window").value = "5";
    $("qw-threshold").value = "72";
    $("qw-industries").value = "";
    $("qw-themes").value = "";
    $("qw-enabled").checked = true;
    $("qw-notify").checked = false;
    $("qw-feishu").checked = false;
    $("qw-wecom").checked = false;
    document.querySelectorAll('#qw-boards input[type="checkbox"]').forEach((input) => {
      input.checked = ["main", "gem"].includes(input.value);
    });
    await saveQuantWatchConfig();
  } else if (tab === "industry") {
    $("i-mode").value = "latest_complete";
    $("i-date").value = "";
    $("i-history-days").value = "20";
    $("i-topn").value = "31";
    $("i-search").value = "";
    $("i-sort").value = "percentile-desc";
    $("i-meta").textContent = "";
    _industryLoaded = false;
    _industryRows = [];
    _industryData = {};
    _industrySelectedCode = "";
    _industryAvailableDates = [];
    _industrySort = { key: "percentile", dir: "desc" };
    await loadIndustry(true);
  } else if (tab === "selections") {
    $("sl-from").value = "";
    $("sl-to").value = "";
    $("sl-hotspot").value = "";
    $("sl-category").value = "";
    $("sl-sort").value = "grouped";
    _selectionSort = "grouped";
    _selectionTag = "";
    _selectionsLoaded = false;
    await loadSelections(true);
  } else if (tab === "portfolio") {
    resetPortfolioForm();
    $("pf-search").value = "";
    $("pf-search-results").classList.add("hidden");
    _portfolioLoaded = false;
    await loadPortfolio(true);
  } else if (tab === "sentiment") {
    $("s-date").value = "";
    $("s-days").value = "15";
    $("sentiment-settings").classList.add("hidden");
    _sentimentTrendEnd = "";
    _sentimentMaxDate = "";
    _sentimentKnownDates = [];
    _sentLoaded = false;
    await loadSentiment(true);
  } else if (tab === "backtest") {
    $("b-category").value = "";
    $("log-scene").value = "api";
    $("log-scope").value = "date";
    $("log-date").value = todayText;
    $("log-from").value = todayText;
    $("log-to").value = todayText;
    updateLogScopeUI();
    _btLoaded = false;
    await loadBacktest(true);
  } else if (tab === "precompute") {
    if (_pcPollTimer) clearTimeout(_pcPollTimer);
    _pcPollTimer = null;
    _pcLoaded = false;
    await loadPrecompute(true);
  } else if (tab === "weights") {
    await loadWeights();
  }
}

$("tab-reset").onclick = async () => {
  if (!_authReady) { openLogin("请先输入有效的服务 Key"); return; }
  const button = $("tab-reset");
  button.disabled = true;
  button.classList.add("loading");
  try {
    await resetActiveTab();
    toast("当前页已恢复初始化状态", "ok");
  } catch (error) {
    toast("页面重置失败：" + error.message, "bad");
  } finally {
    button.disabled = false;
    button.classList.remove("loading");
  }
}

$("cfg-save").onclick = async () => {
  cfg.base = $("cfg-base").value.trim() || window.location.origin;
  cfg.key = $("cfg-key").value.trim();
  rememberConnection();
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
    const market = MarketState.context(h);
    s.textContent = `连接正常 · ${isAdmin() ? "管理员" : "访客"} · ${market.phaseLabel}`;
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

async function runQuantScreen() {
  const btn = $("q-run");
  if (btn.disabled) return;
  btn.disabled = true;
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
    $("q-meta").textContent = `${d.trade_date || ""} · 最近完整日收盘行情 · ${scope}${boardText} · ${rows.length} 只`;
    renderCandidates(rows);
    if (!rows.length && d.note) toast(d.note, "bad");
  } catch (e) { $("q-result").innerHTML = ""; toast("选股失败：" + e.message, "bad"); }
  finally { btn.disabled = false; }
}

$("q-run").onclick = runQuantScreen;
["q-stock-names", "q-industries"].forEach((id) => {
  $(id).addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    runQuantScreen();
  });
});

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
  { key: "score", label: "综合分" },
  { key: "__ticker", label: "标的", sort: "code" },
  { key: "last", label: "最近完整日收盘价" },
  { key: "chg", label: "最近完整日涨幅", pct: true },
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
    if (c.key === "__ticker") {
      return `<th class="quant-ticker-col" title="标的列固定，不支持排序">${c.label}</th>`;
    }
    const sk = c.sort || c.key;
    const arrow = sk === key ? (dir === "asc" ? " ▲" : " ▼") : "";
    const fixedClass = c.key === "score" ? " quant-score-col" : "";
    return `<th class="sortable${fixedClass}" data-sort="${sk}" title="点击排序">${c.label}${arrow}</th>`;
  }).join("");
  const body = sorted.map((r) => "<tr>" + cols.map((c) => {
    if (c.key === "__ticker") return `<td class="cell-ticker quant-ticker-col"><b>${r.name || "-"}</b><span>${r.code || ""}</span></td>`;
    let v = r[c.key], cls = c.key === "score" ? "quant-score-col" : "";
    if (typeof v === "number") {
      if (c.pct || c.key === "score") cls += ` ${v >= 0 ? "pos" : "neg"}`;
      if (c.key === "last") v = v.toFixed(2);
      else v = c.pct ? (v >= 0 ? "+" : "") + v.toFixed(2) + "%" : (Number.isInteger(v) ? v : v.toFixed(3));
    }
    return `<td class="${cls.trim()}">${v == null ? "-" : v}</td>`;
  }).join("") + "</tr>").join("");
  box.innerHTML = `<table class="sortable-table quant-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
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
const INDUSTRY_FACTORS = [
  { key: "sec_mom_12_1", label: "12-1动量" },
  { key: "sec_mom_20d", label: "20日动量" },
  { key: "sec_mom_5d", label: "5日动量" },
  { key: "sec_vol_confirm", label: "量能确认" },
  { key: "sec_low_vol", label: "低波动" },
];
const INDUSTRY_OVERLAY_LABELS = {
  change_pct: "盘中平均涨跌", chg_pct: "盘中涨跌", pct_chg: "盘中涨跌",
  breadth: "成分宽度", adv_ratio: "上涨占比", up_ratio: "上涨占比",
  decline_ratio: "下跌占比", flat_ratio: "平盘占比",
  sample_count: "有效样本", member_count: "行业成分", coverage_ratio: "成分覆盖率",
  as_of: "快照时间", fetched_at: "快照时间",
};
const INDUSTRY_OVERLAY_PERCENT_KEYS = new Set([
  "change_pct", "chg_pct", "pct_chg", "adv_ratio", "up_ratio", "decline_ratio", "flat_ratio",
]);
let _industryLoaded = false;
let _industryRequestSeq = 0;
let _industryRows = [];
let _industryData = {};
let _industrySelectedCode = "";
let _industryAvailableDates = [];
let _industrySort = { key: "percentile", dir: "desc" };

function compactDate(value) {
  const text = String(value || "").replaceAll("-", "");
  return /^\d{8}$/.test(text) ? text : "";
}

function localTodayCompact() {
  return MarketState.context(_marketHealth).serverDate;
}

function industryNumber(row, key) {
  return finiteNumber(row?.[key]);
}

function industryBaseRow(row) {
  const baseline = row?.baseline || row?.complete_day || row?.complete || {};
  return { ...(row || {}), ...(baseline || {}), intraday_overlay: row?.intraday_overlay ?? row?.overlay ?? row?.intraday };
}

function industryStrength(row) {
  const value = industryNumber(row, "percentile");
  return value == null ? null : (value <= 1 ? value : value / 100);
}

function industryValueText(value, digits = 4) {
  return finiteNumberText(value, { digits, trim: true });
}

function industryPercentText(value, digits = 1) {
  return finiteNumberText(value, { digits, sign: true, suffix: "%" });
}

function industrySortRows(rows) {
  const { key, dir } = _industrySort;
  return [...rows].sort((left, right) => {
    if (key === "name") {
      const order = String(left.name || left.code || "").localeCompare(String(right.name || right.code || ""), "zh-CN");
      return dir === "asc" ? order : -order;
    }
    const a = industryNumber(left, key), b = industryNumber(right, key);
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return dir === "asc" ? a - b : b - a;
  });
}

function updateIndustryDateButtons() {
  const current = compactDate($("i-date").value);
  const today = localTodayCompact();
  const available = _industryAvailableDates.filter((date) => date <= today);
  $("i-date-prev").disabled = !current || !available.some((date) => date < current);
  $("i-date-next").disabled = !current || !available.some((date) => date > current);
}

function renderIndustryStatus(data) {
  const requested = compactDate(data.requested_trade_date) || compactDate($("i-date").value);
  const effective = compactDate(data.effective_date || data.trade_date);
  const baseline = compactDate(data.baseline_trade_date);
  const intradayView = $("i-mode").value === "intraday";
  const isComplete = data.is_complete !== false;
  const mode = intradayView ? "盘中视图 · 最终完整基线" : (isComplete ? "完整收盘" : "非完整数据");
  const staleDays = finiteNumber(data.stale_trade_days);
  const stale = data.is_stale
    ? `数据陈旧 ${finiteNumberText(staleDays, { digits: 0, fallback: "未知" })} 个交易日`
    : "数据时效正常";
  const phaseLabels = {
    preopen: "盘前", call_auction: "集合竞价", morning: "上午交易", afternoon: "下午交易",
    lunch: "午间休市", closed_pending: "收盘待确认", final: "日终数据就绪", non_trading_day: "非交易日",
    pre_open: "盘前", trading: "交易中", lunch_break: "午间休市", closed: "已收盘", post_close: "盘后",
  };
  const phase = phaseLabels[data.market_session] || "阶段未知";
  const fallback = data.fallback_reason ? `<span class="industry-status-reason">回退原因：${esc(data.fallback_reason)}</span>` : "";
  $("i-status").innerHTML = `
    <span><b>请求日</b>${esc(formatFullDate(requested) || "最近交易日")}</span>
    <span><b>生效日</b>${esc(formatFullDate(effective) || "—")}</span>
    ${baseline ? `<span><b>基准日</b>${esc(formatFullDate(baseline))}</span>` : ""}
    <span class="${intradayView || !isComplete ? "intraday" : "complete"}">${mode} · ${esc(phase)}</span>
    <span class="${data.is_stale ? "stale" : "fresh"}">${stale}</span>${fallback}`;
}

function renderIndustryKpis() {
  const strengths = _industryRows.map(industryStrength).filter((value) => value != null);
  const momentums = _industryRows.map((row) => industryNumber(row, "sec_mom_5d")).filter((value) => value != null).sort((a, b) => a - b);
  const middle = Math.floor(momentums.length / 2);
  const median = !momentums.length ? null : (momentums.length % 2 ? momentums[middle] : (momentums[middle - 1] + momentums[middle]) / 2);
  const top = [..._industryRows].filter((row) => industryStrength(row) != null)
    .sort((a, b) => industryStrength(b) - industryStrength(a))[0];
  const medianDisplay = median == null ? null : (Math.abs(median) <= 1 ? median * 100 : median);
  $("i-kpi-strong").textContent = finiteNumberText(strengths.filter((value) => value >= 0.7).length, { digits: 0 });
  $("i-kpi-weak").textContent = finiteNumberText(strengths.filter((value) => value <= 0.3).length, { digits: 0 });
  $("i-kpi-median").textContent = industryPercentText(medianDisplay, 2);
  $("i-kpi-median").className = finiteNumberClass(medianDisplay);
  $("i-kpi-top").textContent = top?.name || top?.code || "—";
  const topStrength = top ? industryStrength(top) : null;
  $("i-kpi-top-sub").textContent = topStrength == null ? "强度数据缺失" : `强度 ${finiteNumberText(topStrength * 100, { digits: 1 })}`;
}

function renderIndustryBars() {
  const sorted = _industryRows.filter((row) => industryStrength(row) != null)
    .sort((a, b) => industryStrength(b) - industryStrength(a));
  if (!sorted.length) { $("i-bars").innerHTML = '<div class="empty">暂无可靠行业评分</div>'; return; }
  const top = sorted.slice(0, 5).map((row) => ({ row, side: "top" }));
  const used = new Set(top.map((item) => item.row.code));
  const bottom = sorted.slice(-5).reverse().filter((row) => !used.has(row.code)).map((row) => ({ row, side: "bottom" }));
  const group = (title, items) => `<div class="industry-bar-group"><b>${title}</b>${items.map(({ row, side }) => {
    const strength = Math.max(0, Math.min(1, industryStrength(row)));
    const strengthText = finiteNumberText(strength * 100, { digits: 1 });
    const selected = row.code === _industrySelectedCode;
    return `<button type="button" class="industry-bar ${side} ${selected ? "selected" : ""}" data-industry-code="${esc(row.code)}" title="点击查看 ${esc(row.name || row.code)} 历史">
      <span class="industry-bar-name">${esc(row.name || row.code)}</span><span class="industry-bar-track"><i style="width:${strengthText}%"></i></span><strong>${strengthText}</strong>
    </button>`;
  }).join("")}</div>`;
  $("i-bars").innerHTML = group("强势前 5", top) + group("弱势后 5", bottom);
}

function renderIndustryHeat() {
  if (!_industryRows.length) { $("i-heat").innerHTML = '<div class="empty">暂无可用因子</div>'; return; }
  const rows = [..._industryRows].sort((a, b) => (industryStrength(b) ?? -Infinity) - (industryStrength(a) ?? -Infinity));
  const ranges = Object.fromEntries(INDUSTRY_FACTORS.map(({ key }) => {
    const values = rows.map((row) => industryNumber(row, key)).filter((value) => value != null);
    return [key, { min: Math.min(...values), max: Math.max(...values), has: values.length > 0 }];
  }));
  const head = INDUSTRY_FACTORS.map((factor) => `<th>${factor.label}</th>`).join("");
  const body = rows.map((row) => `<tr class="${row.code === _industrySelectedCode ? "selected" : ""}" data-industry-code="${esc(row.code)}"><th>${esc(row.name || row.code)}</th>${INDUSTRY_FACTORS.map(({ key }) => {
    const value = industryNumber(row, key), range = ranges[key];
    const ratio = value == null || !range.has ? null : (range.max === range.min ? 0.5 : (value - range.min) / (range.max - range.min));
    const style = ratio == null ? "" : ` style="--heat:${ratio.toFixed(3)}"`;
    return `<td class="industry-heat-cell ${ratio == null ? "missing" : ""}"${style}>${industryValueText(value)}</td>`;
  }).join("")}</tr>`).join("");
  $("i-heat").innerHTML = `<table class="industry-heat-table"><thead><tr><th>行业</th>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function industryHistorySeries(code) {
  const name = _industryRows.find((row) => row.code === code)?.name;
  const history = _industryData.history;
  let points = [];
  if (Array.isArray(history)) {
    const direct = history.find((item) => (item.code === code || item.name === name) && Array.isArray(item.points || item.history || item.rows));
    if (direct) points = direct.points || direct.history || direct.rows;
    else history.forEach((snapshot) => {
      const sectors = snapshot.sectors || snapshot.rows;
      const row = Array.isArray(sectors) ? sectors.find((item) => item.code === code || item.name === name) : null;
      if (row) points.push({ ...row, date: snapshot.date || snapshot.trade_date || snapshot.effective_date });
      else if (snapshot.code === code || snapshot.name === name) points.push(snapshot);
    });
  } else if (history && typeof history === "object") {
    points = history[code] || history[name] || history.sectors?.[code] || [];
  }
  const current = _industryRows.find((row) => row.code === code);
  if (!points.length && Array.isArray(current?.history)) points = current.history;
  return (Array.isArray(points) ? points : []).map((point) => {
    const percentile = industryNumber(point, "percentile");
    return { label: formatFullDate(point.date || point.trade_date || point.effective_date), value: percentile == null ? null : (percentile <= 1 ? percentile * 100 : percentile) };
  }).filter((point) => point.label && point.value != null).sort((a, b) => a.label.localeCompare(b.label));
}

function industryTrendLine(points) {
  const host = $("i-trend");
  const H = 220, axisWidth = 48, padX = 22, padT = 16, padB = 38, pointGap = 72;
  const viewportWidth = Math.max(280, (host?.clientWidth || 640) - axisWidth);
  const plotWidth = Math.max(viewportWidth, padX * 2 + Math.max(0, points.length - 1) * pointGap);
  const xAt = (index) => points.length === 1 ? plotWidth / 2 : padX + index * pointGap;
  const yAt = (value) => padT + (H - padT - padB) * (1 - Number(value) / 100);
  let grid = "", axis = "";
  for (let value = 0; value <= 100; value += 20) {
    const y = yAt(value);
    grid += `<line x1="0" y1="${y.toFixed(1)}" x2="${plotWidth}" y2="${y.toFixed(1)}" stroke="#eef1f7"/>`;
    axis += `<span style="top:${y.toFixed(1)}px">${value}</span>`;
  }
  const path = points.map((point, index) => `${index ? "L" : "M"}${xAt(index).toFixed(1)} ${yAt(point.value).toFixed(1)}`).join(" ");
  const area = `${path} L${xAt(points.length - 1).toFixed(1)} ${yAt(0).toFixed(1)} L${xAt(0).toFixed(1)} ${yAt(0).toFixed(1)} Z`;
  const dots = points.map((point, index) => `<g><circle cx="${xAt(index).toFixed(1)}" cy="${yAt(point.value).toFixed(1)}" r="3.5" fill="#3b6cf6"><title>${esc(point.label)}：${Number(point.value).toFixed(1)}</title></circle><text x="${xAt(index).toFixed(1)}" y="${H - 13}" text-anchor="middle" class="ax">${esc(point.label.slice(5))}</text></g>`).join("");
  return `<div class="industry-trend-layout"><div class="industry-trend-axis">${axis}</div><div class="industry-trend-scroll"><svg width="${plotWidth}" height="${H}" viewBox="0 0 ${plotWidth} ${H}" class="industry-trend-svg">${grid}<path d="${area}" fill="#3b6cf6" fill-opacity="0.08"/><path d="${path}" fill="none" stroke="#3b6cf6" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>${dots}</svg></div></div>`;
}

function renderIndustryTrend() {
  const row = _industryRows.find((item) => item.code === _industrySelectedCode);
  if (!row) {
    $("i-trend-meta").textContent = "";
    $("i-trend").innerHTML = '<div class="empty">暂无可选行业</div>';
    return;
  }
  const points = industryHistorySeries(row.code);
  $("i-trend-meta").textContent = `${row.name || row.code} · ${points.length} 个交易日`;
  $("i-trend").innerHTML = points.length
    ? industryTrendLine(points)
    : '<div class="empty">当前响应没有该行业的历史序列</div>';
}

function isReliableIndustryOverlay(overlay) {
  if (!overlay || typeof overlay !== "object" || Array.isArray(overlay)) return false;
  if (overlay.available === false || overlay.is_reliable === false || overlay.reliable === false) return false;
  if (["unavailable", "missing", "unreliable"].includes(String(overlay.status || "").toLowerCase())) return false;
  return Object.entries(overlay).some(([key, value]) => value != null
    && !["available", "is_reliable", "reliable", "status", "reason", "note"].includes(key));
}

function renderIndustryIntraday() {
  const box = $("i-intraday");
  if ($("i-mode").value !== "intraday") { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  const baseline = compactDate(_industryData.baseline_trade_date || _industryData.effective_date);
  const topOverlay = _industryData.intraday_overlay;
  const overlays = _industryRows.map((row) => ({ row, overlay: row.intraday_overlay }))
    .filter(({ overlay }) => isReliableIndustryOverlay(overlay));
  const selected = overlays.find(({ row }) => row.code === _industrySelectedCode) || overlays[0];
  const unavailableReason = topOverlay && typeof topOverlay === "object" && !Array.isArray(topOverlay)
    ? topOverlay.reason || topOverlay.note : "";
  let overlayHtml = `<div class="industry-overlay-empty">盘中覆盖不可用：${esc(unavailableReason || "当前没有后端确认可靠的行业盘中覆盖数据")}。页面仅展示最终完整基线，绝不冒充盘中评分。</div>`;
  if (selected) {
    const entries = Object.entries(selected.overlay).filter(([key, value]) => value != null && ![
      "code", "name", "date", "trade_date", "quote_date", "available", "is_reliable", "reliable",
      "status", "reason", "note", "data_mode", "is_final", "source",
    ].includes(key));
    overlayHtml = `<div class="industry-overlay-title">${esc(selected.row.name || selected.row.code)} · 盘中叠加</div><div class="industry-overlay-grid">${entries.map(([key, value]) => {
      const label = INDUSTRY_OVERLAY_LABELS[key] || "盘中指标";
      let text;
      if (INDUSTRY_OVERLAY_PERCENT_KEYS.has(key) && typeof value === "number") text = industryPercentText(value, 2);
      else if (key === "coverage_ratio" && typeof value === "number") text = `${(value * 100).toFixed(1)}%`;
      else text = typeof value === "number" ? industryValueText(value, 3) : esc(value);
      return `<div><small>${esc(label)}</small><b>${text}</b></div>`;
    }).join("")}</div>`;
  }
  box.innerHTML = `<div class="industry-intraday-head"><div><span class="sentiment-eyebrow">最终完整基线</span><b>最终完整基线：${esc(formatFullDate(baseline) || "不可用")}</b><p>行业强度、综合分和因子矩阵只来自完整交易日。</p></div><span>＋</span><div><span class="sentiment-eyebrow intraday">盘中叠加</span><b>盘中叠加独立展示</b><p>仅展示后端确认可靠的临时变化，不改写最终完整评分。</p></div></div>${overlayHtml}`;
}

function renderIndustryTable() {
  const query = $("i-search").value.trim().toLowerCase();
  const filtered = _industryRows.filter((row) => !query || `${row.name || ""} ${row.code || ""}`.toLowerCase().includes(query));
  const rows = industrySortRows(filtered);
  $("i-detail-meta").textContent = `显示 ${rows.length} / 共 ${_industryRows.length} 个行业`;
  if (!rows.length) { $("i-result").innerHTML = '<div class="empty">没有匹配行业，搜索不会触发新请求</div>'; return; }
  const columns = [
    { key: "name", label: "行业" }, { key: "percentile", label: "行业强度" }, { key: "score", label: "综合分" },
    ...INDUSTRY_FACTORS,
  ];
  const head = columns.map((column) => {
    const active = _industrySort.key === column.key;
    return `<th class="sortable" data-industry-sort="${column.key}">${column.label}${active ? (_industrySort.dir === "asc" ? " ▲" : " ▼") : ""}</th>`;
  }).join("");
  const body = rows.map((row) => `<tr class="industry-detail-row ${row.code === _industrySelectedCode ? "selected" : ""}" data-industry-code="${esc(row.code)}">
    <td class="cell-ticker"><b>${esc(row.name || "—")}</b><span>${esc(row.code || "")}</span></td>
    <td>${finiteNumberText(industryStrength(row) == null ? null : industryStrength(row) * 100, { digits: 1 })}</td>
    <td>${industryValueText(row.score)}</td>${INDUSTRY_FACTORS.map(({ key }) => `<td>${industryValueText(row[key])}</td>`).join("")}
  </tr>`).join("");
  $("i-result").innerHTML = `<table class="sortable-table industry-detail-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function selectIndustry(code) {
  if (!_industryRows.some((row) => row.code === code)) return;
  _industrySelectedCode = code;
  renderIndustryBars();
  renderIndustryHeat();
  renderIndustryTrend();
  renderIndustryIntraday();
  renderIndustryTable();
}

function renderIndustry(data) {
  _industryData = data || {};
  const topOverlay = data.intraday_overlay;
  const topOverlayRows = Array.isArray(topOverlay?.rows)
    ? topOverlay.rows
    : (Array.isArray(topOverlay) ? topOverlay : []);
  _industryRows = (data.sectors || []).map((raw) => {
    const row = industryBaseRow(raw);
    if (!row.intraday_overlay && topOverlay && typeof topOverlay === "object") {
      row.intraday_overlay = topOverlayRows.find((item) => item.code === row.code || item.name === row.name)
        || topOverlay[row.code] || topOverlay[row.name];
    }
    return row;
  });
  const today = localTodayCompact();
  const responseDates = (data.available_dates || []).map(compactDate).filter((date) => date && date <= today);
  // 历史响应只包含截至生效日的快照；保留本会话已确认日期，确保后退后仍可向前返回。
  _industryAvailableDates = [...new Set([..._industryAvailableDates, ...responseDates])].sort();
  const effective = compactDate(data.effective_date || data.trade_date);
  const requested = compactDate(data.requested_trade_date);
  if (!$("i-date").value && (requested || effective)) $("i-date").value = formatFullDate(requested || effective);
  $("i-date").max = formatFullDate(today);
  if (!_industryRows.some((row) => row.code === _industrySelectedCode)) _industrySelectedCode = _industryRows[0]?.code || "";
  const intradayView = $("i-mode").value === "intraday";
  $("i-meta").textContent = `${_industryRows.length} 个行业 · ${intradayView ? "最终完整基线" : (data.is_complete === false ? "非完整数据" : "完整日")}`;
  renderIndustryStatus(data);
  renderIndustryKpis();
  renderIndustryBars();
  renderIndustryHeat();
  renderIndustryTrend();
  renderIndustryIntraday();
  renderIndustryTable();
  updateIndustryDateButtons();
}

async function loadIndustry(force = false) {
  if ((_industryLoaded && !force) || !cfg.key) return;
  if (!_marketHealth.date) {
    try { await health(); } catch (error) { /* 业务请求仍由服务端给出明确错误 */ }
  }
  const requestSeq = ++_industryRequestSeq;
  const button = $("i-run");
  const date = compactDate($("i-date").value);
  const policy = MarketState.industryRequest($("i-mode").value, date, _marketHealth);
  const today = policy.state.maxSelectableDate || policy.state.serverDate;
  if (policy.notice) toast(policy.notice, "ok");
  if ($("i-mode").value !== policy.mode) $("i-mode").value = policy.mode;
  if (policy.date && policy.date !== date) $("i-date").value = formatFullDate(policy.date);
  _industryLoaded = true;
  button.disabled = true;
  button.textContent = "刷新中…";
  $("i-result").innerHTML = '<div class="empty">加载行业评分中…</div>';
  $("i-bars").innerHTML = '<div class="empty">加载强弱行业中…</div>';
  $("i-heat").innerHTML = '<div class="empty">加载指标矩阵中…</div>';
  $("i-trend").innerHTML = '<div class="empty">加载历史趋势中…</div>';
  const params = {
    mode: policy.mode,
    history_days: Math.max(3, Math.min(120, Number($("i-history-days").value) || 20)),
    top_n: Math.max(5, Math.min(100, Number($("i-topn").value) || 31)),
  };
  if (policy.date && policy.mode !== "latest_complete") params.date = policy.date;
  try {
    const data = await call("screen_sector", params);
    if (requestSeq !== _industryRequestSeq) return;
    renderIndustry(data);
  } catch (error) {
    if (requestSeq !== _industryRequestSeq) return;
    _industryLoaded = false;
    $("i-status").innerHTML = `<span class="industry-status-reason">行业数据加载失败：${esc(error.message)}</span>`;
    $("i-result").innerHTML = `<div class="empty">行业评分加载失败：${esc(error.message)}</div>`;
    $("i-bars").innerHTML = '<div class="empty">暂无强弱行业数据</div>';
    $("i-heat").innerHTML = '<div class="empty">暂无指标矩阵</div>';
    $("i-trend").innerHTML = '<div class="empty">暂无历史趋势</div>';
  } finally {
    if (requestSeq === _industryRequestSeq) {
      button.disabled = false;
      button.textContent = "刷新行业数据";
      $("i-date").max = formatFullDate(today);
    }
  }
}

async function resolveIndustryTradeDate(current, direction) {
  const today = localTodayCompact();
  const available = _industryAvailableDates.filter((date) => date <= today);
  return direction < 0
    ? [...available].reverse().find((date) => date < current) || ""
    : available.find((date) => date > current) || "";
}

async function shiftIndustryDate(direction) {
  const current = compactDate($("i-date").value);
  if (!current) return;
  $("i-date-prev").disabled = true;
  $("i-date-next").disabled = true;
  try {
    const target = await resolveIndustryTradeDate(current, direction);
    if (!target) { toast(direction < 0 ? "没有更早的可用行业快照" : "已经是最新可用行业快照", "bad"); return; }
    $("i-date").value = formatFullDate(target);
    $("i-mode").value = "historical";
    await loadIndustry(true);
  } catch (error) {
    toast("行业交易日切换失败：" + error.message, "bad");
  } finally { updateIndustryDateButtons(); }
}

$("i-run").onclick = () => loadIndustry(true);
$("i-mode").onchange = () => {
  if ($("i-mode").value === "intraday") $("i-date").value = formatFullDate(localTodayCompact());
  loadIndustry(true);
};
$("i-date").onchange = () => {
  const date = compactDate($("i-date").value);
  const today = localTodayCompact();
  if (date > today) $("i-date").value = formatFullDate(today);
  if ($("i-mode").value === "latest_complete" || ($("i-mode").value === "intraday" && date !== today)) {
    $("i-mode").value = "historical";
  }
  loadIndustry(true);
};
$("i-history-days").onchange = () => loadIndustry(true);
$("i-topn").onchange = () => loadIndustry(true);
$("i-search").addEventListener("input", renderIndustryTable);
$("i-sort").onchange = () => {
  const [key, dir] = $("i-sort").value.split("-");
  _industrySort = { key: key === "mom5" ? "sec_mom_5d" : key, dir };
  renderIndustryTable();
};
$("i-date-prev").onclick = () => shiftIndustryDate(-1);
$("i-date-next").onclick = () => shiftIndustryDate(1);
$("i-bars").addEventListener("click", (event) => {
  const target = event.target.closest("[data-industry-code]");
  if (target) selectIndustry(target.dataset.industryCode);
});
$("i-heat").addEventListener("click", (event) => {
  const target = event.target.closest("[data-industry-code]");
  if (target) selectIndustry(target.dataset.industryCode);
});
$("i-result").addEventListener("click", (event) => {
  const header = event.target.closest("[data-industry-sort]");
  if (header) {
    const key = header.dataset.industrySort;
    _industrySort = _industrySort.key === key ? { key, dir: _industrySort.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "name" ? "asc" : "desc" };
    renderIndustryTable();
    return;
  }
  const row = event.target.closest("[data-industry-code]");
  if (row) selectIndustry(row.dataset.industryCode);
});

/* ---------- 量化选股看板 ---------- */
const CATEGORY_LABEL = { auto: "每日自动", manual: "用户触发", watch: "关注", holding: "持仓" };
const fmtMaybe = (value, digits = 2) => value == null ? "—" : Number(value).toFixed(digits);
const pctText = (value) => value == null ? "—" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
let _selectionRows = [];
let _selectionData = {};
let _selectionTag = "";
let _selectionSort = "grouped";
let _selectionRequestSeq = 0;
let _selectionQuoteSeq = 0;

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
        <div><small>最近涨幅</small><b class="${chgCls}">${pctText(r.latest_chg_pct)}</b></div>
        <div><small>换手率</small><b>${pctText(r.turnover_rate)}</b></div>
        <div><small>最近成交额</small><b>${amountYi}</b></div>
        <div><small>筛选排名</small><b>${rank}</b></div>
      </div>
      <div class="selection-context"><b>核心事件：</b>${esc(r.core_event || r.event || "未标注")}</div>
      <div class="selection-context"><b>入选理由：</b>${esc(r.reason || "未填写")}</div>
      <div class="selection-detail-footer">
        <div class="factor-snapshot-wrap">
          <details class="factor-snapshot"><summary>查看因子快照</summary><p>${factors}</p></details>
          <div class="selection-audit">入选时间 ${esc(r.selected_at || r.logged_at || "—")} · 入选价格日期 ${esc(r.selected_price_date || "未知")}${r.price_backfilled_at ? ` · 已补齐入选价` : ""} · 行情更新 ${esc(r.latest_quote_time || "—")}</div>
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
  const backfilled = Number(_selectionData.backfilled_prices || 0);
  $("sl-meta").textContent = `${quoteLabel} · ${_selectionRows.length} 条${backfilled ? ` · 自动补价 ${backfilled} 条` : ""}${quoteErrors.length ? ` · 行情错误 ${quoteErrors.length} 条` : ""}`;
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
  const requestSeq = ++_selectionRequestSeq;
  _selectionsLoaded = true;
  $("sl-result").innerHTML = '<div class="empty">加载选股记录与当前行情…</div>';
  const refreshButton = $("sl-refresh-quotes");
  refreshButton.disabled = true;
  refreshButton.textContent = "加载中…";
  const params = { limit: 500 };
  const from = $("sl-from").value.replaceAll("-", "");
  const to = $("sl-to").value.replaceAll("-", "");
  const hotspot = $("sl-hotspot").value.trim();
  const category = $("sl-category").value;
  if (from) params.date_from = from;
  if (to) params.date_to = to;
  if (hotspot) params.hotspot = hotspot;
  if (category) params.category = category;
  try {
    const data = await call("selection_dashboard", params);
    if (requestSeq === _selectionRequestSeq) drawSelections(data);
  } catch (e) {
    if (requestSeq !== _selectionRequestSeq) return;
    _selectionsLoaded = false;
    _selectionRows = [];
    _selectionData = {};
    renderSelectionTags();
    $("sl-list-meta").textContent = "";
    $("sl-refreshed").textContent = "刷新失败";
    $("sl-result").innerHTML = '<div class="empty">选股看板加载失败：' + esc(e.message) + "</div>";
  } finally {
    if (requestSeq === _selectionRequestSeq) {
      refreshButton.disabled = false;
      refreshButton.textContent = "刷新行情";
    }
  }
}

function openedSelectionIds() {
  return new Set([...document.querySelectorAll("details.selection-item[open]")]
    .map((node) => String(node.dataset.selectionId || "")));
}

function restoreOpenedSelections(ids) {
  document.querySelectorAll("details.selection-item").forEach((node) => {
    if (ids.has(String(node.dataset.selectionId || ""))) node.open = true;
  });
}

async function refreshSelectionQuotes() {
  if (!_selectionRows.length) {
    toast("当前列表为空，请先查询选股记录");
    return;
  }
  const quoteSeq = ++_selectionQuoteSeq;
  const button = $("sl-refresh-quotes");
  const progress = $("sl-refresh-progress");
  const opened = openedSelectionIds();
  button.disabled = true;
  button.textContent = "刷新中…";
  progress.classList.remove("hidden");
  $("sl-refreshed").textContent = "正在更新行情";
  try {
    const res = await fetch(cfg.base.replace(/\/$/, "") + "/selections/quotes", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": cfg.key },
      body: JSON.stringify({ items: _selectionRows.map((row) => ({ id: row.id, code: row.code })) }),
    });
    const body = await res.json();
    acceptHealth(body.health);
    if (!res.ok || body.ok === false) throw new Error(body.error || `请求失败（${res.status}）`);
    if (quoteSeq !== _selectionQuoteSeq) return;
    const quotes = body.quotes || {};
    const prices = body.selected_prices || {};
    _selectionRows = _selectionRows.map((row) => {
      const quote = quotes[String(row.code || "").toUpperCase()] || {};
      const priceInfo = prices[String(row.id)] || {};
      const selectedPrice = Number(priceInfo.selected_price ?? row.selected_price);
      const latestPrice = Number(quote.latest_price);
      const sinceSelection = Number.isFinite(selectedPrice) && selectedPrice > 0
        && Number.isFinite(latestPrice) && latestPrice > 0
        ? Math.round((latestPrice / selectedPrice - 1) * 10000) / 100 : null;
      const previousMarketTags = new Set(row.market_tags || []);
      const baseTags = (row.tags || []).filter((tag) => !previousMarketTags.has(tag));
      const marketTags = quote.market_tags || [];
      return {
        ...row, ...quote,
        selected_price: Number.isFinite(selectedPrice) && selectedPrice > 0 ? selectedPrice : null,
        selected_price_date: priceInfo.selected_price_date || row.selected_price_date,
        selected_price_source: priceInfo.selected_price_source || row.selected_price_source,
        price_backfilled_at: priceInfo.price_backfilled_at || row.price_backfilled_at,
        since_selection_pct: sinceSelection,
        market_tags: marketTags,
        tags: [...baseTags, ...marketTags.filter((tag) => !baseTags.includes(tag))],
      };
    });
    _selectionData = {
      ..._selectionData,
      refreshed_at: body.refreshed_at,
      quote_trade_date: body.quote_date,
      quote_trade_date_min: body.quote_date_min,
      quote_trade_date_max: body.quote_date_max,
      mixed_quote_dates: Boolean(body.mixed_quote_dates),
      quote_errors: body.errors || [],
      backfilled_prices: body.backfilled_prices || 0,
    };
    renderSelectionTags();
    renderSelectionRows();
    restoreOpenedSelections(opened);
    toast(`已刷新当前 ${body.record_count ?? _selectionRows.length} 条记录${body.backfilled_prices ? `，补齐 ${body.backfilled_prices} 条选股价` : ""}`, "ok");
  } catch (error) {
    if (quoteSeq === _selectionQuoteSeq) {
      $("sl-refreshed").textContent = "行情刷新失败";
      toast("行情刷新失败：" + error.message, "bad");
    }
  } finally {
    if (quoteSeq === _selectionQuoteSeq) {
      button.disabled = false;
      button.textContent = "刷新行情";
      progress.classList.add("hidden");
    }
  }
}

$("sl-run").onclick = () => loadSelections(true);
$("sl-refresh-quotes").onclick = refreshSelectionQuotes;
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
  $("pf-save").disabled = true;
  $("pf-status").textContent = "";
  $("pf-status").className = "status";
  setPortfolioTypeFields();
}

function selectPortfolioStock(stock, existing = null) {
  _portfolioSelected = { code: stock.code, name: stock.name, note: existing?.note || "" };
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
  }
  setPortfolioTypeFields();
}

function renderPortfolio(data) {
  _portfolioRows = Array.isArray(data?.rows) ? data.rows : [];
  $("pf-version").textContent = `已同步 · ${_portfolioRows.length} 只`;
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
  const item = { ..._portfolioSelected, type, note: _portfolioSelected.note || "" };
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
    toast(data.changed ? "自选已保存" : "内容无变化，无需更新", "ok");
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

function formatFullDate(value) {
  const text = String(value || "").replaceAll("-", "");
  return /^\d{8}$/.test(text) ? `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}` : String(value || "");
}

function temperatureColor(value) {
  const ratio = Math.max(0, Math.min(1, Number(value) / 100));
  const start = [59, 130, 246];
  const end = [239, 68, 68];
  const rgb = start.map((channel, index) => Math.round(channel + (end[index] - channel) * ratio));
  return `rgb(${rgb.join(",")})`;
}

function sentimentTemperatureLine(points) {
  if (!points?.length) return '<div class="empty">无数据</div>';
  const W = Math.max(720, points.length * 96), H = 260;
  const padL = 48, padR = 36, padT = 34, padB = 42;
  const xAt = (index) => padL + (W - padL - padR) * (points.length === 1 ? 0.5 : index / (points.length - 1));
  const yAt = (value) => padT + (H - padT - padB) * (1 - Number(value) / 100);
  let grid = "", yAxis = "";
  for (let value = 0; value <= 100; value += 20) {
    const y = yAt(value);
    grid += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="#eef1f7"/>`;
    yAxis += `<span style="top:${y.toFixed(1)}px">${value}</span>`;
  }
  const path = points.map((point, index) => `${index ? "L" : "M"}${xAt(index).toFixed(1)} ${yAt(point.value).toFixed(1)}`).join(" ");
  const nodes = points.map((point, index) => {
    const x = xAt(index), y = yAt(point.value), color = temperatureColor(point.value);
    return `<g class="sentiment-point ${point.selected ? "selected" : ""}" data-sentiment-date="${esc(point.date)}" tabindex="0" role="button" aria-label="切换到 ${esc(point.label)}，温度 ${esc(point.value)}">
      <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="6" fill="${color}" stroke="#fff" stroke-width="2"><title>${esc(point.label)}：${esc(point.value)}${point.isFinal === false ? "（盘中）" : ""}</title></circle>
      <text x="${x.toFixed(1)}" y="${(y - 11).toFixed(1)}" text-anchor="middle" class="sentiment-value" fill="${color}">${esc(point.value)}</text>
      <text x="${x.toFixed(1)}" y="${(y + 19).toFixed(1)}" text-anchor="middle" class="sentiment-date">${esc(point.label)}</text>
    </g>`;
  }).join("");
  const firstDate = points[0].label;
  const lastDate = points[points.length - 1].label;
  return `<div class="sentiment-line-layout">
    <div class="sentiment-y-axis" aria-hidden="true">${yAxis}</div>
    <div class="sentiment-line-scroll" data-sentiment-scroll>
      <svg viewBox="0 0 ${W} ${H}" class="chart-svg sentiment-line-svg" style="min-width:${W}px" preserveAspectRatio="xMidYMid meet">
        ${grid}<path d="${path}" fill="none" stroke="#9aa8bd" stroke-width="2" stroke-linejoin="round"/>${nodes}
      </svg>
    </div>
  </div>
  <div class="sentiment-date-axis">
    <span title="起始日期">${esc(firstDate)}</span>
    <input type="range" min="0" max="1000" value="1000" step="1" data-sentiment-range aria-label="拖动日期轴查看历史温度" />
    <span title="截止日期">${esc(lastDate)}</span>
  </div>`;
}

function initSentimentTrendScroll() {
  const root = $("s-trend");
  const viewport = root.querySelector("[data-sentiment-scroll]");
  const range = root.querySelector("[data-sentiment-range]");
  if (!viewport || !range) return;
  const maxScroll = () => Math.max(0, viewport.scrollWidth - viewport.clientWidth);
  const syncRange = () => {
    const max = maxScroll();
    range.value = max > 0 ? String(Math.round(viewport.scrollLeft / max * 1000)) : "1000";
    range.disabled = max <= 0;
  };
  range.addEventListener("input", () => {
    viewport.scrollLeft = maxScroll() * Number(range.value) / 1000;
  });
  viewport.addEventListener("scroll", syncRange, { passive: true });
  requestAnimationFrame(() => {
    viewport.scrollLeft = maxScroll();
    syncRange();
  });
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
let _sentRequestSeq = 0;
let _sentimentTrendEnd = "";
let _sentimentMaxDate = "";
let _sentimentKnownDates = [];
async function loadSentiment(force = false, forceIntradayRefresh = false) {
  if ((_sentLoaded && !force) || !cfg.key) return;
  _sentLoaded = true;
  await setSentimentDefaultDate();
  if (!_sentimentTrendEnd) _sentimentTrendEnd = $("s-date").value.trim().replaceAll("-", "");
  const [, loaded] = await Promise.all([
    syncSentimentWindow(), runSentiment({ forceIntradayRefresh }),
  ]);
  if (loaded === false) _sentLoaded = false;
}

async function setSentimentDefaultDate() {
  const input = $("s-date");
  if (!input || input.value.trim()) return;
  try {
    const h = await health();
    const defaults = MarketState.sentimentDefault(h);
    if (defaults.date) {
      _sentimentMaxDate = defaults.maxDate;
      input.max = formatFullDate(defaults.maxDate);
      input.value = formatFullDate(defaults.date);
      updateSentimentDateStepButtons();
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
  runSentiment({ forceIntradayRefresh: true });
};
$("s-date").addEventListener("change", () => {
  _sentimentTrendEnd = $("s-date").value.trim().replaceAll("-", "");
  updateSentimentDateStepButtons();
  runSentiment();
});

function offsetCompactDate(value, deltaDays) {
  if (!/^\d{8}$/.test(String(value || ""))) return "";
  const date = new Date(Date.UTC(
    Number(value.slice(0, 4)), Number(value.slice(4, 6)) - 1, Number(value.slice(6, 8))));
  date.setUTCDate(date.getUTCDate() + deltaDays);
  return date.toISOString().slice(0, 10).replaceAll("-", "");
}

function updateSentimentDateStepButtons() {
  const current = $("s-date").value.trim().replaceAll("-", "");
  $("s-date-prev").disabled = !/^\d{8}$/.test(current);
  $("s-date-next").disabled = !/^\d{8}$/.test(current)
    || Boolean(_sentimentMaxDate && current >= _sentimentMaxDate);
}

async function resolveAdjacentTradeDate(current, direction) {
  const known = [...new Set(_sentimentKnownDates)].sort();
  const cached = direction < 0
    ? [...known].reverse().find((date) => date < current)
    : known.find((date) => date > current && (!_sentimentMaxDate || date <= _sentimentMaxDate));
  if (cached) return cached;

  const start = direction < 0 ? offsetCompactDate(current, -45) : offsetCompactDate(current, 1);
  let end = direction < 0 ? offsetCompactDate(current, -1) : offsetCompactDate(current, 45);
  if (direction > 0 && _sentimentMaxDate && end > _sentimentMaxDate) end = _sentimentMaxDate;
  if (!start || !end || start > end) return "";
  const calendar = await call("meta_trade_cal", { start, end });
  const openDates = (calendar.rows || [])
    .filter((row) => Number(row.is_open) === 1)
    .map((row) => String(row.cal_date || ""))
    .filter((date) => /^\d{8}$/.test(date)
      && (!_sentimentMaxDate || date <= _sentimentMaxDate))
    .sort();
  _sentimentKnownDates = [...new Set([..._sentimentKnownDates, ...openDates])].sort();
  return direction < 0 ? (openDates.at(-1) || "") : (openDates[0] || "");
}

async function shiftSentimentDate(direction) {
  const current = $("s-date").value.trim().replaceAll("-", "");
  if (!/^\d{8}$/.test(current)) return;
  $("s-date-prev").disabled = true;
  $("s-date-next").disabled = true;
  try {
    const target = await resolveAdjacentTradeDate(current, direction);
    if (!target) {
      toast(direction < 0 ? "没有找到更早的交易日" : "已经是最新交易日", "bad");
      return;
    }
    $("s-date").value = formatFullDate(target);
    _sentimentTrendEnd = target;
    await runSentiment();
  } catch (error) {
    toast("交易日切换失败：" + error.message, "bad");
  } finally {
    updateSentimentDateStepButtons();
  }
}

$("s-date-prev").onclick = () => shiftSentimentDate(-1);
$("s-date-next").onclick = () => shiftSentimentDate(1);

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

async function runSentiment({ forceIntradayRefresh = false } = {}) {
  const requestSeq = ++_sentRequestSeq;
  let primarySucceeded = false;
  const refreshBtn = $("s-refresh");
  refreshBtn.disabled = true;
  const date = $("s-date").value.trim().replaceAll("-", "");
  const trendEnd = _sentimentTrendEnd || date;
  const days = Number($("s-days").value) || 15;
  $("s-result").innerHTML = '<div class="empty">读取中…</div>';
  $("s-trend").innerHTML = '<div class="empty">读取中…</div>';
  $("s-extreme-summary").innerHTML = '<div class="empty">读取中…</div>';
  $("s-extreme-trend").innerHTML = '<div class="empty">读取中…</div>';
  $("s-extreme-fill").style.height = "0%";
  $("s-extreme-value").textContent = "--";
  $("s-extreme-level").textContent = "极端指数";
  $("s-fallback").textContent = "";
  $("s-fallback").classList.add("hidden");
  // 单日温度 + 指标分解；强刷只交给首个请求，后续走势与极端指数复用同一快照。
  try {
    const temperatureParams = date ? { date } : {};
    if (forceIntradayRefresh) temperatureParams.force_refresh = true;
    const d = await call("sentiment_temperature", temperatureParams);
    if (requestSeq !== _sentRequestSeq) return false;
    if (d.error) throw new Error(d.error);
    setGauge(d.temperature, d.level);
    const intraday = d.data_mode === "intraday";
    const fallback = d.data_mode === "fallback";
    const staleIntraday = intraday && d.intraday_stale;
    primarySucceeded = !(fallback && date === _sentimentMaxDate && d.date !== date);
    $("s-current-date").textContent = `日期：${d.date || "最近交易日"}${intraday ? "（盘中）" : ""}`;
    $("s-data-mode").textContent = staleIntraday
      ? `盘中快照${d.intraday_as_of ? `（截至 ${String(d.intraday_as_of).slice(-8)}）` : ""}`
      : intraday ? "盘中临时温度" : fallback ? "已回退完整收盘" : "完整收盘";
    $("s-data-mode").className = `meta-chip sentiment-mode ${intraday ? "intraday" : fallback ? "fallback" : "final"}`;
    $("s-current-window").textContent = `归一窗口：${d.window_size || $("s-window").value || "--"} 日`;
    const coverage = d.weight_coverage == null ? "" : ` · 权重覆盖 ${(Number(d.weight_coverage) * 100).toFixed(0)}%`;
    $("s-meta").textContent = `${d.date} · 窗口 ${d.window_dates?.length || 0} 日${coverage}`;
    const uniqueMessages = (values) => [...new Set(values.filter(Boolean).map((value) => String(value).trim()).filter(Boolean))];
    const sourceEvents = uniqueMessages(d.intraday_source_events || []);
    const componentWarnings = uniqueMessages(d.intraday_component_warnings || []);
    const fallbackMessages = uniqueMessages([
      d.fallback_reason, d.intraday_stale_reason, ...sourceEvents, ...componentWarnings,
    ]);
    if (componentWarnings.length && d.weight_coverage != null) {
      fallbackMessages.push(`本次按剩余 ${(Number(d.weight_coverage) * 100).toFixed(0)}% 配置权重重新归一`);
    }
    const quality = d.intraday_quality || {};
    const qualityDetails = [["全市场", quality.stock], ["OHLC", quality.ohlc], ["板块", quality.sector]]
      .filter(([, item]) => item && Object.keys(item).length)
      .map(([label, item]) => `${label}覆盖：${item.valid_rows ?? item.unique_rows ?? 0}/${item.total ?? 0}（${((Number(item.ratio) || 0) * 100).toFixed(1)}%）`);
    $("s-meta").title = uniqueMessages([
      d.fallback_reason, d.intraday_stale_reason, d.turnover_note,
      ...sourceEvents, ...componentWarnings, ...qualityDetails, ...(d.intraday_errors || []),
    ]).join("\n");
    if (fallbackMessages.length) {
      $("s-fallback").textContent = `数据说明：${uniqueMessages(fallbackMessages).join("；")}`;
      $("s-fallback").classList.remove("hidden");
    }
    $("s-breadth").textContent = d.breadth?.adv != null
      ? `上涨 ${d.breadth.adv} 家 · 下跌 ${d.breadth.dec} 家${intraday ? " · 实时" : ""}` : "市场宽度暂不可用";
    const inds = d.indicators || {};
    const displayWeights = d.applied_weights || d.weights || {};
    $("s-ranges").innerHTML = renderRanges(inds, displayWeights);
    const rows = Object.keys(inds).map((key) => ({
      指标: factorLabel(key), 权重: displayWeights[key], 今值: inds[key].raw_today,
      窗口低: inds[key].window_min, 窗口均值: inds[key].window_mean, 窗口高: inds[key].window_max,
      较均值: inds[key].vs_mean, 子分: inds[key].sub_score,
    }));
    $("s-result").innerHTML = rows.length ? renderTable(rows) : '<div class="empty">无数据</div>';
  } catch (e) {
    if (requestSeq === _sentRequestSeq) {
      _sentLoaded = false;
      $("s-result").innerHTML = "";
      toast("情绪读取失败：" + e.message, "bad");
    }
  }
  // 多日温度走势（market_timing 返回温度序列）
  try {
    const params = { days };
    if (trendEnd) params.date = trendEnd;
    const t = await call("market_timing", params);
    if (requestSeq !== _sentRequestSeq) return false;
    if (t.error) throw new Error(t.error);
    const series = (t.recent || []).map((item) => ({
      date: item.date, label: formatFullDate(item.date), value: item.temperature,
      isFinal: item.is_final, selected: item.date === date,
    }));
    _sentimentKnownDates = [...new Set([
      ..._sentimentKnownDates, ...series.map((item) => item.date),
    ])].sort();
    updateSentimentDateStepButtons();
    $("s-trend").innerHTML = sentimentTemperatureLine(series);
    initSentimentTrendScroll();
    $("s-trend-meta").textContent = `${series.length} 个交易日 · 截止 ${formatFullDate(t.date || trendEnd)}${t.data_mode === "intraday" ? " · 含盘中临时点" : ""}`;
    renderTiming(t);
  } catch (e) {
    if (requestSeq === _sentRequestSeq) {
      $("s-trend").innerHTML = '<div class="empty">走势读取失败：' + esc(e.message) + "</div>";
      $("s-timing").innerHTML = '<div class="empty">择时读取失败</div>';
    }
  }
  // 情绪极端指数：详情跟随所选日期，走势窗口独立保持 trendEnd。
  try {
    const detailParams = { days };
    const detailDate = date || trendEnd;
    if (detailDate) detailParams.date = detailDate;
    const trendParams = { days };
    if (trendEnd) trendParams.date = trendEnd;
    const sameWindow = !detailDate || !trendEnd || detailDate === trendEnd;
    const detailPromise = call("sentiment_extreme_index", detailParams);
    const [e, trendData] = sameWindow
      ? await detailPromise.then((result) => [result, result])
      : await Promise.all([detailPromise, call("sentiment_extreme_index", trendParams)]);
    if (requestSeq !== _sentRequestSeq) return false;
    if (e.error) throw new Error(e.error);
    if (trendData.error) throw new Error(trendData.error);
    const series = (trendData.recent || []).map((item) => ({
      label: fmtDate(item.date), value: item.extreme_index,
    }));
    const temporary = e.is_final === false || e.model_mode === "provisional"
      || trendData.is_final === false
      || (trendData.recent || []).some((item) => item.is_final === false);
    $("s-extreme-trend").innerHTML = svgLine(series, { min: 0, max: 100, color: "#ef4444" });
    $("s-extreme-meta").textContent = `20日稳健基线 · ${series.length} 个交易日 · 走势截止 ${formatFullDate(trendData.date || trendEnd) || "—"}${temporary ? " · 含盘中临时值" : ""}`;
    $("s-extreme-meta").className = `meta-chip ${temporary ? "extreme-temporary" : ""}`;
    renderExtreme(e, date, trendEnd);
  } catch (e) {
    if (requestSeq === _sentRequestSeq) {
      $("s-extreme-summary").innerHTML = '<div class="empty">极端指数读取失败：' + esc(e.message) + "</div>";
      $("s-extreme-trend").innerHTML = '<div class="empty">极端指数走势读取失败</div>';
    }
  }
  if (requestSeq === _sentRequestSeq) refreshBtn.disabled = false;
  return primarySucceeded;
}

function selectSentimentDate(date) {
  if (!/^\d{8}$/.test(String(date || ""))) return;
  $("s-date").value = `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}`;
  updateSentimentDateStepButtons();
  runSentiment();
}

$("s-trend").addEventListener("click", (event) => {
  const point = event.target.closest("[data-sentiment-date]");
  if (point) selectSentimentDate(point.dataset.sentimentDate);
});
$("s-trend").addEventListener("keydown", (event) => {
  if (!["Enter", " "].includes(event.key)) return;
  const point = event.target.closest("[data-sentiment-date]");
  if (point) {
    event.preventDefault();
    selectSentimentDate(point.dataset.sentimentDate);
  }
});

const EXTREME_COMPONENTS = [
  { key: "volatility", label: "波动强度" },
  { key: "volume_shock", label: "量能冲击" },
  { key: "kline_shock", label: "K线冲击" },
  { key: "breadth_extreme", label: "市场宽度极端" },
  { key: "limit_shock", label: "涨跌停冲击" },
];

function extremeComponentValue(component) {
  if (typeof component === "number") return component;
  if (!component || typeof component !== "object") return null;
  for (const key of ["robust_strength", "normalized_20d", "normalized", "score", "sub_score", "value"]) {
    const number = Number(component[key]);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function extremeWeightText(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${(number <= 1 ? number * 100 : number).toFixed(0)}%`;
}

function renderExtreme(e, selectedDate = "", trendEnd = "") {
  const value = Math.max(0, Math.min(100, Number(e.extreme_index) || 0));
  const color = value >= 80 ? "#ef4444" : (value >= 60 ? "#f97316" : (value >= 40 ? "#f59e0b" : "#3b82f6"));
  const temporary = e.is_complete === false || e.is_final === false || e.data_mode === "intraday";
  const configured = e.configured_weights || e.weights || {};
  const applied = e.applied_weights || configured;
  const missing = new Set(e.missing_components || []);
  const componentLabels = Object.fromEntries(EXTREME_COMPONENTS.map((item) => [item.key, item.label]));
  const coverageNumber = Number(e.component_coverage);
  const coverage = Number.isFinite(coverageNumber)
    ? `${(coverageNumber <= 1 ? coverageNumber * 100 : coverageNumber).toFixed(0)}%` : "—";
  const modeLabels = {
    final: "完整模型", provisional: "盘中临时模型", full: "完整模型", complete: "完整模型",
    partial: "部分组件模型", degraded: "降级模型", fallback: "回退模型", intraday: "盘中临时模型",
  };
  const modelMode = modeLabels[e.model_mode] || (temporary ? "盘中临时模型" : "模型模式未标注");
  const componentCards = EXTREME_COMPONENTS.map(({ key, label }) => {
    const component = e.components?.[key];
    const score = extremeComponentValue(component);
    const raw = component && typeof component === "object"
      ? component.raw_today ?? component.raw ?? component.current : null;
    const unavailable = missing.has(key) || score == null;
    return `<div class="extreme-component ${unavailable ? "missing" : ""}"><small>${label}</small><b>${unavailable ? "缺失" : industryValueText(score, 1)}</b><span>${raw == null ? "20日稳健基线" : `原值 ${esc(industryValueText(raw, 3))}`}</span></div>`;
  }).join("");
  const weightRows = EXTREME_COMPONENTS.map(({ key, label }) => `
    <div><span>${label}</span><b>${extremeWeightText(configured[key])}</b><i>→</i><strong>${extremeWeightText(applied[key])}</strong></div>`).join("");
  const missingText = [...missing].map((key) => componentLabels[key] || "未识别组件").join("、") || "无";
  const selectedText = formatFullDate(selectedDate) || "最近交易日";
  const trendText = formatFullDate(e.date || trendEnd) || "最近交易日";
  const fallbackHtml = e.fallback_reason
    ? `<div class="extreme-fallback">回退原因：${esc(e.fallback_reason)}</div>` : "";
  $("s-extreme-fill").style.height = `${value}%`;
  $("s-extreme-fill").style.background = color;
  $("s-extreme-bulb").style.background = color;
  $("s-extreme-value").textContent = e.extreme_index ?? "--";
  $("s-extreme-level").textContent = `${e.level || "极端指数"}${temporary ? "（盘中临时值）" : ""}`;
  $("s-extreme-summary").innerHTML = `
    <div class="extreme-context ${temporary ? "temporary" : ""}">
      <span>当前选择日：<b>${esc(selectedText)}</b></span><span>走势截止日：<b>${esc(trendText)}</b></span>${temporary ? "<strong>盘中临时值</strong>" : ""}
    </div>
    ${fallbackHtml}
    <div class="extreme-components">${componentCards}</div>
    <div class="extreme-model-grid">
      <div class="extreme-model-state"><small>模型状态</small><b>${esc(modelMode)}</b><span>组件覆盖 ${coverage}</span><span>缺失组件：${esc(missingText)}</span></div>
      <div class="extreme-weights"><div class="extreme-weight-head"><span>组件</span><b>配置</b><strong>实际</strong></div>${weightRows}</div>
    </div>
    <div class="timing-stance">${esc(e.selection_bias || e.note || "指数越高表示市场状态越极端，但不表示上涨或下跌方向。")}</div>`;
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
const HZ_ORDER = ["1t", "2t", "3t", "7c", "30c"];
const HZ_LABEL = {
  "1t": "1交易日", "2t": "2交易日", "3t": "3交易日",
  "7c": "7自然日", "30c": "30自然日",
};
const RETURN_STATUS_LABEL = {
  success: "已成熟", not_matured: "未成熟", failed: "计算失败", missing: "待计算",
};
let _backtestDetails = [];
let _btLoaded = false;
let _btPage = 1;
let _btQuickRange = "all";

function backtestReturnValue(row, horizon) {
  const field = $("b-return-mode").value === "excess" ? "excess_pct" : "returns_pct";
  return finiteNumber(row[field]?.[horizon]);
}

function returnChip(row, horizon) {
  const status = row.return_status?.[horizon] || "missing";
  const value = backtestReturnValue(row, horizon);
  const cls = value == null ? "pending" : (value >= 0 ? "pos" : "neg");
  const exitDate = row.return_exit_dates?.[horizon];
  const error = row.return_errors?.[horizon];
  const mode = $("b-return-mode").value === "excess" ? "超额" : "涨幅";
  const title = [RETURN_STATUS_LABEL[status] || status, exitDate ? `退出 ${exitDate}` : "", error || ""]
    .filter(Boolean).join(" · ");
  return `<div class="return-chip ${cls}" title="${esc(title)}">
    <small>${HZ_LABEL[horizon]}${mode}</small><b>${pctText(value)}</b><span>${esc(exitDate || RETURN_STATUS_LABEL[status] || status)}</span>
  </div>`;
}

function backtestDateList() {
  return [...new Set(_backtestDetails.map((row) => String(row.date || "").slice(0, 10)).filter(Boolean))].sort().reverse();
}

function syncBacktestQuickButtons() {
  [["b-quick-all", "all"], ["b-quick-3", "3"], ["b-quick-1", "1"]].forEach(([id, value]) => {
    $(id).classList.toggle("active", _btQuickRange === value);
  });
}

function setBacktestDateRange(mode) {
  const dates = backtestDateList();
  _btQuickRange = mode;
  _btPage = 1;
  if (dates.length) {
    const count = mode === "1" ? 1 : mode === "3" ? 3 : dates.length;
    const selected = dates.slice(0, count);
    $("b-date-from").value = selected[selected.length - 1];
    $("b-date-to").value = selected[0];
  } else {
    $("b-date-from").value = "";
    $("b-date-to").value = "";
  }
  syncBacktestQuickButtons();
  renderBacktestDetails();
}

function filteredBacktestRows() {
  const category = $("b-category").value;
  const from = $("b-date-from").value;
  const to = $("b-date-to").value;
  const maturity = $("b-maturity").value;
  const rows = _backtestDetails.filter((row) => {
    const date = String(row.date || "").slice(0, 10);
    const status = row.return_status?.["30c"] || "missing";
    return (!category || row.category === category)
      && (!from || date >= from) && (!to || date <= to)
      && (!maturity || status === maturity);
  });
  const key = $("b-sort").value;
  const direction = $("b-order").value === "asc" ? 1 : -1;
  const metric = (row) => {
    if (key === "date") return String(row.date || "");
    if (key === "score") return finiteNumber(row.score);
    if (key === "since") return finiteNumber(row.since_selection_pct);
    return backtestReturnValue(row, key);
  };
  rows.sort((left, right) => {
    const a = metric(left); const b = metric(right);
    if (a == null && b != null) return 1;
    if (a != null && b == null) return -1;
    if (a != null && b != null) {
      const compared = typeof a === "string" ? a.localeCompare(b) : a - b;
      if (compared) return compared * direction;
    }
    const dateCompared = String(right.date || "").localeCompare(String(left.date || ""));
    return dateCompared || Number(right.id || 0) - Number(left.id || 0);
  });
  return rows;
}

function backtestPager(total, totalPages) {
  if (!total) return "";
  return `<button type="button" data-bt-page="first" ${_btPage <= 1 ? "disabled" : ""}>首页</button>
    <button type="button" data-bt-page="prev" ${_btPage <= 1 ? "disabled" : ""}>上一页</button>
    <span>第 <b>${_btPage}</b> / ${totalPages} 页 · 共 ${total} 条</span>
    <button type="button" data-bt-page="next" ${_btPage >= totalPages ? "disabled" : ""}>下一页</button>
    <button type="button" data-bt-page="last" ${_btPage >= totalPages ? "disabled" : ""}>末页</button>`;
}

function bindBacktestPagers(totalPages) {
  document.querySelectorAll("[data-bt-page]").forEach((button) => {
    button.onclick = () => {
      const action = button.dataset.btPage;
      if (action === "first") _btPage = 1;
      if (action === "prev") _btPage = Math.max(1, _btPage - 1);
      if (action === "next") _btPage = Math.min(totalPages, _btPage + 1);
      if (action === "last") _btPage = totalPages;
      renderBacktestDetails();
    };
  });
}

function renderBacktestSummary(rows) {
  const since = rows.map((row) => finiteNumber(row.since_selection_pct)).filter((value) => value != null);
  const average = since.length ? since.reduce((sum, value) => sum + value, 0) / since.length : null;
  const winRate = since.length ? since.filter((value) => value > 0).length / since.length * 100 : null;
  const mature = rows.filter((row) => row.return_status?.["30c"] === "success").length;
  const matureRate = rows.length ? mature / rows.length * 100 : null;
  const signClass = average == null ? "" : average >= 0 ? "bt-up" : "bt-down";
  $("b-detail-summary").innerHTML = `
    <div><small>筛选记录</small><b>${rows.length}</b></div>
    <div><small>至今平均</small><b class="${signClass}">${pctText(average)}</b></div>
    <div><small>至今盈利占比</small><b>${pctText(winRate)}</b></div>
    <div><small>30日成熟占比</small><b>${pctText(matureRate)}</b></div>`;
}

function renderBacktestDetails() {
  const rows = filteredBacktestRows();
  const pageSize = Number($("b-page-size").value) || 20;
  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
  _btPage = Math.min(Math.max(1, _btPage), totalPages);
  const pageRows = rows.slice((_btPage - 1) * pageSize, _btPage * pageSize);
  $("b-detail-meta").textContent = `筛选 ${rows.length} / ${_backtestDetails.length} 条 · 第 ${_btPage}/${totalPages} 页`;
  renderBacktestSummary(rows);
  const pager = backtestPager(rows.length, totalPages);
  $("b-page-top").innerHTML = pager;
  $("b-page-bottom").innerHTML = pager;
  if (!pageRows.length) {
    $("b-detail").innerHTML = '<div class="empty">当前筛选条件没有回测明细</div>';
    bindBacktestPagers(totalPages);
    return;
  }
  $("b-detail").innerHTML = pageRows.map((row) => {
    const percentile = row.score_percentile == null ? "—" : `${(Number(row.score_percentile) * 100).toFixed(1)}%`;
    const since = finiteNumber(row.since_selection_pct);
    const sinceClass = since == null ? "" : since >= 0 ? "bt-up" : "bt-down";
    const latestChange = finiteNumber(row.latest_chg_pct);
    const latestClass = latestChange == null ? "" : latestChange >= 0 ? "bt-up" : "bt-down";
    return `<article class="backtest-detail-item">
      <div class="backtest-detail-main">
        <div class="backtest-detail-ticker"><b>${esc(row.name || "-")}</b><code>${esc(row.code)}</code></div>
        <div><small>选股日期</small><b>${esc(row.date || "—")}</b></div>
        <div><small>来源 / 驱动</small><b>${esc(CATEGORY_LABEL[row.category] || row.category)} · ${esc(row.driver || "未标注")}</b></div>
        <div><small>评分 / 分位</small><b>${fmtMaybe(row.score)} / ${percentile}</b></div>
        <div class="backtest-since"><small>选股至今</small><b class="${sinceClass}">${pctText(since)}</b><span>现价 ${fmtMaybe(row.latest_price)} · 当日 <i class="${latestClass}">${pctText(latestChange)}</i></span></div>
        <span class="controlled-badge ${row.controlled_auto ? "yes" : "no"}">${row.controlled_auto ? "受控样本" : "非调参样本"}</span>
      </div>
      <div class="return-chip-row">${HZ_ORDER.map((horizon) => returnChip(row, horizon)).join("")}</div>
      <div class="backtest-detail-foot">选股价 ${fmtMaybe(row.selected_price)} · 样本组 ${esc(row.bucket || "—")} · 最近行情 ${esc(row.latest_quote_time || "不可用")}</div>
    </article>`;
  }).join("");
  bindBacktestPagers(totalPages);
}

function exportBacktestRows() {
  const rows = filteredBacktestRows();
  if (!rows.length) { toast("当前筛选没有可导出的明细", "bad"); return; }
  const headers = ["选股日期", "股票名称", "股票代码", "类别", "驱动", "选股评分", "评分分位", "选股价", "最新价", "选股至今涨幅"];
  HZ_ORDER.forEach((horizon) => headers.push(`${HZ_LABEL[horizon]}涨幅`, `${HZ_LABEL[horizon]}超额`, `${HZ_LABEL[horizon]}状态`));
  const csvCell = (value) => {
    let text = value == null ? "" : String(value);
    if (/^[=+@]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  const lines = [headers.map(csvCell).join(",")];
  rows.forEach((row) => {
    const values = [row.date, row.name, row.code, CATEGORY_LABEL[row.category] || row.category, row.driver,
      row.score, row.score_percentile, row.selected_price, row.latest_price, row.since_selection_pct];
    HZ_ORDER.forEach((horizon) => values.push(row.returns_pct?.[horizon], row.excess_pct?.[horizon], RETURN_STATUS_LABEL[row.return_status?.[horizon]] || row.return_status?.[horizon]));
    lines.push(values.map(csvCell).join(","));
  });
  const blob = new Blob(["\ufeff", lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
  anchor.href = url; anchor.download = `选股回测_${$("b-date-from").value || "开始"}_${$("b-date-to").value || "最新"}.csv`;
  document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
}

["b-category", "b-return-mode", "b-maturity", "b-sort", "b-order", "b-page-size"].forEach((id) => {
  $(id).onchange = () => { _btPage = 1; renderBacktestDetails(); };
});
["b-date-from", "b-date-to"].forEach((id) => {
  $(id).onchange = () => { _btQuickRange = "custom"; _btPage = 1; syncBacktestQuickButtons(); renderBacktestDetails(); };
});
$("b-quick-all").onclick = () => setBacktestDateRange("all");
$("b-quick-3").onclick = () => setBacktestDateRange("3");
$("b-quick-1").onclick = () => setBacktestDateRange("1");
$("b-export").onclick = exportBacktestRows;
$("b-run").onclick = () => loadBacktest(true);

async function loadBacktest(force = false) {
  if (_btLoaded && !force) return;
  _btLoaded = true;
  const button = $("b-run");
  button.disabled = true;
  button.lastChild.textContent = " 加载中…";
  const setLoading = (id) => ($(id).innerHTML = '<div class="empty">加载中…</div>');
  ["b-auto-ret", "b-auto-win", "b-driver", "b-pred-acc", "b-detail"].forEach(setLoading);
  $("b-page-top").innerHTML = "";
  $("b-page-bottom").innerHTML = "";
  $("b-detail-summary").innerHTML = '<div class="empty">正在汇总筛选结果…</div>';
  $("b-hints").innerHTML = '<div class="empty">加载中…</div>';
  $("b-gate-reasons").innerHTML = "";

  try {
    const d = await call("selection_backtest", { save_snapshot: true });
    const gate = d.optimization_gate || {};
    const eligible = Boolean(gate.eligible);
    $("b-meta").textContent = `${d.fetched_at || ""} · ${d.total_selections ?? 0} 条`;
    $("b-kpi-total").textContent = d.total_selections ?? 0;
    $("b-kpi-controlled").textContent = gate.controlled_sample_count ?? 0;
    $("b-kpi-oos").textContent = gate.oos_sample_count ?? 0;
    $("b-kpi-gate").textContent = eligible ? "已开放" : "已锁定";
    $("b-kpi-gate").className = `gate-value ${eligible ? "open" : "locked"}`;
    $("b-kpi-gate-sub").textContent = eligible ? "可依据建议人工调参" : `${(gate.reasons || []).length} 项条件未满足`;
    $("b-gate-badge").textContent = eligible ? "允许分析调参" : "禁止自动调参";
    $("b-gate-badge").className = `gate-badge ${eligible ? "open" : "locked"}`;
    const quoteErrors = Array.isArray(d.quote_errors) ? d.quote_errors : [];
    $("b-version").textContent = `计算版本 ${d.return_calc_version || "—"} · 快照 ${d.snapshot_id ?? "未保存"} · 本次重算 ${d.recomputed_samples ?? 0} 条 · 自动补价 ${d.backfilled_prices ?? 0} 条 · 行情 ${d.quote_refreshed_at || "不可用"}${quoteErrors.length ? ` · ${quoteErrors.length} 项行情缺口` : ""}`;
    $("b-gate-reasons").innerHTML = (gate.reasons || []).length
      ? (gate.reasons || []).map((reason) => `<span>${esc(reason)}</span>`).join("")
      : '<span class="passed">样本量、日期覆盖与样本外表现均通过</span>';

    const auto = d.by_category_return?.auto || null;
    if (auto) {
      const ret = HZ_ORDER.filter((h) => auto[h]).map((h) => ({ label: HZ_LABEL[h], value: auto[h].avg_pct }));
      const win = HZ_ORDER.filter((h) => auto[h]).map((h) => ({ label: HZ_LABEL[h], value: auto[h].win_rate }));
      $("b-auto-ret").innerHTML = ret.length ? svgBars(ret, { unit: "%" }) : '<div class="empty">暂无成熟收益样本</div>';
      $("b-auto-win").innerHTML = win.length ? svgBars(win, { unit: "%" }) : '<div class="empty">暂无成熟胜率样本</div>';
    } else {
      $("b-auto-ret").innerHTML = '<div class="empty">暂无自动选股成熟样本</div>';
      $("b-auto-win").innerHTML = '<div class="empty">—</div>';
    }

    const driver = d.auto_by_driver_excess || {};
    const driverItems = Object.keys(driver)
      .filter((key) => driver[key]?.["30c"])
      .map((key) => ({ label: key, value: driver[key]["30c"].avg_pct }));
    $("b-driver").innerHTML = driverItems.length
      ? svgBars(driverItems, { unit: "%" })
      : '<div class="empty">30自然日受控超额样本不足</div>';

    const hints = d.tuning_hints || [];
    $("b-hints").innerHTML = hints.length
      ? '<ul class="hint-list">' + hints.map((hint) => `<li>${esc(hint)}</li>`).join("") + "</ul>"
      : '<div class="empty">暂无建议</div>';

    const hadDetails = _backtestDetails.length > 0;
    _backtestDetails = (d.details || []).slice();
    if (!hadDetails || !$("b-date-from").value || !$("b-date-to").value) {
      setBacktestDateRange("all");
    } else {
      _btPage = 1;
      renderBacktestDetails();
    }
  } catch (error) {
    _btLoaded = false;
    _backtestDetails = [];
    $("b-page-top").innerHTML = "";
    $("b-page-bottom").innerHTML = "";
    $("b-detail-summary").innerHTML = '<div class="empty">明细加载失败</div>';
    ["b-auto-ret", "b-auto-win", "b-driver", "b-detail"].forEach((id) => ($(id).innerHTML = ""));
    $("b-hints").innerHTML = "";
    $("b-meta").textContent = "加载失败";
    toast("选股回测加载失败：" + error.message, "bad");
  }

  try {
    const d = await call("predictions_backtest", {});
    $("b-pred-meta").textContent = d.trade_date ? `${d.trade_date} · ${d.correct}/${d.total} 命中` : "";
    const accuracy = d.accuracy_by_driver || {};
    const items = Object.keys(accuracy)
      .filter((key) => accuracy[key] != null)
      .map((key) => ({ label: key, value: accuracy[key] }));
    if (d.accuracy_pct != null) items.unshift({ label: "总体", value: d.accuracy_pct });
    $("b-pred-acc").innerHTML = items.length
      ? svgBars(items, { unit: "%" })
      : '<div class="empty">当日无可回测预判</div>';
  } catch (error) {
    $("b-pred-acc").innerHTML = '<div class="empty">预判回测加载失败：' + esc(error.message) + "</div>";
  } finally {
    button.disabled = false;
    button.lastChild.textContent = " 刷新回测";
  }
}

function updateLogScopeUI() {
  const scope = $("log-scope").value;
  document.querySelectorAll("[data-log-mode]").forEach((node) => {
    node.classList.toggle("hidden", node.dataset.logMode !== scope);
  });
}

$("log-scope").onchange = updateLogScopeUI;
const logTodayCompact = MarketState.context(_marketHealth).serverDate;
const todayText = `${logTodayCompact.slice(0, 4)}-${logTodayCompact.slice(4, 6)}-${logTodayCompact.slice(6, 8)}`;
$("log-date").value = todayText;
$("log-from").value = todayText;
$("log-to").value = todayText;
$("log-download").onclick = async () => {
  const button = $("log-download");
  const scope = $("log-scope").value;
  const params = new URLSearchParams({ scene: $("log-scene").value, scope });
  if (scope === "date") params.set("date", $("log-date").value.replaceAll("-", ""));
  if (scope === "range") {
    params.set("date_from", $("log-from").value.replaceAll("-", ""));
    params.set("date_to", $("log-to").value.replaceAll("-", ""));
  }
  button.disabled = true;
  button.textContent = "准备下载…";
  try {
    const response = await fetch(`${cfg.base.replace(/\/$/, "")}/admin/logs/download?${params}`, {
      headers: { "X-API-Key": cfg.key },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "stock-agent-logs.jsonl";
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
    toast("日志下载已开始", "ok");
  } catch (error) {
    toast("日志下载失败：" + error.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = "下载日志";
  }
};

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
      <span class="model-sum">当前生效配置</span>
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
      if (r.applied) {
        const message = model === "sector"
          ? "已保存行业权重；旧合同快照立即停用，服务端将在 16:00 日终收口后自动重算"
          : `已保存 ${MODEL_LABEL[model] || model} 权重，新请求立即生效`;
        toast(message, "ok");
        loadWeights();
      }
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
      <div><div class="pc-task-title">${esc(mode)}<span class="pc-state ${esc(task.status)}">${esc(label)}</span></div></div>
      <span class="meta">开始于 ${esc(task.started_at || "—")}</span>
    </div>
    <div class="pc-progress-row"><div class="pc-progress"><i style="width:${progress}%"></i></div><span class="pc-progress-value">${progress}%</span></div>
    <div class="pc-task-grid">
      <div class="pc-task-stat"><small>当前阶段</small><b>${esc(task.stage || "—")}</b></div>
      <div class="pc-task-stat"><small>当前交易日</small><b>${esc(task.current_date || "—")}</b></div>
      <div class="pc-task-stat"><small>日期进度</small><b>${esc(count)}</b></div>
      <div class="pc-task-stat"><small>最近更新</small><b>${esc(task.heartbeat_at || "—")}</b></div>
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
  $("pc-meta").textContent = `${latest}${usable}`;
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

/* ---------- 服务端量化盯盘 ---------- */
let _qwSocket = null;
let _qwReconnectTimer = null;
let _qwSocketGeneration = 0;
let _qwConfigLoaded = false;
let _qwRequestedTradeDate = "";
let _qwCurrentTradeDate = "";
let _qwLoadSequence = 0;

const quantWatchActive = () => document.querySelector('.tab.active')?.dataset.tab === "quant-watch";
const quantWatchShouldConnect = () => isAdmin() && quantWatchActive()
  && !_qwRequestedTradeDate && document.visibilityState === "visible";
const splitTerms = (value) => String(value || "").replaceAll("，", ",").split(",").map((item) => item.trim()).filter(Boolean);
const amountText = (value) => {
  const number = finiteNumber(value);
  if (number == null) return "—";
  if (Math.abs(number) >= 1e8) return `${finiteNumberText(number / 1e8, { digits: 2 })}亿`;
  if (Math.abs(number) >= 1e4) return `${finiteNumberText(number / 1e4, { digits: 1 })}万`;
  return finiteNumberText(number, { digits: 0 });
};

function stopQuantWatchSocket() {
  _qwSocketGeneration += 1;
  if (_qwReconnectTimer) clearTimeout(_qwReconnectTimer);
  _qwReconnectTimer = null;
  const socket = _qwSocket;
  _qwSocket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "量化盯盘连接已停止");
  const badge = $("qw-connection");
  if (badge) { badge.textContent = "未连接"; badge.className = "qw-connection off"; }
}

function fillQuantWatchConfig(data, force = false) {
  if (_qwConfigLoaded && !force) return;
  const config = data.config || {};
  $("qw-mode").value = config.universe_mode || "market";
  $("qw-interval").value = config.interval_seconds ?? 60;
  $("qw-window").value = config.window_minutes ?? 5;
  $("qw-threshold").value = config.qualified_score ?? 72;
  $("qw-industries").value = (config.industries || []).join(",");
  $("qw-themes").value = (config.themes || []).join(",");
  $("qw-enabled").checked = config.enabled !== false;
  $("qw-notify").checked = Boolean(config.notify_enabled);
  $("qw-feishu").checked = (config.notify_channels || []).includes("feishu");
  $("qw-wecom").checked = (config.notify_channels || []).includes("wecom");
  document.querySelectorAll('#qw-boards input[type="checkbox"]').forEach((input) => {
    input.checked = (config.boards || ["main", "gem"]).includes(input.value);
  });
  const channels = data.notification_channels || {};
  [["qw-feishu", "feishu"], ["qw-wecom", "wecom"]].forEach(([id, name]) => {
    const input = $(id);
    input.title = channels[name] ? "webhook 已配置" : "服务端尚未配置 webhook，保存后也不会发送";
  });
  _qwConfigLoaded = true;
}

function qwStockTable(title, rows, limit = 20) {
  const items = (rows || []).slice(0, limit);
  if (!items.length) return `<div class="qw-block"><h4>${esc(title)}</h4><div class="empty compact">本轮没有达到条件的标的</div></div>`;
  const body = items.map((row) => {
    const pctChange = finiteNumber(row.pct_change);
    const speedPct = finiteNumber(row.speed_pct);
    return `<tr class="${row.is_priority ? "priority" : ""}"><td><b>${esc(row.name || "—")}</b><code>${esc(row.code || "")}</code>${row.is_priority ? '<span class="qw-priority">优先</span>' : ""}</td><td class="score">${finiteNumberText(row.score, { digits: 2, trim: true })}</td><td class="${finiteNumberClass(pctChange)}">${finiteNumberText(pctChange, { digits: 2, suffix: "%" })}</td><td class="${finiteNumberClass(speedPct)}">${finiteNumberText(speedPct, { digits: 3, suffix: "%", fallback: "样本不足" })}</td><td>${amountText(row.window_amount)}</td><td>${finiteNumberText(row.sector_score, { digits: 2, trim: true })}</td><td>${esc(row.reason || "—")}</td></tr>`;
  }).join("");
  return `<div class="qw-block"><h4>${esc(title)} <small>${items.length}只</small></h4><div class="table-wrap"><table class="qw-table"><thead><tr><th>标的</th><th>评分</th><th>涨跌</th><th>窗口涨速</th><th>窗口成交额</th><th>行业分</th><th>依据</th></tr></thead><tbody>${body}</tbody></table></div></div>`;
}

function renderQuantWatchSectors(latest) {
  const box = $("qw-sectors");
  const top = latest?.sector_rotation?.top_by_level || {};
  const rows = ["L1", "L2", "L3"].flatMap((level) => (top[level] || []).slice(0, 8));
  if (!rows.length) {
    const quality = latest?.quality?.sector_membership || {};
    box.innerHTML = `<div class="empty">${esc(quality.reason || "本轮没有可靠行业轮动数据")}</div>`;
    return;
  }
  box.innerHTML = `<table><thead><tr><th>级别</th><th>行业</th><th>轮动分</th><th>窗口涨速</th><th>窗口成交额</th><th>与指数</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${esc(row.level)}</td><td><b>${esc(row.name)}</b><code>${esc(row.code)}</code></td><td>${finiteNumberText(row.score, { digits: 1 })}</td><td class="${finiteNumberClass(row.speed_pct)}">${finiteNumberText(row.speed_pct, { digits: 3, suffix: "%" })}</td><td>${amountText(row.window_amount)}</td><td>${esc(row.index_sync || "—")}</td></tr>`).join("")}</tbody></table>`;
}

function quantWatchWindowQualityText(quality) {
  const windowQuality = quality.window;
  if (windowQuality == null) return "短线变化数据待确认";
  const status = typeof windowQuality === "object" ? windowQuality.status : windowQuality;
  const reason = typeof windowQuality === "object"
    ? windowQuality.warmup_reason || windowQuality.reason || windowQuality.note : "";
  const statusNames = { available: "短线变化数据已就绪", warming: "正在积累短线数据", unavailable: "短线变化数据不可用", stale: "短线变化数据已过时", error: "短线变化数据异常" };
  const statusText = statusNames[String(status || "").toLowerCase()] || "短线变化数据待确认";
  return `${statusText}${reason ? `（${reason}）` : ""}`;
}

function renderQuantWatch(data, forceConfig = false) {
  fillQuantWatchConfig(data, forceConfig);
  const state = data.state || {};
  const latest = data.latest;
  const summary = latest?.market_summary || {};
  const effectiveDate = String(data.effective_trade_date || data.trade_date || "");
  const currentDate = String(data.current_trade_date || "");
  const historical = Boolean(data.is_historical);
  _qwCurrentTradeDate = currentDate;
  if (effectiveDate.length === 8) $("qw-date").value = `${effectiveDate.slice(0, 4)}-${effectiveDate.slice(4, 6)}-${effectiveDate.slice(6, 8)}`;
  if (currentDate.length === 8) $("qw-date").max = `${currentDate.slice(0, 4)}-${currentDate.slice(4, 6)}-${currentDate.slice(6, 8)}`;
  const stateNames = {
    running: "运行中", waiting: "等待交易时段", disabled: "已停用",
    unavailable: "服务不可用", degraded: "服务降级", error: "异常",
  };
  $("qw-kpi-status").textContent = historical ? "历史回看" : (stateNames[state.status] || state.status || "—");
  $("qw-kpi-time").textContent = latest?.scanned_at?.slice(11) || (!historical ? state.last_scan_at?.slice(11) : "—") || "—";
  $("qw-kpi-count").textContent = finiteNumberText(summary.scanned_count, { digits: 0 });
  $("qw-kpi-signal").textContent = latest
    ? `${finiteNumberText(summary.qualified_count, { digits: 0 })} / ${finiteNumberText(summary.priority_alert_count, { digits: 0 })}`
    : "—";
  if (latest) {
    const quality = latest.quality || {};
    const sector = quality.sector_membership || {};
    const minute = quality.minute_indicators || {};
    const universe = latest.universe || {};
    const missingCodes = Array.isArray(universe.missing_priority_codes) ? universe.missing_priority_codes : null;
    const priorityErrors = Array.isArray(quality.priority_errors) ? quality.priority_errors : null;
    const completeText = universe.priority_complete === true ? "完整" : universe.priority_complete === false ? "有缺失" : "待确认";
    const minuteCount = finiteNumberText(minute.sample_count, { digits: 0 });
    const sectorLevels = (sector.levels || []).length;
    const qualityParts = [
      `行情更新 ${quality.quote_as_of || "待确认"}`,
      `分时数据已积累 ${minuteCount} 次`,
      quantWatchWindowQualityText(quality),
      `行业覆盖 ${sectorLevels ? `${sectorLevels} 个层级` : "不可用"}`,
      `重点标的覆盖 ${completeText}`,
      `缺失重点标的 ${missingCodes == null ? "待确认" : (missingCodes.join("、") || "无")}`,
      `重点标的读取异常 ${priorityErrors == null ? "待确认" : (priorityErrors.join("；") || "无")}`,
      "逐笔大单数据暂未接入",
    ];
    if (!historical && state.last_error) qualityParts.push(`最近错误 ${state.last_error}`);
    $("qw-quality").innerHTML = qualityParts.map((item) => `<span>${esc(item)}</span>`).join(" · ");
    $("qw-meta").textContent = `${effectiveDate || "日期待确认"} · ${latest.scanned_at || ""} · ${finiteNumberText(latest.window_minutes, { digits: 0 })}分钟窗口 · ${latest.manual ? "手动诊断" : "自动扫描"}${historical ? " · 历史数据" : ""}`;
    $("qw-latest").innerHTML = [
      qwStockTable("评分达标候选", latest.qualified, 20),
      qwStockTable("关注 / 持仓 / 近期选股异动", latest.priority_alerts, 30),
      qwStockTable("窗口成交额前十", latest.top_window_amount, 10),
      qwStockTable("涨速前十", latest.top_speed, 10),
      qwStockTable("形态突破", latest.breakouts, 20),
      qwStockTable("异常上涨 / 下跌", latest.anomalies, 20),
      qwStockTable("当日总成交额前二十", latest.top_total_amount, 20),
    ].join("");
  } else {
    $("qw-quality").textContent = (!historical && state.last_error) ? `最近错误：${state.last_error}` : `${effectiveDate || "所选日期"}暂无扫描结果`;
    $("qw-meta").textContent = effectiveDate ? `${effectiveDate} · 暂无数据` : "";
    $("qw-latest").innerHTML = '<div class="empty">所选日期没有可展示的聚合消息；切换日期或点击「最新」查看最近有数据日。</div>';
  }
  renderQuantWatchSectors(latest);
  const history = (data.messages || []).slice(latest ? 1 : 0);
  $("qw-history").innerHTML = history.length ? history.map((item) => {
    const payload = item.payload || {};
    const s = payload.market_summary || {};
    return `<details class="qw-history-item"><summary><b>${esc(item.scanned_at || "—")}</b><span>扫描 ${finiteNumberText(s.scanned_count, { digits: 0 })} · 达标 ${finiteNumberText(s.qualified_count, { digits: 0 })} · 优先异动 ${finiteNumberText(s.priority_alert_count, { digits: 0 })}</span></summary><div>${qwStockTable("当轮达标", payload.qualified, 12)}${qwStockTable("当轮优先异动", payload.priority_alerts, 12)}</div></details>`;
  }).join("") : '<div class="empty">所选日期暂无更早消息</div>';
}

function renderQuantWatchLoadFailure(error) {
  const reason = error?.message || "未知错误";
  stopQuantWatchSocket();
  $("qw-kpi-status").textContent = "加载失败";
  $("qw-kpi-time").textContent = "陈旧";
  $("qw-kpi-count").textContent = "—";
  $("qw-kpi-signal").textContent = "—";
  $("qw-quality").textContent = `加载失败，原质量状态已作废：${reason}`;
  $("qw-meta").textContent = "数据陈旧 · 请刷新重试";
  $("qw-latest").innerHTML = `<div class="empty">最新扫描加载失败，旧结果不再视为当前：${esc(reason)}</div>`;
  $("qw-sectors").innerHTML = `<div class="empty">行业轮动加载失败，旧结果已标记为陈旧：${esc(reason)}</div>`;
  $("qw-history").innerHTML = `<div class="empty">历史消息加载失败，旧记录已标记为陈旧：${esc(reason)}</div>`;
}

async function connectQuantWatchSocket() {
  if (!quantWatchShouldConnect()) return;
  stopQuantWatchSocket();
  const generation = _qwSocketGeneration;
  const badge = $("qw-connection");
  badge.textContent = "连接中"; badge.className = "qw-connection connecting";
  try {
    const ticketData = await adminApi("/quant-watch/ticket", "POST", {});
    if (generation !== _qwSocketGeneration || !quantWatchShouldConnect()) return;
    const url = new URL(cfg.base.replace(/\/$/, ""));
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `${url.pathname.replace(/\/$/, "")}/ws/quant-watch`;
    url.search = `ticket=${encodeURIComponent(ticketData.ticket)}`;
    const socket = new WebSocket(url.toString());
    _qwSocket = socket;
    socket.onopen = () => {
      if (socket !== _qwSocket) return;
      if (!quantWatchShouldConnect()) { stopQuantWatchSocket(); return; }
      badge.textContent = "实时连接"; badge.className = "qw-connection on";
    };
    socket.onmessage = (event) => {
      if (socket !== _qwSocket || _qwRequestedTradeDate) return;
      try { const message = JSON.parse(event.data); if (message.data) renderQuantWatch(message.data); } catch (error) { /* 忽略单条格式异常 */ }
    };
    socket.onerror = () => { if (socket === _qwSocket) { badge.textContent = "连接异常"; badge.className = "qw-connection bad"; } };
    socket.onclose = () => {
      if (socket !== _qwSocket) return;
      _qwSocket = null;
      badge.textContent = "已断开"; badge.className = "qw-connection off";
      if (quantWatchShouldConnect()) _qwReconnectTimer = setTimeout(connectQuantWatchSocket, 3000);
    };
  } catch (error) {
    if (generation !== _qwSocketGeneration || !quantWatchShouldConnect()) return;
    badge.textContent = "连接失败"; badge.className = "qw-connection bad";
    _qwReconnectTimer = setTimeout(connectQuantWatchSocket, 5000);
  }
}

async function loadQuantWatch(force = false) {
  if (!isAdmin()) return;
  const sequence = ++_qwLoadSequence;
  const refreshButton = $("qw-refresh");
  if (force) { refreshButton.disabled = true; refreshButton.textContent = "刷新中…"; }
  try {
    const params = { limit: 100 };
    if (_qwRequestedTradeDate) params.trade_date = _qwRequestedTradeDate;
    const data = await call("quant_watch_status", params);
    if (sequence !== _qwLoadSequence) return;
    renderQuantWatch(data, force || !_qwConfigLoaded);
    if (quantWatchShouldConnect()) {
      if (!_qwSocket || _qwSocket.readyState > WebSocket.OPEN) connectQuantWatchSocket();
    } else {
      stopQuantWatchSocket();
    }
    if (force) toast(data.is_historical ? "历史盯盘数据已更新" : "盯盘状态已更新", "ok");
  } catch (error) {
    if (sequence === _qwLoadSequence) renderQuantWatchLoadFailure(error);
  } finally {
    if (force && sequence === _qwLoadSequence) { refreshButton.disabled = false; refreshButton.textContent = "刷新"; }
  }
}

async function saveQuantWatchConfig() {
  const boards = [...document.querySelectorAll('#qw-boards input[type="checkbox"]:checked')].map((input) => input.value);
  const channels = [["qw-feishu", "feishu"], ["qw-wecom", "wecom"]].filter(([id]) => $(id).checked).map(([, name]) => name);
  const config = {
    enabled: $("qw-enabled").checked,
    universe_mode: $("qw-mode").value,
    interval_seconds: Number($("qw-interval").value),
    window_minutes: Number($("qw-window").value),
    qualified_score: Number($("qw-threshold").value),
    boards,
    industries: splitTerms($("qw-industries").value),
    themes: splitTerms($("qw-themes").value),
    notify_enabled: $("qw-notify").checked,
    notify_channels: channels,
  };
  const status = $("qw-setting-status");
  status.textContent = "保存中…"; status.className = "status";
  try {
    const data = await call("quant_watch_set_config", { config, reason: "前端量化盯盘设置" });
    _qwConfigLoaded = false;
    fillQuantWatchConfig(data, true);
    status.textContent = data.changed ? "设置已保存并生效" : "设置无变化";
    status.className = "status ok";
    await loadQuantWatch(true);
  } catch (error) {
    status.textContent = `保存失败：${error.message}`; status.className = "status bad";
  }
}

$("qw-date").onchange = () => {
  _qwRequestedTradeDate = String($("qw-date").value || "").replaceAll("-", "");
  stopQuantWatchSocket();
  loadQuantWatch(true);
};
$("qw-latest-date").onclick = () => {
  _qwRequestedTradeDate = "";
  loadQuantWatch(true);
};
$("qw-refresh").onclick = () => loadQuantWatch(true);
$("qw-save").onclick = saveQuantWatchConfig;
$("qw-scan").onclick = async () => {
  const button = $("qw-scan");
  button.disabled = true; button.textContent = "扫描中…";
  try {
    await call("quant_watch_scan_once", {});
    _qwRequestedTradeDate = "";
    await loadQuantWatch(true);
    toast("手动扫描完成", "ok");
  }
  catch (error) { toast("扫描失败：" + error.message, "bad"); }
  finally { button.disabled = false; button.textContent = "立即扫描"; }
};
window.addEventListener("pagehide", stopQuantWatchSocket);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") {
    stopQuantWatchSocket();
    return;
  }
  if (quantWatchShouldConnect()) loadQuantWatch(true);
});
window.addEventListener("beforeunload", stopQuantWatchSocket);

/* 首屏：没有本地 Key 时必须先登录；已有 Key 仍需重新向服务端验证 */
applyRoleUI();
if (!cfg.key) {
  openLogin("请输入服务 Key 后继续");
} else {
  refreshRole();
}
