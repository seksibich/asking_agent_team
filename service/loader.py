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
LIB_ONLY = {"factors"}


def _scripts_dirs() -> list[Path]:
    dirs = [SERVICE_DIR]
    if SKILLS_DIR.exists():
        for sub in sorted(SKILLS_DIR.iterdir()):
            sc = sub / "scripts"
            if sc.is_dir():
                dirs.append(sc)
    return dirs


def discover() -> list[str]:
    """将所有 scripts 目录加入 sys.path 并导入功能模块。返回已导入模块名。"""
    dirs = _scripts_dirs()
    for d in dirs:
        p = str(d)
        if p not in sys.path:
            sys.path.insert(0, p)

    imported: list[str] = []
    for d in dirs:
        for py in sorted(d.glob("*.py")):
            mod = py.stem
            if mod.startswith("_") or mod in {"app", "registry", "loader", "common", "cli", "db", "version"}:
                continue
            if mod in LIB_ONLY:
                continue
            try:
                importlib.import_module(mod)
                imported.append(mod)
            except Exception as e:  # noqa: BLE001
                print(f"[loader] skip {mod}: {e}", file=sys.stderr)
    return imported
