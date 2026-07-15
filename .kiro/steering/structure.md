# 工程目录结构

工程按职责分为四大目录 + 根级全局文档（2026-07 重组，v1.3.0）：

```
stock_agent_kit/
├── README.md  DEPLOY.md  LICENSE           # 全局文档（项目说明 / 部署规则）
├── Dockerfile  docker-compose.yml  docker-compose.override.yml  .dockerignore
├── .env                                    # 真实密钥（gitignore）
│
├── agent/                                  # ★ 与 agent 相关的全部内容
│   ├── init.md  index.md  SOUL.md  schedule.md
│   ├── agents/                             # 主 + 5 子 Agent 角色 + 编排（TEAM/ORCHESTRATION）
│   ├── memory/                             # MEMORY.md + templates/
│   └── skills/<skill>/                     # 12 个技能
│       ├── SKILL.md                        #   agent 技能规范（提示词）
│       └── scripts/*.py                    #   数据服务功能模块（被 service 加载）
│
├── service/                                # ★ 数据服务：后端 + 前端 + DB
│   ├── app.py registry.py loader.py common.py db.py cli.py
│   ├── web/                                # 前端（同源 /ui/）
│   └── db/                                 # schema.sql + 说明
│
├── doc/                                    # ★ agent↔服务交叉文档 + 业务索引
│   ├── AGENT_SERVICE_GUIDE.md              # 服务调用协议/版本机制
│   └── SERVICE_INDEX.md                    # 服务功能业务索引 + 定时任务交叉表
│
├── profile/                                # ★ 配置 + 变更日志
│   ├── CHANGELOG-AGENT.md  .env.example  requirements.txt
│
├── cache/  data/                           # 运行期产物（gitignore）
```

## 路径引用约定（重要）

- **agent/ 目录内文档互相引用**写相对 `agent/` 的路径：`skills/x/SKILL.md`、`index.md`、`agents/TEAM.md`、`memory/MEMORY.md`。
- **跨目录引用**（`agent/` 之外的 `doc/`、`profile/`、`service/`）写仓库根相对路径：`doc/AGENT_SERVICE_GUIDE.md`、`profile/CHANGELOG-AGENT.md`、`service/db/schema.sql`。
- 运行期记忆/产出目录 `盯盘/agent记忆/`、`投研/` 是 agent 生成的输出，与工程目录 `agent/` 无关。

## 关键耦合点（改目录务必同步）

- `service/loader.py` 的 `SKILLS_DIR = APP_ROOT/"agent"/"skills"`：数据服务从这里发现功能脚本。
- `service/app.py` 的 `WEB_DIR = <service>/web`：前端同源挂载。
- `Dockerfile` 的 `COPY service`、`COPY agent/skills`、`COPY profile/requirements.txt`。
- `docker-compose.override.yml` 的热挂载与 `--reload-dir /app/agent/skills`。
- 改动后用 `python cli.py functions` 与 `node --check service/web/app.js` 验证。
