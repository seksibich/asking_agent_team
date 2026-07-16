# 关注与持仓管理（结构化事实源）

服务端 `portfolio_items` 是当前关注与持仓唯一事实源；运行期 `盯盘/agent记忆/关注与持仓.md` 只保存同步版本与全量可读镜像，不保存待办、进度、关联板块或推断信息。

## 状态模型

- 每个完整股票代码只能有一条 `watch`（关注）或 `holding`（持仓）状态。
- `holding` 必须有真实且大于 0 的成本和整数手数；`watch` 的成本、手数必须为空。
- 禁止猜测代码、成本或手数；信息不完整时先询问用户。

## 强制同步

1. 从 `关注与持仓.md` 读取 `BASE_URL` 和最近同步的 `portfolio_version`。
2. 调 `BASE_URL/health`；若 `db_ready=false` 或版本为 `unavailable`，不得覆盖镜像，只在 `短期记忆/` 建立待同步事项。
3. 若 health 版本不同，调用 `portfolio_get {}`，以响应 `data.rows` 全量覆盖持仓和关注表，并使用同一响应的 `portfolio_version` 更新文件。
4. 用户增删改前先调用 `portfolio_stock_search` 选择标准代码和名称，再形成内存草稿并调用 `portfolio_upload`。
5. 上传成功后只使用该响应的 rows 和版本覆盖镜像；上传失败不得声称已保存，失败事项写入短期记忆，成功补同步后立即删除。
6. 下一次任务再次比较 health 版本，处理并发更新；镜像永远不能反向覆盖服务端未知的新版本。

## 服务调用

- 搜索：`portfolio_stock_search {query, limit}`。
- 获取：`portfolio_get {}`。
- 关注：`portfolio_upload {items:[{code,type:"watch",note}],source:"agent"}`。
- 持仓：`portfolio_upload {items:[{code,type:"holding",cost_price,lots,note}],source:"agent"}`。
- 移除：`portfolio_upload {items:[{code,deleted:true}],source:"agent"}`。
- 以上功能只允许管理员 Key。

## 与业务审计的边界

`portfolio_items` 表示当前状态；`selections` 中的 `watch/holding` 只是用户明确要求时创建的历史观察快照。每日快照可引用当前 rows，但不得回写镜像。题材、选股进度、复查事项进入报告、DB、daily 或短期记忆，不能写入 `关注与持仓.md`。