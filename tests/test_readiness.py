"""存活与流量就绪探针测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
import app  # noqa: E402


class ReadinessTest(unittest.TestCase):
    def test全部依赖正常时就绪(self):
        health = {"db_ready": True, "tushare_ready": True,
                  "functions": 3, "market_phase": "final"}
        with patch.object(app, "_health_snapshot", return_value=health), \
                patch.object(app.loader, "report", return_value={"imported": ["a"], "errors": []}):
            body, status = app._readiness_snapshot()
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")
        self.assertTrue(all(body["checks"].values()))

    def test数据库或模块装载失败时拒绝流量(self):
        health = {"db_ready": False, "tushare_ready": True,
                  "functions": 3, "market_phase": "final"}
        report = {"imported": ["a"], "errors": [{"module": "broken"}]}
        with patch.object(app, "_health_snapshot", return_value=health), \
                patch.object(app.loader, "report", return_value=report):
            body, status = app._readiness_snapshot()
        self.assertEqual(status, 503)
        self.assertFalse(body["checks"]["database"])
        self.assertFalse(body["checks"]["module_load"])
        self.assertEqual(body["load_error_modules"], ["broken"])

    def test存活探针不调用依赖探测(self):
        with patch.object(app, "_health_snapshot", side_effect=AssertionError("不应调用")):
            response = app.live()
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
