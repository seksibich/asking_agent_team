# 部署说明（供 AI/运维快捷部署）

本仓库根目录即**单一部署目录**，包含：后端 `service/`、功能脚本 `skills/*/scripts/`、
前端 `web/`（同源 `/ui/`）、数据库 DDL `service/db/schema.sql`、Docker 配置（根目录）。
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
node --check web/app.js

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

### 0.3 安装 Python 依赖后复检

裸机方式优先使用工程目录中的离线包。目标环境必须是 **Python 3.11 + Linux x86_64**，在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --find-links=python-packages -r requirements.txt
```

`python-packages/` 中已包含 `requirements.txt` 的直接依赖及传递依赖，不需要联网。若部署环境不是 Linux x86_64 + Python 3.11（例如 macOS、ARM 或其他 Python 版本），请先按目标环境重新生成对应离线包，或使用下面的联网备用命令：

```bash
python -m pip install -r requirements.txt
```

安装后运行：

```bash
python -c 'import fastapi, uvicorn, tushare, pandas, numpy, sqlalchemy, pymysql; print("Python dependencies: OK")'
```

Docker 方式无需宿主机安装这些 Python 包；镜像构建过程会按 `requirements.txt` 安装，并固定使用 Python 3.11。

---

## 1. 前置：配置 .env（两种方式都需要）

```bash
cp .env.example .env
# 编辑 .env：
#   TUSHARE_TOKEN=<你的 tushare token>
#   API_KEY=<强随机字符串，管理员 Key，用作 X-API-Key，完整权限>
#   USER_API_KEY=<可选，只读用户 Key：不能改权重/归一窗口、不能触发回测；留空则不启用>
#   DB_URL=            # 留空=本地 SQLite；上云填 mysql+pymysql://user:pwd@host:3306/stock_agent?charset=utf8mb4
```

---

## 方式一：Docker（推荐）

要求：Docker + Docker Compose。

```bash
# 在仓库根目录
docker compose up -d --build          # 首次构建并启动（含 Web）
docker compose ps                      # 查看状态（healthy）
docker compose logs -f                 # 日志

# 健康检查
curl -H "X-API-Key: $API_KEY" http://localhost:18901/health
# 浏览器打开 Web 面板
#   http://localhost:18901/ui/   （右上角设置填入 API_KEY）
```

说明：
- 根目录 `docker-compose.override.yml` 为**开发热挂载**（改 web/service/skills 免重建，`--reload` 生效）。
  生产部署请忽略它：`docker compose -f docker-compose.yml up -d --build`。
- 数据/缓存持久化在根目录 `data/`、`cache/`（compose 卷挂载）。
- 上云 RDS：在 `.env` 设 `DB_URL`，并先在 RDS 执行 `service/db/schema.sql`（或依赖启动自动建表）。

常用运维：
```bash
docker compose restart                 # 重启
docker compose down                    # 停止（保留 data/cache）
docker compose -f docker-compose.yml up -d --build   # 生产模式（无热挂载）
```

---

## 方式二：直接部署（裸机 / venv，无 Docker）

要求：Python 3.11+。

```bash
# 1) 依赖（优先使用工程目录内的离线包）
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --find-links=python-packages -r requirements.txt
# 若当前平台与离线包不匹配，改用联网安装：
# python -m pip install -r requirements.txt

# 2) 环境变量（读 .env 或直接 export）
export TUSHARE_TOKEN=xxx
export API_KEY=xxx            # 管理员 Key（完整权限）
export USER_API_KEY=xxx       # 可选：只读用户 Key（禁改权重/窗口、禁回测）
export CACHE_DIR=$(pwd)/cache
export DATA_DIR=$(pwd)/data
# 可选：export DB_URL=mysql+pymysql://user:pwd@host:3306/stock_agent?charset=utf8mb4
mkdir -p cache data

# 3) 启动（工作目录必须是 service/，以便 import common/registry/loader）
cd service
uvicorn app:app --host 0.0.0.0 --port 18901
#   开发热重载： uvicorn app:app --host 0.0.0.0 --port 18901 --reload --reload-dir . --reload-dir ../skills
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
（全量较慢；日常由定时任务/每日盘后跑增量。未预计算时选股会自动回退实时逐只。）

---

## 上云要点（阿里云 ECS + RDS）

1. ECS 安装 Docker，克隆仓库，配 `.env`（`DB_URL` 指向 RDS MySQL）。
2. RDS 建库并执行 `service/db/schema.sql`（或首启自动建表）。
3. `docker compose -f docker-compose.yml up -d --build`。
4. 安全组仅放行必要来源到 `18901`；建议加反向代理（Nginx/TLS）与 IP 白名单。
5. Agent 侧把服务基址改为公网地址、更新记忆 `service_state.json` 的 `base_url`。

## 排错

| 现象 | 排查 |
|---|---|
| `/health` `tushare_ready:false` | `.env` 的 `TUSHARE_TOKEN` 未配置/无效 |
| 前端提示 unauthorized | Web 设置里未填或填错 `X-API-Key`（=API_KEY 或 USER_API_KEY） |
| 403 forbidden / 用户 Key 不可改配置 | 用户 Key 只读；改权重/归一窗口、触发回测需用管理员 `API_KEY` |
| `/health` `db_ready:false` | `DB_URL` 连接失败；检查 RDS 连通/账号；SQLite 检查 `data/` 可写 |
| 402 tushare quota | 对应 tushare 接口积分/权限不足 |
| 构建慢 | 网络拉取镜像/依赖慢；可配国内 Docker registry mirror 与 pip 源 |
