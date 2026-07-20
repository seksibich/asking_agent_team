"""运行监控事件、告警分级与日志生命周期测试。"""
from __future__ import annotations

import gzip
import tempfile
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
import observability  # noqa: E402


class ObservabilityTest(unittest.TestCase):
    def test接口与盯盘事件可聚合为中文日报(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 7, 19, 22, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
            log_root = Path(directory) / "logs" / "monitor"
            with patch.object(observability, "LOG_ROOT", log_root), \
                    patch.object(observability.common, "DATA_DIR", Path(directory)), \
                    patch.object(observability, "_now", return_value=now):
                observability.record_http("/call", "screen_quant", 200, 120.0)
                observability.record_http("/call", "screen_quant", 500, 300.0)
                observability.record_quant_watch(
                    "success", 800.0, manual=False,
                    payload={"market_summary": {
                        "scanned_count": 100, "qualified_count": 3,
                        "priority_alert_count": 1,
                    }})
                result = observability.build_daily_summary("20260719")
                summary = (log_root / "daily" / "20260719.md").read_text(encoding="utf-8")
        self.assertEqual(result["http"]["count"], 2)
        self.assertEqual(result["http"]["failures"], 1)
        self.assertEqual(result["http"]["rejections"], 0)
        self.assertEqual(result["quant_watch"]["avg_scanned"], 100.0)
        self.assertEqual(result["status"], "需关注")
        self.assertIn("业务拒绝", summary)
        self.assertIn("日志生命周期与容量", summary)

    def test普通四百状态只计业务拒绝(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 7, 19, 22, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
            log_root = Path(directory) / "logs" / "monitor"
            with patch.object(observability, "LOG_ROOT", log_root), \
                    patch.object(observability.common, "DATA_DIR", Path(directory)), \
                    patch.object(observability, "_now", return_value=now):
                observability.record_http("/whoami", None, 401, 10.0)
                observability.record_http("/missing", None, 404, 12.0)
                result = observability.build_daily_summary("20260719")
        self.assertEqual(result["http"]["rejections"], 2)
        self.assertEqual(result["http"]["failures"], 0)
        self.assertEqual(result["status"], "正常")

    def test旧日志按配置压缩和删除(self):
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            logs_root = data_root / "logs"
            monitor_root = logs_root / "monitor"
            compress_file = logs_root / "selection" / "2026" / "07" / "10.jsonl"
            delete_file = logs_root / "api" / "2026" / "01" / "01.jsonl"
            compress_file.parent.mkdir(parents=True)
            delete_file.parent.mkdir(parents=True)
            compress_file.write_text('{"event":"保留并压缩"}\n', encoding="utf-8")
            delete_file.write_text('{"event":"过期删除"}\n', encoding="utf-8")
            environment = {
                "MONITOR_LOG_CLEANUP_ENABLED": "true",
                "MONITOR_LOG_RETENTION_DAYS": "90",
                "MONITOR_LOG_COMPRESS_AFTER_DAYS": "7",
            }
            with patch.object(observability, "LOG_ROOT", monitor_root), \
                    patch.object(observability.common, "DATA_DIR", data_root), \
                    patch.dict(observability.os.environ, environment, clear=False):
                result = observability.maintain_logs("20260719")
            compressed = Path(f"{compress_file}.gz")
            self.assertFalse(delete_file.exists())
            self.assertFalse(compress_file.exists())
            self.assertTrue(compressed.exists())
            with gzip.open(compressed, "rt", encoding="utf-8") as handle:
                self.assertIn("保留并压缩", handle.read())
        self.assertEqual(result["deleted_files"], 1)
        self.assertEqual(result["compressed_files"], 1)

    def test非法日期被拒绝(self):
        for value in ("2026/07/19", "20260231"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                observability.build_daily_summary(value)


if __name__ == "__main__":
    unittest.main()
