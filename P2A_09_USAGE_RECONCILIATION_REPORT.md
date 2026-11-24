# P2A-09 Usage 对账

U01–U08 全部通过。原始子线程案例的 915 个累计 token 中，4 笔带响应 ID 的回执合计 732，余下 183 计入该线程的 `UNATTRIBUTED_DELTA`。父线程有另外 3 笔各 35 的回执。两条线程共归属 837 个 token。本次回放的父预留在宽限期后按 837 估算结算一次，预算仍为额外 183 保留敞口，共占用 1,020。[原始宿主记录](acceptance/phase2a/regression/final/batch/extra-child/observation.json) · [复测摘要](acceptance/closure/usage-history/usage-bb58cc8/summary.json)

## 官方协议的归属边界

核验了本地 Codex `rust-v0.169.0` 源码包，SHA-256 为 `ed604080584e0236b39da1662baecd8c5b735e6cd998cd2f1f5c2e38d3014bd7`，对应上游提交 `d32063f96c2dff1dc96af839b3952abb6acfeda4`。[发行锁](release-assets/lock.json)

- 包内 `codex-rs/core/src/session/mod.rs` 第 5678–5718 行先发送带响应 ID 的完成通知。Usage 为空时跳过 token 记录。`app-server/src/bespoke_event_handling.rs` 第 1245–1255 行把线程、Turn、响应 ID 和可空的 Usage 交给客户端。
- `core/src/state/session.rs` 第 253–288 行按 Turn ID 重置 Turn 小计，在线程上继续累加。`protocol/src/protocol.rs` 第 2593–2614 行累计上下文计数，同时保存最后一次用量。第 2617–2629 行还有按上下文窗口填充值的路径。因此，单凭一次 `thread/tokenUsage/updated` 无法确定某次请求的费用。`app-server/src/bespoke_event_handling.rs` 第 1653–1668 行说明了这类通知携带 Turn ID 的来源。
- `core/src/agent/history.rs` 第 364–389 行清理继承历史中的计量信息。`core/src/agent/control_tests.rs` 第 2825–2842 和 2859–2874 行核对子线程的检查点和自身累计。原探针显式建立独立上下文，`fork_turns` 取 `none`。[原探针](acceptance/acc1_protocol.py)

## 产品账目

`gx-p0 status --state DIR --run-id RUN` 的 `metering.cost_report` 按每个真实线程汇总。一次状态查询使用同一 SQLite 读快照。原生子线程通过已核验的亲缘关系和 `rootTurnId` 归入原预算，子线程响应仍按自己的线程和 Turn 展示。[成本报告](gx-h/gx_core/p0/metering_report.py) · [状态读取入口](gx-h/gx_core/p0/metering.py)

| 类别 | 报告字段的含义 |
| --- | --- |
| `PROVIDER_CONFIRMED` | 去重、无冲突的响应所给的 token，或通过官方历史边界核验的同一 Turn 用量。重叠证据取覆盖范围较大的一份。Turn 仍待补全时，已有凭据的部分可以报告。 |
| `ESTIMATED` | `provisional_budget_tokens` 是仍属估算的预算净额。`tokens` 给出同一 Root 预算上超过已有观察的估算敞口。两者分别回答“暂扣多少”和“有多少仍靠估算”。 |
| `UNRESOLVED` | 归属冲突、未确认发送、仍在等待、缺乏可用依据的预留保留其引用。未知 token 为 `null`。已落账后出现矛盾回执时，成本报告标出争议，先前凭证保留。 |
| `UNATTRIBUTED_DELTA` | 每条线程的累计观察减去其可归属用量。明确的非负差额逐线程汇总。缺少快照或回执已超过旧快照时，该线程的差额为 `null`。 |

每次付费 `turn/start` 使用同一预留。完整、可匹配的响应回执结算一次，子线程无需另扣 API 调用。自然中断缺少终止依据时保留 `HELD`。已确认发送且终止的 Turn 从持久化的 `terminal_at` 起使用运行配置 `metering.grace_seconds`，默认 30 秒。Driver 通过现有本地维护入口处理到期记录，已暂停项目也可结算，过程无需连接模型。有效 grant 和可归属的响应下界支持保守估算。缺失 grant 和冲突在无法确认时保留原预留。[原生投递](gx-h/gx_core/p0/root_delivery.py) · [宽限期与结算](gx-h/gx_core/p0/metering.py) · [Driver](gx-h/gx_core/p0/driver.py)

原估算是带 `confidence=ESTIMATED` 的追加式 `budget.settled` 事件。迟到的完整回执在同一写事务中追加 `usage.receipt_adjusted`，引用原结算的 `event_id`。有差额时，由既有预算所有者追加一笔有符号的 `budget.token_adjusted`。每个预留最多有一个回执调整，差额为零也保留确认事件。预算视图保留既有 `PROVIDER_RECONCILED` 名称和原结算额。Turn 的 `usage_class`、逐响应成本与调整的 `basis` 给出最终证据类别。旧版关于共同资金范围的累计检查点以 `THREAD_BUDGET_CHECKPOINT` 标注。它可核对同一资金范围内经过宽限期的历史估算，未归属部分继续呈现在线程账上。[增量迁移](gx-h/gx_core/p0/zzzz_p2a_usage.sql) · [预算敞口](gx-h/gx_core/p0/meter_limits.py)

## 可复核结果

下列矩阵采用 `SCRIPTED` 输入、产品 RootDelivery 和真实磁盘 SQLite。U07 回放的是历史 `OFFICIAL_HOST_LOOPBACK` 所存的原响应 ID、线程归属与金额，保留原来的冷恢复超时结果。U08 在独立进程和真实时钟上执行。报告中的 token 均无货币单价。[测试用例](acceptance/closure/test_usage.py) · [命令、环境与退出码](acceptance/closure/usage-history/usage-bb58cc8/commands.json)

| 场景 | 观测结果 |
| --- | --- |
| U01 完整回执 | 两份响应分别为 17 和 21。重放完成通知，预算仅结算 38 和一次 API 调用。 |
| U02 中断后等待 | 已收到的 35 在响应账可见。宽限期前运行产品 Driver 后，原预留继续保持 HELD。 |
| U03 到期 | Driver 两次维护只留下一个 100 的估算结算事件，其中已有 35 的凭据，估算敞口为 65。 |
| U04 迟到回执 | 原事件 4 估算 100，事件 5 引用 4 并记录确认为 70，资金差额为 −30。第二次估算与回执均为 100，事件 9 引用 8 并记录零调整。 |
| U05 重复、乱序与争议 | 一笔迟到回执先于匹配快照，两个独立连接并发重放快照后只增加 30。冲突金额和跨 Turn 重用同一响应 ID 均留下争议，第二个 Turn 的预留保持 HELD。 |
| U06 父子线程 | 两个线程使用相同响应 ID，各有独立的 35 和 11。资金账为 46、一次 API 调用。 |
| U07 历史 915 | 子线程可归属 732，差额 183。父线程自身可归属 105。两线程均无重复调整。 |
| U08 跨进程到期 | 进程 1653277 留下原预留。真实的一秒宽限期届满后，进程 1653343 和 1653361 先后打开原库运行 Driver。原预留上仅有一次估算结算。 |
| 无可用 grant | 历史预留没有 token 额度，已见的 35 只支持响应下界。到期后保持 `UNRESOLVED/HELD`，无估算结算和重复的到期唤醒。 |

计量实现提交为 `bb58cc8`。另有 49 项既有计量、归属、中断和投递测试通过。归档运行时仓库 HEAD 为 `76d990c`，环境记录包含同时进行的其他 Closure 修改及其差异摘要。[运行环境](acceptance/closure/usage-history/usage-bb58cc8/environment.json)

统一入口的独立计量复测归档有 31 份原始文件，包括每个案例的 SQLite 快照、预算事件、Usage 观察和执行日志。归档 SHA-256 为 `a6cf279c2a30bbf1a16a94a111e6ce6a36317b3acaf7e2ec73c9c3fe9795e518`。[原始归档](acceptance/closure/usage-history/usage-bb58cc8/raw.tar.gz) · [包清单](acceptance/closure/usage-history/usage-bb58cc8/manifest.sha256) · [原始文件清单](acceptance/closure/usage-history/usage-bb58cc8/raw-files.sha256)

```sh
bin/gx-acceptance run phase2a --case U01 --case U02 --case U03 --case U04 --case U05 --case U06 --case U07 --case U08
bin/gx-acceptance verify usage-bb58cc8 --runs-dir acceptance/closure/usage-history
```
