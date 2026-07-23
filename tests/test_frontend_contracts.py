"""静态前端与后端功能契约自动检查。"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

import loader  # noqa: E402
import registry  # noqa: E402


class FrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "service/web/app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "service/web/index.html").read_text(encoding="utf-8")
        loader.discover()

    def test前端引用的元素全部存在(self):
        references = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', self.javascript))
        ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', self.html))
        self.assertEqual(sorted(references - ids), [])

    def test页面元素编号不重复(self):
        ids = re.findall(r'\bid="([A-Za-z0-9_-]+)"', self.html)
        duplicates = sorted(key for key in set(ids) if ids.count(key) > 1)
        self.assertEqual(duplicates, [])

    def test前端调用功能均已注册(self):
        calls = set(re.findall(r'call\("([A-Za-z0-9_-]+)"', self.javascript))
        self.assertEqual(sorted(calls - set(registry.names())), [])

    def test主要业务入口均有前端调用(self):
        calls = set(re.findall(r'call\("([A-Za-z0-9_-]+)"', self.javascript))
        expected = {
            "screen_quant", "screen_sector", "selection_dashboard",
            "portfolio_get", "portfolio_stock_search", "portfolio_upload",
            "sentiment_temperature", "market_timing", "sentiment_extreme_index",
            "selection_backtest", "predictions_backtest", "precompute_status",
            "quant_watch_status",
        }
        self.assertEqual(sorted(expected - calls), [])

    def test行情刷新不触发整页重载(self):
        start = self.javascript.index("async function refreshSelectionQuotes")
        end = self.javascript.index('$("sl-run").onclick', start)
        body = self.javascript[start:end]
        self.assertIn('/selections/quotes', body)
        self.assertNotIn('location.reload', body)
        self.assertNotIn('selection_dashboard', body)
        self.assertIn('sl-refresh-progress', body)

    def test访问凭据缓存到本地便于移动端复用(self):
        # 为满足移动端浏览器（如 Safari）免重复登录的需求，访问凭据缓存到 localStorage；
        # 同时保留 sessionStorage 写入以兼容旧逻辑，并提供 forgetConnection 清除入口。
        self.assertIn('localStorage.setItem(LS.key', self.javascript)
        self.assertIn('sessionStorage.setItem(SS.key', self.javascript)
        self.assertIn('function forgetConnection', self.javascript)

    def test访客Key列表不提供明文复制(self):
        self.assertNotIn('data-uk-copy', self.javascript)
        self.assertIn('masked_key', self.javascript)
        self.assertIn('完整 Key 只显示这一次', self.html)
