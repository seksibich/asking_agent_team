# 技术栈与常用命令

## 技术栈

- **后端**：Python 3.11+ / FastAPI / uvicorn
- **数据**：tushare pro（1.4.6，15000 积分档）、pandas / numpy
- **持久化**：SQLAlchemy 2.0；默认本地 SQLite（`data/stock_agent.db`），上云切 RDS MySQL（`DB_URL`，PyMySQL）
- **前端**：静态 Web（原生 HTML/CSS/JS，无构建），与后端同源挂载在 `/ui/`
- **部署**：Docker + docker compose；也支持裸机 uvicorn
- **端口**：`18901`
- **鉴权**：请求头 `X-API-Key`（管理员 `API_KEY` / 访客 `USER_API_KEY`）

## 服务架构关键点

- `service/registry.py`：`@register(name, group, desc, params, returns)` 注册功能；`data_version` = 功能索引内容哈希，新增/改动自动变化。
- `service/loader.py`：启动时扫描 `agent/skills/*/scripts/*.py` 与 `service/` 自动 import 触发注册；所有 scripts 目录加入 `sys.path`，模块间可扁平 import（如 `import factors`）。
- `service/common.py`：tushare 客户端单例、缓存（当日缓存 + `permanent/` 永久历史缓存）、交易日守卫、统一返回结构 `{source, fetched_at, rows}`。
- `service/db.py`：selections / predictions / forward_returns / backtest_snapshots / daily_factors / config_versions / daily_sentiment 等表，幂等 upsert。
- 端点：`GET /health`、`GET /functions`、`GET /whoami`、`POST /call`、`/admin/user-keys*`。
- `service/version.py`：`/health` 回传 `agent_doc_version`（从 `agent/init.md` 解析）与 `git_revision`（读根目录 `VERSION` 文件，回退 `git rev-parse`），供 agent 做文档版本对齐与增量更新；`VERSION` 由 `deploy/remote_deploy.sh` 写入、已 gitignore。

## 常用命令（均在仓库根目录执行）

```bash
# Docker（生产）：显式使用主配置，避免合并开发热挂载
docker compose -f docker-compose.yml up -d --build --force-recreate
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs --tail=100

# 健康 / 就绪 / 鉴权自检（不回显密钥）
curl http://localhost:18901/live
curl http://localhost:18901/ready
curl http://localhost:18901/health
curl -sS -H "X-API-Key: $API_KEY" http://localhost:18901/whoami

# 不启服务直接调试（在 service/ 目录）
python cli.py functions
python cli.py call screen_sector '{"top_n":10}'

# 前端语法检查（静态资源，无需 npm）
node --check service/web/market-state.js
node --check service/web/app.js

# 自动化测试
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_frontend_state.mjs
```

## 开发扩展功能（新增数据接口）

1. 在某个 `agent/skills/<skill>/scripts/` 下新增/编辑模块。
2. 用 `@register` 声明功能，函数签名统一 `fn(p: dict) -> dict`，返回 `{source, fetched_at, rows/...}`。
3. 重启/重建服务，`loader` 自动发现，`/functions` 自动收录，`data_version` 自动变化。
4. 破坏性协议变更才手工把 `registry.py` 的 `SCHEMA_VERSION` +1。

## 错误码约定

| status | 含义 | 处理 |
|---|---|---|
| 400 | 参数错误 / 未知功能 | 校验 function 与 params |
| 401 | 鉴权失败 | 检查 X-API-Key |
| 402 | tushare 积分/权限不足 | 跳过或走资讯类 fallback |
| 403 | 用户 Key 调用管理员专属功能 | 改用管理员 Key |
| 503 | 服务未启动 | 启动本地服务 |
