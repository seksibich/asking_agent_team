# 部署说明（供 AI/运维快捷部署）

本仓库根目录即**单一部署目录**，包含：后端 `service/`、功能脚本 `skills/*/scripts/`、
前端 `web/`（同源 `/ui/`）、数据库 DDL `service/db/schema.sql`、Docker 配置（根目录）。
前后端与数据库一体，一个服务进程对外暴露 HTTP + Web。

- 监听端口：`18901`
- 接口鉴权：请求头 `X-API-Key`（值=`.env` 的 `API_KEY`）
- 数据库：默认本地 SQLite（`data/stock_agent.db`）；上云可切 RDS MySQL（`DB_URL`）
- Web 面板：`http://<host>:18901/ui/`

---

## 0. 前置：配置 .env（两种方式都需要）

```bash
cp .env.example .env
# 编辑 .env：
#   TUSHARE_TOKEN=<你的 tushare token>
#   API_KEY=<强随机字符串，用作 X-API-Key>
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
# 1) 依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) 环境变量（读 .env 或直接 export）
export TUSHARE_TOKEN=xxx
export API_KEY=xxx
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
| 前端提示 unauthorized | Web 设置里未填或填错 `X-API-Key`（=API_KEY） |
| `/health` `db_ready:false` | `DB_URL` 连接失败；检查 RDS 连通/账号；SQLite 检查 `data/` 可写 |
| 402 tushare quota | 对应 tushare 接口积分/权限不足 |
| 构建慢 | 网络拉取镜像/依赖慢；可配国内 Docker registry mirror 与 pip 源 |
