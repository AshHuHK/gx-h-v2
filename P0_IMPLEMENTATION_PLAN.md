# P0 分阶段实施与回退

| 阶段 | 变更、验收与回退 |
|---|---|
| 0 | 导入固定 GX 源树，核实官方 tag/CLI，保存原测试失败与环境依赖。回退到本地仓库首提交。 |
| 1 | 用官方 protocol 从外部驱动主对话和独立 Root。测试握手、Goal、通知、steer、进程重启。 |
| 2 | 在 GX 事务中确认人类来源、条目版本、影响与 Outbox。对不同 SQLite 写顺序和 SIGKILL 做验证。 |
| 3 | 接好每个 Attempt 的 Root、工具权限、待处理消息、工作流与事件循环。重启通过已存 ID 和官方历史对账。 |
| 4 | 项目预算和结构化约束进入 Job 准入；由同一 Job owner 固化、完成、核验执行清单。 |
| 5 | 原始测量进入冻结评估与独立审查，正式写入证据、选择、状态和下一轮。 |
| 6 | 通过真实官方二进制、实际 CPU Job 和连续三轮验收。在线 OAuth 与脚本模型分别计数，记录未通过项与未测原因。 |

阶段检查在 [acceptance](acceptance/) 下。部署新增状态目录；回退时停止 P0 驱动、保留其状态，原发布继续使用自己的数据库。跨版本恢复先备份整套状态及 artifacts。

## 阶段交接

| 阶段 | 发布树实现与证据 |
|---|---|
| 0 | `7617a24` 导入、`37a4e0f` 源码所有者审计。上游锁定、基线失败和后续修正各自保存。[基线](BASELINE_REPORT.md) |
| 1 | `e2653ca` 投递收据、`a52cedc` 官方根线程，`7b32b59` 版本与账号绑定。[协议测试](audit/NATIVE_ACCEPTANCE.md) |
| 2 | `7395334` 人类来源、条目版本、影响和 Outbox；`4474080` 真实通知投递故障。[授权和影响](acceptance/e2e_matrix.md) |
| 3 | `39bbc07` 项目驱动、`53aa2c9` 断线恢复和用量、`7a8f0ec` 原线程配置恢复。[强杀后的原任务](acceptance/runs/e2e-kill/fault.json) |
| 4 | `00ba331` Job 真实执行清单、哈希核验及预算；`cde84dc` 子代理观测归集。[Job 专项](acceptance/runs/jobs/unittest.log) |
| 5 | `99ae393` 正式科学记录和结算，`5dfd560` 原产物重评与独立审查，`ca5d40c` 目标变化后在原项目重开。[科学记录](acceptance/runs/science/) · [换目标](acceptance/runs/replan-grid/summary.json) |
| 6 | `1c2b165` 完整验收矩阵和三轮实验，`3a00e5d` 同项目新目标的实际端到端证据。正式发行后的核对见总报告。[验收报告](ACCEPTANCE_REPORT.md) |
