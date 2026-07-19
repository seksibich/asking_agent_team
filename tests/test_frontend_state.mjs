import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const MarketState = require("../service/web/market-state.js");

const base = {
  date: "20260717", is_trading_day: true,
  last_calendar_trade_date: "20260717", last_data_ready_date: "20260716",
};

for (const [phase, continuous] of [
  ["preopen", false], ["call_auction", false], ["morning", true],
  ["lunch", false], ["afternoon", true], ["closed_pending", false], ["final", false],
]) {
  test(`前端正确识别 ${phase}`, () => {
    const value = MarketState.context({ ...base, market_phase: phase, is_continuous_trading: continuous });
    assert.equal(value.isContinuousTrading, continuous);
    assert.ok(value.phaseLabel);
  });
}

test("非交易日最多选择最近交易日", () => {
  const value = MarketState.context({
    date: "20260719", is_trading_day: false, market_phase: "non_trading_day",
    last_calendar_trade_date: "20260717", last_data_ready_date: "20260717",
  });
  assert.equal(value.maxSelectableDate, "20260717");
  assert.equal(value.phaseLabel, "非交易日");
});

test("盘中行业请求只在连续交易时段启用", () => {
  const morning = MarketState.industryRequest("intraday", "20260717", { ...base, market_phase: "morning" });
  assert.equal(morning.mode, "intraday");
  const lunch = MarketState.industryRequest("intraday", "20260717", { ...base, market_phase: "lunch" });
  assert.equal(lunch.mode, "latest_complete");
  assert.match(lunch.notice, /午间休市/);
});


test("情绪日期在交易日使用当天并标记临时数据", () => {
  const value = MarketState.sentimentDefault({ ...base, market_phase: "morning" });
  assert.equal(value.date, "20260717");
  assert.equal(value.maxDate, "20260717");
  assert.equal(value.isProvisional, true);
});

test("情绪日期在非交易日回到最近交易日", () => {
  const value = MarketState.sentimentDefault({
    date: "20260719", is_trading_day: false, market_phase: "non_trading_day",
    last_calendar_trade_date: "20260717", last_data_ready_date: "20260717",
  });
  assert.equal(value.date, "20260717");
  assert.equal(value.maxDate, "20260717");
  assert.equal(value.isProvisional, false);
});

test("指定行业历史日期不能越过可选上限", () => {
  const value = MarketState.industryRequest("historical", "20260720", base);
  assert.equal(value.date, "20260717");
  assert.match(value.notice, /最近可用交易日/);
});


for (const phase of ["preopen", "call_auction", "morning", "lunch", "afternoon", "closed_pending", "final"]) {
  test(`全时段请求矩阵 ${phase}`, () => {
    const health = { ...base, market_phase: phase,
      is_continuous_trading: ["morning", "afternoon"].includes(phase) };
    const industry = MarketState.industryRequest("intraday", "20260717", health);
    assert.equal(industry.mode, ["morning", "afternoon"].includes(phase) ? "intraday" : "latest_complete");
    const sentiment = MarketState.sentimentDefault(health);
    assert.equal(sentiment.date, "20260717");
    assert.equal(sentiment.isProvisional, phase !== "final");
  });
}