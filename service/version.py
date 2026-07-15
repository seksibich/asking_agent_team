"""版本信息：供 /health 暴露 agent 文档版本号与部署 git 版本，实现 agent↔服务版本对齐。

- git_revision：本次部署的 git commit 短 sha。优先级：
    环境变量 GIT_REVISION > 仓库根 VERSION 文件 > 运行时 `git rev-parse --short HEAD` > "unknown"
  （部署脚本 remote_deploy.sh 会把当前 commit 写入 VERSION 文件，避免运行环境无 .git 时取不到。）
- agent_doc_version：从 agent/init.md 顶部解析的 AGENT_DOC_VERSION（语义版本）。

两者均在进程启动后不变（重新部署会重启进程刷新），故缓存一次即可。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

# 仓库根 = service/ 的上一级
REPO_ROOT = Path(__file__).resolve().parent.parent
INIT_MD = REPO_ROOT / "agent" / "init.md"
VERSION_FILE = REPO_ROOT / "VERSION"

_git_rev_cache: Optional[str] = None
_doc_ver_cache: Optional[str] = None


def git_revision() -> str:
    global _git_rev_cache
    if _git_rev_cache is not None:
        return _git_rev_cache
    rev = (os.getenv("GIT_REVISION") or "").strip()
    if not rev and VERSION_FILE.exists():
        try:
            rev = VERSION_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            rev = ""
    if not rev:
        try:
            rev = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(REPO_ROOT), stderr=subprocess.DEVNULL, timeout=5,
            ).decode("utf-8").strip()
        except Exception:
            rev = ""
    _git_rev_cache = rev or "unknown"
    return _git_rev_cache


def agent_doc_version() -> str:
    """从 agent/init.md 解析 AGENT_DOC_VERSION（形如 v1.4.0）。"""
    global _doc_ver_cache
    if _doc_ver_cache is not None:
        return _doc_ver_cache
    ver = ""
    try:
        text = INIT_MD.read_text(encoding="utf-8")
        # 匹配：AGENT_DOC_VERSION：`v1.4.0`（中英文冒号、可选加粗/空白）
        m = re.search(r"AGENT_DOC_VERSION[：:]\s*`?(v\d+\.\d+\.\d+)`?", text)
        if m:
            ver = m.group(1)
    except Exception:
        ver = ""
    _doc_ver_cache = ver or "unknown"
    return _doc_ver_cache
