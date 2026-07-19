#!/usr/bin/env python3
"""正式环境服务探针与每日中文监控汇总触发器。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _base_url() -> str:
    port = str(os.getenv("PORT", "18901")).strip() or "18901"
    return str(os.getenv("MONITOR_BASE_URL", f"http://127.0.0.1:{port}")).rstrip("/")


def _request(path: str, *, api_key: str = "", timeout: float = 8.0) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(_base_url() + path, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"error": raw[:300]}
        return int(exc.code), body


def probe() -> int:
    results = {}
    for path in ("/live", "/ready"):
        try:
            status, body = _request(path, timeout=5.0)
            results[path] = {"status": status, "state": body.get("status")}
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            results[path] = {"status": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}
    ready = results.get("/live", {}).get("status") == 200 and results.get("/ready", {}).get("status") == 200
    print(json.dumps({"time": datetime.now().isoformat(timespec="seconds"),
                      "ready": ready, "checks": results}, ensure_ascii=False))
    return 0 if ready else 1


def daily_summary() -> int:
    api_key = str(os.getenv("API_KEY") or os.getenv("ADMIN_API_KEY") or "").strip()
    if not api_key:
        print("未配置 API_KEY 或 ADMIN_API_KEY，无法生成管理员监控汇总", file=sys.stderr)
        return 2
    try:
        status, body = _request("/admin/monitor/daily", api_key=api_key, timeout=30.0)
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"每日监控汇总请求失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"每日监控汇总返回异常状态：{status}", file=sys.stderr)
        return 1
    summary = body.get("summary") or {}
    print(json.dumps({
        "time": datetime.now().isoformat(timespec="seconds"),
        "status": summary.get("status"),
        "date": summary.get("date"),
        "summary_file": summary.get("summary_file"),
    }, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="股票数据服务正式环境监控")
    parser.add_argument("action", choices=("probe", "daily"))
    args = parser.parse_args()
    return probe() if args.action == "probe" else daily_summary()


if __name__ == "__main__":
    raise SystemExit(main())