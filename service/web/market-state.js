/* 上海市场时段与前端请求策略；浏览器和 Node 自动化测试共用。 */
(function initMarketState(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.MarketState = api;
}(typeof globalThis !== "undefined" ? globalThis : this, () => {
  const PHASE_LABELS = Object.freeze({
    preopen: "盘前", call_auction: "集合竞价", morning: "上午交易",
    lunch: "午间休市", afternoon: "下午交易", closed_pending: "收盘待确认",
    final: "日终数据已完成", non_trading_day: "非交易日",
  });
  const CONTINUOUS_PHASES = new Set(["morning", "afternoon"]);

  const compactDate = (value) => {
    const text = String(value || "").replaceAll("-", "");
    return /^\d{8}$/.test(text) ? text : "";
  };

  function shanghaiToday(now = new Date()) {
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(now);
    const values = Object.fromEntries(parts.map((item) => [item.type, item.value]));
    return `${values.year}${values.month}${values.day}`;
  }

  function context(health = {}, now = new Date()) {
    const phase = String(health.market_phase || "");
    const serverDate = compactDate(health.date) || shanghaiToday(now);
    const tradingDay = health.is_trading_day ?? health.trade_open;
    const continuous = health.is_continuous_trading ?? CONTINUOUS_PHASES.has(phase);
    const latestTradeDate = compactDate(health.last_calendar_trade_date)
      || compactDate(health.last_data_ready_date) || serverDate;
    return {
      phase, phaseLabel: PHASE_LABELS[phase] || "市场状态待确认", serverDate,
      isTradingDay: tradingDay === true, isContinuousTrading: continuous === true,
      lastDataReadyDate: compactDate(health.last_data_ready_date), latestTradeDate,
      maxSelectableDate: tradingDay === true ? serverDate : latestTradeDate,
    };
  }

  function industryRequest(mode, date, health = {}) {
    const state = context(health);
    const requestedDate = compactDate(date);
    if (mode === "intraday" && !state.isContinuousTrading) {
      return {
        mode: "latest_complete", date: "",
        notice: `当前为${state.phaseLabel}，已展示最近完整交易日数据`, state,
      };
    }
    if (mode === "intraday") {
      return { mode, date: state.serverDate, notice: "", state };
    }
    if (mode === "latest_complete") return { mode, date: "", notice: "", state };
    return {
      mode: "historical",
      date: requestedDate && requestedDate <= state.maxSelectableDate
        ? requestedDate : state.maxSelectableDate,
      notice: requestedDate > state.maxSelectableDate ? "查看日期已调整为最近可用交易日" : "",
      state,
    };
  }

  function sentimentDefault(health = {}) {
    const state = context(health);
    return {
      date: state.isTradingDay ? state.serverDate : state.latestTradeDate,
      maxDate: state.maxSelectableDate,
      phaseLabel: state.phaseLabel,
      isProvisional: state.isTradingDay && state.phase !== "final",
    };
  }

  return Object.freeze({
    PHASE_LABELS, compactDate, shanghaiToday, context, industryRequest, sentimentDefault,
  });
}));
