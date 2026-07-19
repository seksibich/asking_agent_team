"""上海市场时钟全时段自动化测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
import common  # noqa: E402


class _Calendar:
    def __init__(self, open_days: list[str]):
        self.open_days = open_days

    def trade_cal(self, **_kwargs):
        days = pd.date_range("2026-07-13", "2026-07-20", freq="D")
        return pd.DataFrame({
            "cal_date": [day.strftime("%Y%m%d") for day in days],
            "is_open": [int(day.strftime("%Y%m%d") in self.open_days) for day in days],
        })


class MarketClockTest(unittest.TestCase):
    OPEN_DAYS = ["20260713", "20260714", "20260715", "20260716", "20260717", "20260720"]

    def clock(self, value: str):
        now = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch.object(common, "get_pro", return_value=_Calendar(self.OPEN_DAYS)):
            with patch.object(common, "_final_ready_time", return_value=datetime.strptime("16:00", "%H:%M").time()):
                return common.market_clock(now)

    def test交易日七类关键阶段(self):
        cases = [
            ("2026-07-17 08:30:00", "preopen", False, "20260716"),
            ("2026-07-17 09:20:00", "call_auction", False, "20260716"),
            ("2026-07-17 10:00:00", "morning", True, "20260716"),
            ("2026-07-17 12:00:00", "lunch", False, "20260716"),
            ("2026-07-17 14:00:00", "afternoon", True, "20260716"),
            ("2026-07-17 15:30:00", "closed_pending", False, "20260716"),
            ("2026-07-17 16:01:00", "final", False, "20260717"),
        ]
        for text, phase, continuous, ready in cases:
            with self.subTest(phase=phase):
                value = self.clock(text)
                self.assertEqual(value["phase"], phase)
                self.assertEqual(value["is_continuous_trading"], continuous)
                self.assertEqual(value["last_data_ready_date"], ready)

    def test非交易日使用最近交易日且不进入连续竞价(self):
        value = self.clock("2026-07-19 10:00:00")
        self.assertEqual(value["phase"], "non_trading_day")
        self.assertFalse(value["is_trading_day"])
        self.assertFalse(value["is_continuous_trading"])
        self.assertEqual(value["last_calendar_trade_date"], "20260717")
        self.assertEqual(value["last_data_ready_date"], "20260717")
