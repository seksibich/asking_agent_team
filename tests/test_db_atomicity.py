"""关键业务幂等与并发原子性测试。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
import db  # noqa: E402


class SelectionAtomicityTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_url = os.environ.get("DB_URL")
        os.environ["DB_URL"] = f"sqlite:///{Path(self.tempdir.name) / 'test.db'}"
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db.init_db()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        if self.old_url is None:
            os.environ.pop("DB_URL", None)
        else:
            os.environ["DB_URL"] = self.old_url
        self.tempdir.cleanup()

    @staticmethod
    def record(reason: str = "首次理由"):
        return {
            "sel_date": date(2026, 7, 17), "code": "600000.SH", "name": "浦发银行",
            "score": 0.8, "driver": "逻辑", "reason": reason,
            "category": "auto", "extra": {"screening_run_id": "run-test"},
            "logged_at": datetime(2026, 7, 17, 22, 0, 0),
        }

    def test并发重复登记只保留一条且首次内容不可变(self):
        values = [self.record(f"并发理由{i}") for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(db.upsert_selection, values))
        rows = db.fetch_selections(category="auto")
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(bool(item["inserted"]) for item in results), 1)
        self.assertIn(rows[0]["reason"], {item["reason"] for item in values})

    def test重复登记不覆盖首次快照(self):
        first = db.upsert_selection(self.record("原始理由"))
        second = db.upsert_selection(self.record("后续理由"))
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(second["record"]["reason"], "原始理由")

    def test动态访客Key只保存摘要且启停删除立即生效(self):
        raw_key = "sk-stockagent-user-" + "a" * 40
        created = db.create_user_api_key("key-test", "测试访客", raw_key)
        self.assertTrue(db.verify_user_api_key(raw_key))
        listed = db.list_user_api_keys()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("key", listed[0])
        self.assertNotIn("key_hash", listed[0])
        self.assertIn("masked_key", listed[0])
        self.assertTrue(db.toggle_user_api_key(created["id"]))
        self.assertFalse(db.verify_user_api_key(raw_key))
        self.assertTrue(db.delete_user_api_key(created["id"]))
        self.assertEqual(db.list_user_api_keys(), [])

    def test旧明文访客Key迁移后删除配置值(self):
        raw_key = "sk-stockagent-user-" + "b" * 40
        db.set_config("user_api_keys", {"keys": [{
            "id": "legacy", "label": "旧访客", "key": raw_key,
            "created_at": "2026-07-17 12:00:00", "disabled": False,
        }]})
        db.init_db()
        self.assertTrue(db.verify_user_api_key(raw_key))
        self.assertIsNone(db.get_config("user_api_keys"))

    def test筛选契约与运行任一步失败时整体回滚(self):
        contract = {
            "schema_hash": "c" * 64, "model": "stock", "factor_version": "v-test",
            "components": [], "definition": {},
        }
        record = {
            "run_id": "run-bad", "function_name": None, "trade_date": "20260717",
            "factor_version": "v-test", "schema_hash": "c" * 64,
            "weight_version": "w-test", "contract": contract,
            "candidate_codes": [], "candidates": [], "params": {},
        }
        with self.assertRaises(Exception):
            db.save_screening_snapshot(contract, record)
        self.assertIsNone(db.get_factor_contract("c" * 64))


if __name__ == "__main__":
    unittest.main()
