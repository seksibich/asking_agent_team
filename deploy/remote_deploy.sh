#!/usr/bin/env bash
# =============================================================================
# 服务器端部署脚本（阿里云 ECS / Alibaba Cloud Linux）
#
# 由本地 deploy/local_deploy.sh 通过 SSH 触发，也可在服务器上手动执行。
# 流程：拉取最新代码 → 准备 venv/依赖 → 检查并同步 MySQL 结构 → 释放端口
#       → 安装/更新 systemd 单元 → 重启服务 → 健康检查 → 打印状态与日志。
#
# 首次使用前提（服务器上一次性准备）：
#   1) 安装 git、python3.11+、python3-venv
#   2) git clone <仓库> 到 $APP_DIR（默认 /root/asking_agent_team）
#   3) cp profile/.env.example .env 并填好 TUSHARE_TOKEN / API_KEY / DB_URL 等
#
# 可用环境变量覆盖默认值：
#   APP_DIR  BRANCH  PORT  SERVICE_NAME  RUN_USER  VENV  INSTALL_DEPS
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/root/asking_agent_team}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-18901}"
SERVICE_NAME="${SERVICE_NAME:-stock-agent}"
RUN_USER="${RUN_USER:-root}"
VENV="${VENV:-$APP_DIR/.venv}"
INSTALL_DEPS="${INSTALL_DEPS:-1}"          # 1=每次同步依赖，0=跳过
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-40}"     # 健康检查最长等待秒数

log()  { echo -e "[$(date '+%F %T')] $*"; }
die()  { echo -e "[$(date '+%F %T')] [ERROR] $*" >&2; exit 1; }
step() { echo; log "===== $* ====="; }

trap 'die "部署在第 ${BASH_LINENO[0]} 行中断（命令: ${BASH_COMMAND}）"' ERR

[ -d "$APP_DIR/.git" ] || die "未找到 git 仓库: $APP_DIR（请先 git clone 到该目录）"
cd "$APP_DIR"

# ---------------------------------------------------------------------------
step "1/7 拉取最新代码（分支 $BRANCH）"
git fetch --prune origin
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"     # 与远端严格一致，避免本地脏改冲突
GIT_REV="$(git rev-parse --short HEAD)"
log "当前提交: $GIT_REV - $(git log -1 --pretty=%s)"
# 写入 VERSION 文件，供服务端 /health 暴露 git_revision（不依赖运行时 .git 可用）
echo "$GIT_REV" > "$APP_DIR/VERSION"
log "已写入 VERSION 文件: $GIT_REV"

# ---------------------------------------------------------------------------
step "2/7 准备 Python 虚拟环境与依赖"
if [ ! -x "$VENV/bin/python" ]; then
  log "创建虚拟环境: $VENV"
  python3 -m venv "$VENV"
fi
if [ "$INSTALL_DEPS" = "1" ]; then
  log "同步依赖 profile/requirements.txt"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$APP_DIR/profile/requirements.txt"
else
  log "跳过依赖安装（INSTALL_DEPS=0）"
fi

# ---------------------------------------------------------------------------
step "3/7 校验配置 .env"
[ -f "$APP_DIR/.env" ] || die "缺少 $APP_DIR/.env（从 profile/.env.example 复制并填写）"
set -a; . "$APP_DIR/.env"; set +a
export CACHE_DIR="${CACHE_DIR:-$APP_DIR/cache}"
export DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
mkdir -p "$CACHE_DIR" "$DATA_DIR"
if [ -n "${DB_URL:-}" ]; then log "数据库: RDS/MySQL（DB_URL 已配置）"; else log "数据库: 本地 SQLite（DB_URL 未设置）"; fi

# ---------------------------------------------------------------------------
step "4/7 检查并同步数据库结构"
# PYTHONPATH 指向 service/，保证 db_sync.py 能 import db / common（脚本本体在 deploy/）
PYTHONPATH="$APP_DIR/service" "$VENV/bin/python" "$APP_DIR/deploy/db_sync.py" \
  || die "数据库结构检查/同步失败，终止部署（不会重启旧服务上的新代码）"

# ---------------------------------------------------------------------------
step "5/7 停止旧服务并释放端口 $PORT"
if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
  log "停止 systemd 服务 $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME" || true
fi
# 兜底：杀掉仍占用端口的进程（优雅→强制）
release_port() {
  local pids=""
  if command -v fuser >/dev/null 2>&1; then
    pids=$(fuser -n tcp "$PORT" 2>/dev/null | tr -s ' ' || true)
  fi
  if [ -z "$pids" ] && command -v ss >/dev/null 2>&1; then
    pids=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u | tr '\n' ' ' || true)
  fi
  echo "$pids"
}
pids=$(release_port)
if [ -n "${pids// /}" ]; then
  log "端口 $PORT 仍被进程占用: $pids，尝试优雅结束"
  kill $pids 2>/dev/null || true
  sleep 3
  pids=$(release_port)
  if [ -n "${pids// /}" ]; then
    log "强制结束残留进程: $pids"
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
fi
pids=$(release_port)
[ -z "${pids// /}" ] && log "端口 $PORT 已释放 ✓" || die "端口 $PORT 仍被占用: $pids"

# ---------------------------------------------------------------------------
step "6/7 安装/更新 systemd 单元并启动"
UNIT_SRC="$APP_DIR/deploy/stock-agent.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
[ -f "$UNIT_SRC" ] || die "缺少 systemd 单元模板 $UNIT_SRC"
sed -e "s#__APP_DIR__#$APP_DIR#g" \
    -e "s#__VENV__#$VENV#g" \
    -e "s#__PORT__#$PORT#g" \
    -e "s#__RUN_USER__#$RUN_USER#g" \
    "$UNIT_SRC" > "$UNIT_DST"
log "已写入 $UNIT_DST"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"
log "systemd 服务已重启"

# ---------------------------------------------------------------------------
step "7/7 健康检查（最长 ${HEALTH_TIMEOUT}s）"
ok=0
for ((i=1; i<=HEALTH_TIMEOUT; i++)); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/tmp/stock_agent_health.json 2>/dev/null; then
    ok=1; break
  fi
  sleep 1
done
if [ "$ok" = "1" ]; then
  log "健康检查通过 ✓  /health => $(cat /tmp/stock_agent_health.json)"
else
  log "健康检查未通过，最近日志如下："
  journalctl -u "$SERVICE_NAME" -n 40 --no-pager || true
  die "服务未在 ${HEALTH_TIMEOUT}s 内就绪"
fi

echo
systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true
log "部署完成 ✓  外网访问: http://8.153.99.132:${PORT}/ui/"
