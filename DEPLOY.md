# 部署说明（供 AI/运维快捷部署）

本仓库根目录即**单一部署目录**，包含：后端 `service/`、前端 `service/web/`（同源 `/ui/`）、
功能脚本 `agent/skills/*/scripts/`、数据库 DDL `service/db/schema.sql`、Python 依赖 `profile/requirements.txt`、Docker 配置（根目录）。
前后端与数据库一体，一个服务进程对外暴露 HTTP + Web。

- 监听端口：`18901`
- 接口鉴权：请求头 `X-API-Key`（值=`.env` 的 `API_KEY`）
- 数据库：默认本地 SQLite（`data/stock_agent.db`）；上云可切 RDS MySQL（`DB_URL`）
- Web 面板：`http://<host>:18901/ui/`

---

## 0. 部署前环境检查

先在仓库根目录完成环境预检。不同部署方式的要求如下：

| 环境 | 建议版本 | 是否必须 | 用途 |
|---|---:|---|---|
| Python | 3.11+ | 裸机部署必须；Docker 部署由镜像提供 | 后端服务、数据处理和功能脚本 |
| Node.js | 18+ | 建议安装，运行服务不依赖 | 检查前端 JavaScript 语法；当前 Web 为静态资源，无需 `npm install` 或构建 |
| MySQL | 8.0+ | 仅使用 MySQL/RDS 时需要；默认 SQLite 可跳过 | 建库、执行 DDL 和排查数据库连通性 |
| Docker / Compose | Docker 部署必须 | 仅 Docker 方式 | 构建和运行一体化服务 |

### 0.1 检查命令与版本

```bash
# Python：必须为 3.11 或更高版本
python3 --version
python3 -c 'import sys; print("Python:", sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11+")'

# Node.js：建议为 18 或更高版本；用于检查静态前端脚本
node --version
node -e 'const major=Number(process.versions.node.split(".")[0]); console.log("Node.js:", process.versions.node); process.exit(major >= 18 ? 0 : 1)'
node --check service/web/app.js

# MySQL：仅 DB_URL 指向 MySQL/RDS 时检查；默认 SQLite 可跳过
mysql --version

# Docker 部署时额外检查
docker --version
docker compose version
```

任一命令提示 `command not found` 时，先安装对应环境再继续。Node.js 检查失败不影响当前静态 Web 随后端运行，但应在修改前端脚本前修复；使用 SQLite 时不要求安装 MySQL 客户端。

### 0.2 检查 MySQL/RDS 连通性

仅当 `.env` 中配置了 MySQL `DB_URL` 时执行。不要把密码直接写进命令或提交到仓库；`--password` 会安全地交互式询问密码。

```bash
mysql --host="<MySQL或RDS地址>" \
  --port=3306 \
  --user="<用户名>" \
  --password \
  --execute="SELECT VERSION() AS version, NOW() AS server_time;"
```

连接成功后以只读方式确认服务端字符集和目标库是否存在：

```bash
mysql --host="<MySQL或RDS地址>" \
  --port=3306 \
  --user="<用户名>" \
  --password \
  --execute="SHOW VARIABLES LIKE 'character_set_server'; SHOW DATABASES LIKE 'stock_agent';"
```

若目标库不存在，由 DBA/RDS 管理员创建 `stock_agent` 并设置 `utf8mb4`；应用账号至少需要目标库的建表和读写权限。随后执行 `service/db/schema.sql`，或让服务首次启动时自动建表。

### 0.3 Python 环境与依赖复检

Python 解释器、虚拟环境和 `profile/requirements.txt` 依赖由部署配置方按目标系统自行提供；本仓库不内置 Python 运行时或第三方依赖包。完成配置后，在仓库根目录执行：

```bash
python3 --version
python3 -m pip --version
python3 -m pip check
python3 -c 'import fastapi, uvicorn, tushare, pandas, numpy, sqlalchemy, pymysql; print("Python dependencies: OK")'
```

如果使用虚拟环境，请先由配置方创建并激活 `.venv`；如果使用系统包管理器、内部镜像或离线制品库，也由配置方负责安装、版本匹配和安全校验。目标环境至少应满足 Python 3.11，并与 `profile/requirements.txt` 兼容。

Docker 构建同样依赖配置方准备可用的基础镜像和 Python 依赖来源；无网络环境请提前导入基础镜像或配置内部镜像仓库。

---

## 1. 前置：配置 .env（两种方式都需要）

```bash
cp profile/.env.example .env
# 编辑 .env：
#   TUSHARE_TOKEN=<你的 tushare token>
#   API_KEY=<强随机字符串，管理员 Key，用作 X-API-Key，完整权限>
#   USER_API_KEY=<可选：访客 Key，可查看/选股/读情绪/查看回测结果，不能改权重/窗口/预计算；留空则不启用>
#   DB_URL=            # 留空=本地 SQLite；上云填 mysql+pymysql://user:pwd@host:3306/stock_agent?charset=utf8mb4
```

---

## 方式一：Docker（推荐）

要求：Docker + Docker Compose。

```bash
# 在仓库根目录
# 生产部署必须显式使用主配置，避免自动合并开发热挂载配置
docker compose -f docker-compose.yml up -d --build --force-recreate
docker compose -f docker-compose.yml ps                 # 查看状态（healthy）
docker compose -f docker-compose.yml logs --tail=100    # 查看启动日志

# 三类探针
curl -fsS http://localhost:18901/live    # 只判断进程存活
curl -fsS http://localhost:18901/ready   # 生产流量就绪；任一关键依赖失败返回 503
curl -fsS http://localhost:18901/health  # 市场、版本和依赖诊断快照

# 管理员鉴权检查：只输出角色，不输出 Key
curl -sS -H "X-API-Key: $API_KEY" http://localhost:18901/whoami
# 预期："role":"admin"、"is_admin":true

# 浏览器打开 Web 面板
#   http://localhost:18901/ui/   （右上角设置填入 API_KEY）
```

说明：
- 管理员变量支持 `API_KEY` 和 `ADMIN_API_KEY`，两者均可生效；建议统一只配置 `API_KEY`，避免运维混淆。
- Key 在服务进程启动时读取。修改 `.env` 或云平台环境变量后，必须执行 `up -d --build --force-recreate`；仅执行 `restart` 可能继续使用旧容器环境。
- 后端会清理配置值和请求 Key 的首尾空白及外层引号，但不会修复变量名错误。请求头必须是 `X-API-Key`，不能只配置 `ADMIN_KEY`、`SERVICE_API_KEY` 等未支持的变量名。
- 启动日志只输出 `admin_keys` 和 `user_key` 的配置状态，不会输出密钥内容。若 `admin_keys=0`，说明云端变量没有注入到实际服务进程。
- 根目录 `docker-compose.override.yml` 为**开发热挂载**（改 `service/`（含 `service/web`）、`agent/skills` 免重建，`--reload` 生效）。生产部署请显式使用：`docker compose -f docker-compose.yml up -d --build --force-recreate`。
- 数据/缓存持久化在根目录 `data/`、`cache/`（compose 卷挂载）。
- 上云 RDS：在 `.env` 设 `DB_URL`，并先在 RDS 执行 `service/db/schema.sql`（或依赖启动自动建表）。

常用运维：
```bash
docker compose -f docker-compose.yml restart                 # 不改变环境变量时使用
docker compose -f docker-compose.yml down                    # 停止（保留 data/cache）
docker compose -f docker-compose.yml up -d --build --force-recreate   # 更新代码或 Key 后使用
```

---

## 方式二：直接部署（裸机 / venv，无 Docker）

要求：由配置方提供 Python 3.11+、虚拟环境和已安装且通过校验的 `profile/requirements.txt` 依赖。本仓库不负责提供或安装 Python 运行时及第三方依赖。

```bash
# 1) 复检配置方提供的 Python 环境
python3 --version
python3 -m pip check
python3 -c 'import fastapi, uvicorn, tushare, pandas, numpy, sqlalchemy, pymysql; print("Python dependencies: OK")'

# 2) 环境变量（读 .env 或直接 export）
export TUSHARE_TOKEN=xxx
export API_KEY=xxx            # 管理员 Key（完整权限）
export USER_API_KEY=xxx       # 可选：访客 Key（可查看回测，禁改权重/窗口/预计算）
export CACHE_DIR=$(pwd)/cache
export DATA_DIR=$(pwd)/data
# 可选：export DB_URL=mysql+pymysql://user:pwd@host:3306/stock_agent?charset=utf8mb4
mkdir -p cache data

# 3) 启动（工作目录必须是 service/，以便 import common/registry/loader）
cd service
uvicorn app:app --host 0.0.0.0 --port 18901
#   开发热重载： uvicorn app:app --host 0.0.0.0 --port 18901 --reload --reload-dir . --reload-dir ../agent/skills
```

后台常驻（任选）：
- systemd：`ExecStart=/path/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 18901`，`WorkingDirectory=/path/service`，`EnvironmentFile=/path/.env`。
- 或 `nohup uvicorn ... &` / `tmux`。

验证：`curl -H "X-API-Key: $API_KEY" http://localhost:18901/health`，Web 打开 `http://localhost:18901/ui/`。

---

## 首次数据准备（可选，提升选股速度）

选股优先读预计算因子（`daily_factors`），首次可补算历史窗口：

```bash
curl -XPOST http://localhost:18901/call \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"function":"precompute_daily_factors","params":{"full":true}}'
```
（全量较慢；日常由外部定时任务在每日盘后调用默认单日模式。任务失败或部分成功时，按返回的 `retryable_dates` 重跑；未预计算或质量不合格时选股会自动回退实时逐只。）

---

## 上云要点（阿里云 ECS + RDS）

1. ECS 安装 Docker，克隆仓库，配 `.env`（`DB_URL` 指向 RDS MySQL）。
2. RDS 建库并执行最新版 `service/db/schema.sql`（包含因子、盯盘和动态访客 Key 摘要表；应用首启也会通过 `create_all()` 建立缺失表，并执行幂等兼容迁移）。
3. `docker compose -f docker-compose.yml up -d --build --force-recreate`。
4. 安全组仅放行必要来源到 `18901`；建议加反向代理（Nginx/TLS）与 IP 白名单。
5. Agent 侧把公网基址同时写入运行期 `服务状态与能力.md` 与 `关注与持仓.md` 的 `BASE_URL`；真实 Key 仍只放安全配置。

## 自动化部署脚本（阿里云 ECS + systemd）

`deploy/` 提供一套「本地推送 → 云端拉取重启」的自动化脚本（裸机 systemd 方式，服务器 8.153.99.132，端口 18901）。

**服务器一次性准备**（Alibaba Cloud Linux，以 root 与仓库目录 `/root/asking_agent_team` 为例）：
```bash
yum install -y git python3                 # 需 Python 3.11+，必要时装 python3-venv
git clone <仓库地址> /root/asking_agent_team
cd /root/asking_agent_team
cp profile/.env.example .env               # 填 TUSHARE_TOKEN / API_KEY / DB_URL(RDS) 等
```

**日常部署**（本地仓库根目录，改完代码并 commit 后）：
```bash
bash deploy/local_deploy.sh                # 推送当前分支 → SSH 触发服务器部署
# 指定分支：BRANCH=main bash deploy/local_deploy.sh
```

`deploy/local_deploy.sh` 会 `git push` 后 SSH 登录服务器执行 `deploy/remote_deploy.sh`，后者依次：
1. `git reset --hard origin/<分支>` 拉取最新代码；准备 venv、同步 `profile/requirements.txt` 依赖；
2. **检查并同步数据库结构**（`deploy/db_sync.py`：对比并 `create_all` 缺失表 + 兼容迁移，失败即终止，不带病重启）；
3. **释放端口 18901**（先停 systemd 服务，再 `fuser`/`ss` 兜底清理残留进程）；
4. 渲染并安装主服务及两组监控单元：五分钟 `/live`+`/ready` 探针、每日 22:50 中文运行汇总 timer；随后重启主服务；
5. **就绪检查** `/ready`（最长 40s），通过后再读取 `/health` 诊断快照；失败则打印 `journalctl` 日志并以非 0 退出。

全流程 `set -euo pipefail` + 分步中文日志，任一环失败立即中断并提示。可用环境变量覆盖 `APP_DIR/BRANCH/PORT/SERVICE_NAME/RUN_USER/VENV` 等默认值。

**内存护栏（避免整机因 OOM 失联）**：主服务 systemd 单元自动写入 `MemoryHigh`（软上限，默认总内存 70%）与 `MemoryMax`（硬上限，默认总内存 85%），并设 `OOMPolicy=kill`。超过硬上限只在本服务 cgroup 内 OOM、随后 `Restart=on-failure` 自动拉起，**不再拖垮整机与 SSH**。可用环境变量覆盖：

```bash
MEMORY_MAX=6G   MEMORY_HIGH=5G   bash deploy/remote_deploy.sh   # 显式指定
# 不指定时按 /proc/meminfo 的总内存自动计算；总内存不可解析时退回 infinity（不设护栏）
```

> 小机型（如 2C1G 经济型）跑全市场 `precompute` 极易 OOM 拖垮整机；建议升级到 **2C8G 通用算力型 u1（`ecs.u1-c1m4.large`）+ ESSD**，并保留本护栏作为兜底。

**盯盘 HTTP 连接复用**：服务端 `common.get_session()` 提供全局共享 `requests.Session`（keep-alive + 连接池 + 有限重试），盘中实时快照（新浪分页并发抓取）与通知 webhook 均复用连接，降低每轮握手开销。连接池大小可用 `HTTP_POOL_MAXSIZE`（默认 16）、重试次数用 `HTTP_RETRY_TOTAL`（默认 1）调整。

正式监控单元默认名称随 `SERVICE_NAME` 变化，可用以下命令检查：

```bash
systemctl list-timers | grep stock-agent-monitor
systemctl status stock-agent-monitor.timer stock-agent-monitor-daily.timer
journalctl -u stock-agent-monitor.service -n 50 --no-pager
```

每日汇总写入 `data/logs/monitor/daily/YYYYMMDD.md`；原始低基数事件位于 `data/logs/monitor/events/`。定时器从 `.env` 读取管理员 Key 调用汇总接口，日志不回显 Key。汇总把普通 4xx 记为业务拒绝，把 408、429 和 5xx 记为服务故障；默认对 `data/logs/` 下审计及监控日志保留 90 日、7 日后压缩，并检查磁盘使用率和剩余空间。可在 `.env` 调整：

```bash
MONITOR_LOG_CLEANUP_ENABLED=true       # 调查期间设 false，冻结压缩和删除
MONITOR_LOG_RETENTION_DAYS=90
MONITOR_LOG_COMPRESS_AFTER_DAYS=7
MONITOR_DISK_WARN_PERCENT=85
MONITOR_DISK_MIN_FREE_GB=5
PUBLIC_BASE_URL=https://stock.example.com  # 部署完成提示使用；留空不猜测公网地址
```

`PUBLIC_BASE_URL` 只用于部署完成提示，不改变 uvicorn 监听地址或鉴权。日志删除不可恢复；需要长期留存时应先接入对象存储或云日志归档。

> 若用 RDS MySQL，`.env` 配好 `DB_URL`，第 2 步会自动在 RDS 上建表/迁移；无 `DB_URL` 时回退本地 SQLite。

## 排错

| 现象 | 排查 |
|---|---|
| `/health` `tushare_ready:false` | `.env` 的 `TUSHARE_TOKEN` 未配置/无效 |
| 前端提示 unauthorized | Web 设置里未填或填错 `X-API-Key`（=API_KEY 或 USER_API_KEY） |
| 403 forbidden / 用户 Key 不可改配置 | 用户 Key 只读；改权重/归一窗口、运行全市场预计算需用管理员 `API_KEY`；查看回测不需要管理员 |
| `/health` `db_ready:false` | `DB_URL` 连接失败；检查 RDS 连通/账号；SQLite 检查 `data/` 可写 |
| 402 tushare quota | 对应 tushare 接口积分/权限不足 |
| 构建慢 | 网络拉取镜像/依赖慢；可配国内 Docker registry mirror 与 pip 源 |
