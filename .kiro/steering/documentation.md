# 业务变更与文档同步规范

本规则默认始终生效。任何新增、修改或删除业务都必须在同一轮改动中同步文档，不能只改代码。

## 必须同步的内容

1. **业务说明**：用户看到什么、适用场景、输入输出、失败时如何表达。
2. **接口契约**：端点或功能名、参数、权限、返回字段、错误码和兼容策略。
3. **数据口径**：事实源、有效日期、盘中/日终、完整/临时、缓存周期、缺失项和 fallback。
4. **交易时段**：交易日各阶段、非交易日、默认日期、是否允许盘中请求。
5. **一致性与安全**：唯一键、事务、幂等、并发、多实例、敏感数据和审计边界。
6. **测试矩阵**：正常、边界、权限、失败、并发、全时段和前后端契约测试。
7. **监控运维**：`/live`、`/ready`、业务指标、错误摘要、日志位置、部署与回滚影响。

## 按改动类型更新

- Agent 行为、Skill、角色或任务：更新 `agent/**`、`doc/02-Agent编排与业务模块.md`、`doc/SERVICE_INDEX.md`。
- 后端接口或数据功能：更新 `doc/AGENT_SERVICE_GUIDE.md`、`doc/SERVICE_INDEX.md` 和对应专题。
- 前端业务或日期规则：更新 `doc/03-前端业务与全时段规则.md` 及测试矩阵。
- 数据库、缓存、原子性或鉴权：更新 `doc/04-数据存储缓存与一致性.md` 和 `service/db/schema.sql`。
- 探针、日志、Docker、systemd 或部署：更新 `doc/05-测试探针监控与运维.md` 与 `DEPLOY.md`。
- 新增专题文档：同步加入 `doc/README.md` 和根 `README.md` 入口。

## Agent 文档版本

修改 `agent/**`、`agent/skills/**/SKILL.md`、`agent/memory/**` 或 `doc/AGENT_SERVICE_GUIDE.md` 等 Agent 相关/交叉文档时，必须：

1. 提升 `agent/init.md` 的 `AGENT_DOC_VERSION`；
2. 在 `profile/CHANGELOG-AGENT.md` 顶部新增版本、日期、摘要、文件清单和 Agent 动作；
3. 不手工修改服务 `data_version`。

## 完成门禁

代码与文档同步后，运行受影响测试、语法检查和功能装载检查。涉及运行时代码、前端、Docker 或装载路径时，必须重建本地 Docker，检查容器、`/live`、`/ready` 和 `/health`。未经用户明确授权不得 commit、push 或触发阿里云部署。
