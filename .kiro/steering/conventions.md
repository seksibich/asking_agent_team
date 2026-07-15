# 协作与编码规范

## 语言（强制）

- **一切输出统一使用中文**：对话回复、文档、代码注释、docstring、Git commit message、变更日志、报告全部用中文。
- 技术专有名词（函数名、参数名、接口名、库名、错误码等）保留英文原文，但须紧邻中文解释；不得整段堆砌英文。
- 面向最终用户的报告与推送使用通俗中文，避免罗列英文接口名 / 参数名 / JSON 字段 / 因子代码（详见 `product.md`）。

## 文档版本机制（改 agent 文档必守）

每次调整任何 agent 相关文档（`agent/init.md` / `agent/index.md` / `agent/schedule.md` / `agent/SOUL.md` / `agent/agents/**` / `agent/skills/**/SKILL.md` / `agent/memory/**` / `doc/AGENT_SERVICE_GUIDE.md` 等）都必须：

1. 在 `profile/CHANGELOG-AGENT.md` 顶部新增一条版本记录（语义化版本号 `vMAJOR.MINOR.PATCH` + 日期 + 摘要 + 变更文件清单 + agent 需执行的动作）。
2. 同步更新 `agent/init.md` 顶部的 `AGENT_DOC_VERSION` 为该新版本号。

数据服务功能索引的变更由服务端 `data_version` 自动管理，与文档版本互不替代。

## 数据服务改动须守

- 新增/修改功能通过 `@register` 声明，不手工维护 `data_version`。
- 保持功能返回统一结构 `{source, fetched_at, rows/...}`；数据接口失败要如实抛错，禁止用编造/推断兜底。
- 移动或删除 `agent/skills/*/scripts/` 结构时，务必同步 `service/loader.py` 的扫描路径与 `Dockerfile`、`docker-compose*.yml` 的挂载/COPY 路径，改动后用 `python cli.py functions` 验证功能可加载。

## 安全

- `.env` 含真实 token / Key，已 gitignore，禁止提交或在日志/回复中回显密钥值，只按变量名引用。
- 数据/缓存目录 `data/`、`cache/` 为运行期产物，已 gitignore。

## 提交与验证

- 改动后尽量本地验证：服务侧 `python cli.py functions` 能加载全部功能；前端 `node --check service/web/app.js`。
- Git commit message 用中文，简明说明改了什么、为什么。
