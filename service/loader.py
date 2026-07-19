"""功能模块自动发现与加载。

扫描 agent/skills/*/scripts/ 与 service/ 下的功能模块并 import，触发 @register。
所有 scripts 目录都会加入 sys.path，因此模块间可用扁平方式互相 import
（如 quant_screen 可 `import factors`，research_report 可 `import price_hike`）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

# 镜像内工程根：/app（COPY 整个工程到 /app）
# 目录重组后，技能脚本位于 agent/skills/*/scripts（agent 相关内容统一收拢到 agent/ 下）
APP_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = APP_ROOT / "agent" / "skills"
SERVICE_DIR = APP_ROOT / "service"

# 不作为功能模块导入的纯库（仍会因在 sys.path 上而可被 import）
LIB_ONLY = {"factors", "eod_fallback"}
_LAST_REPORT: dict[str, object] = {"imported": [], "errors": []}


def _scripts_dirs() -> list[Path]:
    dirs = [SERVICE_DIR]
    if SKILLS_DIR.exists():
        for sub in sorted(SKILLS_DIR.iterdir()):
            sc = sub / "scripts"
            if sc.is_dir():
                dirs.append(sc)
    return dirs


def discover() -> list[str]:
    """导入功能模块并保存完整装载报告，供就绪探针判断部分失败。"""
    global _LAST_REPORT
    dirs = _scripts_dirs()
    for directory in dirs:
        path_text = str(directory)
        if path_text not in sys.path:
            sys.path.insert(0, path_text)

    imported: list[str] = []
    errors: list[dict[str, str]] = []
    for directory in dirs:
        for py in sorted(directory.glob("*.py")):
            mod = py.stem
            if mod.startswith("_") or mod in {
                    "app", "registry", "loader", "common", "cli", "db", "version",
                    "daily_scheduler", "observability"}:
                continue
            if mod in LIB_ONLY:
                continue
            try:
                importlib.import_module(mod)
                imported.append(mod)
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"[:500]
                errors.append({"module": mod, "path": str(py), "error": message})
                print(f"[loader] skip {mod}: {message}", file=sys.stderr)
    _LAST_REPORT = {"imported": imported.copy(), "errors": errors}
    return imported


def report() -> dict[str, object]:
    """返回最近一次发现结果的副本，不暴露可变内部列表。"""
    return {
        "imported": list(_LAST_REPORT.get("imported") or []),
        "errors": [dict(item) for item in (_LAST_REPORT.get("errors") or [])],
    }
