"""本地调试 CLI：不启服务直接调用功能。

用法：
    python cli.py functions
    python cli.py call screen_sector '{"top_n":10}'
    python cli.py call price_hike_scan '{}'
"""
from __future__ import annotations

import json
import sys

import loader
import registry


def main() -> None:
    try:
        import db
        db.init_db()
    except Exception as e:  # noqa: BLE001
        print(f"[cli] db init skipped: {e}")
    loader.discover()
    if len(sys.argv) < 2:
        print("usage: cli.py functions | call <name> [json_params]")
        return
    cmd = sys.argv[1]
    if cmd == "functions":
        print(json.dumps(registry.functions_index(), ensure_ascii=False, indent=2))
    elif cmd == "call":
        name = sys.argv[2]
        params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        print(json.dumps(registry.call(name, params), ensure_ascii=False, indent=2))
    else:
        print(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
