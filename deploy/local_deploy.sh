#!/usr/bin/env bash
# =============================================================================
# 本地一键部署：提交推送到 Git 仓库 -> 通过 SSH 通知阿里云 ECS 拉取并重启服务。
#
# 用法（在仓库根目录）：
#   bash deploy/local_deploy.sh                        # 用默认 IP/用户连接
#   SSH_HOST=aliyun-stock bash deploy/local_deploy.sh  # 用 ~/.ssh/config 别名连接
#   BRANCH=main bash deploy/local_deploy.sh            # 指定分支
#
# 前提：
#   - 本地已配置到服务器的 SSH（建议密钥免密）；
#   - 服务器已按 deploy/remote_deploy.sh 头部说明完成一次性准备（clone + .env）。
#
# 可用环境变量覆盖默认值：
#   SSH_HOST  SERVER_IP  SSH_USER  SSH_PORT  REMOTE_APP_DIR  BRANCH  SERVICE_NAME  PORT
# =============================================================================
set -euo pipefail

# SSH_HOST：若设置了 ~/.ssh/config 别名（如 aliyun-stock），优先按别名连接
SSH_HOST="${SSH_HOST:-}"
SERVER_IP="${SERVER_IP:-8.153.99.132}"
SSH_USER="${SSH_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/root/asking_agent_team}"
SERVICE_NAME="${SERVICE_NAME:-stock-agent}"
PORT="${PORT:-18901}"
BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

log() { echo "[$(date '+%F %T')] $*"; }
die() { echo "[$(date '+%F %T')] [ERROR] $*" >&2; exit 1; }

command -v git >/dev/null || die "未找到 git"
command -v ssh >/dev/null || die "未找到 ssh"

# 1) 本地提交状态检查
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "存在未提交改动，请先 git commit 后再部署（部署以远端仓库为准）"
fi

log "推送分支 $BRANCH 到 origin"
git push origin "$BRANCH"

# 2) SSH 触发远端部署
REMOTE_CMD="APP_DIR='$REMOTE_APP_DIR' BRANCH='$BRANCH' PORT='$PORT' SERVICE_NAME='$SERVICE_NAME' bash '$REMOTE_APP_DIR/deploy/remote_deploy.sh'"
if [ -n "$SSH_HOST" ]; then
  log "连接 SSH 别名 $SSH_HOST，执行远端部署脚本"
  ssh "$SSH_HOST" "$REMOTE_CMD" || die "远端部署失败，请查看上方日志或登录服务器 journalctl -u $SERVICE_NAME"
else
  log "连接 $SSH_USER@$SERVER_IP:$SSH_PORT，执行远端部署脚本"
  ssh -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$SSH_USER@$SERVER_IP" "$REMOTE_CMD" \
    || die "远端部署失败，请查看上方日志或登录服务器 journalctl -u $SERVICE_NAME"
fi

log "部署流程结束，访问 http://$SERVER_IP:$PORT/ui/"
