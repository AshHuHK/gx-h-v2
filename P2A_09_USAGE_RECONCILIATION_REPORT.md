# P2A-09 Usage 对账

冻结提交 `fa8bba97c79a006ab950ea07ca82384fefe362a9` 通过 U01–U08、无可用额度对照和 B01–B12 共 21 项磁盘账本测试。三个官方 Codex 宿主冷、热连接探针共通过 29 个断言。独立审查者在四份冻结代码上运行同一组 13 个资金反例，最终全部通过。既有计量、重启、原生投递和回执归属测试 49 项全部通过。[统一入口复测](acceptance/closure/usage-history/usage-fa8bba9/summary.json) · [环境与复核命令](acceptance/closure/usage-history/funding-fa8bba9/receipt.json) · [49 项逐项结果](acceptance/closure/usage-history/funding-fa8bba9/legacy-49.log)

原始子线程累计 915 个 token，4 笔带响应 ID 的回执合计 732，余下 183 计入该线程的 `UNATTRIBUTED_DELTA`。父线程另有 3 笔各 35 的回执。两条线程共归属 837 个 token。回放时，父预留在宽限期后按 837 估算结算一次，预算还需计入 183 的未归属敞口，共占用 1,020。[原始宿主记录](acceptance/phase2a/regression/final/batch/extra-child/observation.json) · [冻结版原始 SQLite、事件与状态](acceptance/closure/usage-history/usage-fa8bba9/raw.tar.gz)

## 资金归属与独立审查

| 场景 | 已核对的产品行为 |
| --- | --- |
| 两个 Turn 同用或分用预算，B01、B02 | T1 估算 100；T2 只有线程累计 145，其预留在宽限期前保持 `HELD`。T1 的迟到回执确认 120，只有原资金行变为 120；跨资金运行中 B 的已付额仍为 0。线程另有 25 待归属。 |
| 宿主自行发起 Turn，B03 | 已付 20 的 Turn 后，宿主独立回执为 200，下一个 GX Turn 额度为 100。线程从 220 增到 225。GX Turn 自身已证下界为 0，到期估算 100。资金账为 120，宿主独立敞口为 200，合计 320。成本报告列出 220 已归属、5 线程差额和 95 的估算敞口。 |
| 关闭子预算，B04、B10、B11 | 父预算在关闭前后均保留已付 20 和未归属 50，敞口为 70。另一组关闭后到达的正、负更正分别使原子预算变为 140 或 60。兄弟预算自己的 8 保留，项目分别显示 148 或 68。 |
| 历史结算缺乏单笔金额，B05、B12 | 历史资金账已付 100，原 Turn 金额保留 `null`；当期确认 20 后已付 120。新宿主回执 50 后敞口为 170，下一条当前观察使其变为 175。仅收到新宿主原始回执 250 时，持久化的官方 Turn 开始边界证明其发生在历史结算之后，敞口为 370，再预留 100 后已承诺 470。历史边界之前的旧回执 25 遇到迟到的开始通知时仍保持原归属。 |
| 后续工作仍占用额度，B06、B07 | 原估算 100，迟到回执分别产生 +40 和 −40。原预算已付分别为 140 和 60；连同其他观察，敞口分别为 150 和 95。 |
| 同一资金已按累计对账，B08 | 两个到期估算先按同一可核对区间归集为 145；迟到的第一笔 120 使临时余额降至 25；第二笔确认为 25 后，两笔共计 145 均有明确归属，资金余额始终为 145。 |
| 同时遗留的两个 HELD Turn，B09 | 两个独立响应分别为 180 和 160，均缺少线程累计。已观察敞口和已承诺额均为 340，预留原额共 200。 |

只有单一资金范围、可核对区间及完整线程和子线程边界才能用于累计资金核对。已终止的新 Turn 在只有累计、缺少自身可用回执时先保持预留；宽限期到达后先保存原估算，再按符合条件的同一资金检查点调整。成本报告仍按每个 Turn 实际拥有的回执给出归属。历史原账金额缺失时，未来可确认的回执和新增累计仍纳入敞口，历史资金账继续保留原值。[计量与检查点](gx-h/gx_core/p0/metering.py) · [预算敞口](gx-h/gx_core/p0/meter_limits.py) · [历史开始边界迁移](gx-h/gx_core/p0/zzzzzz_p2a_legacy_boundary.sql)

独立审查的原始复现按相同输入保存。版本 `bb58cc8` 通过 4/13，`c7a9477` 通过 10/13，`c28f9b0` 通过 12/13，`fa8bba9` 通过 13/13。最后一个失败是具有独立开始边界、缺少最终累计的 250-token 新宿主回执。审查脚本和每一版的完整读数、断言与原始提交号均在归档中。[审查输入](acceptance/closure/usage-history/funding-fa8bba9/independent/gx-closure-cost-review-fixtures.py) · [逐项断言](acceptance/closure/usage-history/funding-fa8bba9/independent/gx-closure-cost-independent-assert.py) · [首轮九项失败](acceptance/closure/usage-history/funding-fa8bba9/independent/bb58cc8/assertions.log) · [最终原始读数](acceptance/closure/usage-history/funding-fa8bba9/independent/fa8bba9/stdout.jsonl)

## 官方宿主的冷热回归

三个探针使用锁定的 `codex-cli 0.169.0`、独立的 Linux 网络命名空间和真实 Mihomo 的两个环回代理端口，命名空间无外部路由。提供方为脚本化环回服务。每次重放原 Turn ID 后，资金账与请求计数一致。核对结果来自官方宿主的实际通知与 GX 写入的 SQLite。[命令](acceptance/closure/usage-history/funding-fa8bba9/official/commands.json) · [网络及代理收据](acceptance/closure/usage-history/funding-fa8bba9/official/cold-network.json)

| 探针 | 宽限期前 | 成本与资金结果 |
| --- | --- | --- |
| 热连接 | 第二个 Turn 自身收到完整回执 35，立即确认。 | 第一个估算 100 仍可追查。共同资金更正 −65 后实付 70、2 次调用。[宿主记录](acceptance/closure/usage-history/funding-fa8bba9/official/warm.json) |
| 冷重连 | 第二个 Turn 仅有累计，保留 HELD。 | 到期后两笔原额 100 均保留，资金更正 −130 后实付 70、2 次调用。[宿主记录](acceptance/closure/usage-history/funding-fa8bba9/official/cold.json) |
| 冷重连并重复累计 | 第二个 Turn 连续收到 70、70 并保持 HELD。第三个 Turn 仅见累计 105，同样等待自身宽限期。 | 三笔原估算 100 的资金更正共 −195，实付 105、3 次调用。[宿主记录](acceptance/closure/usage-history/funding-fa8bba9/official/cold-repeated.json) |

原 V2.1 冷重连用例把新 Turn 在宽限期前直接按 35 结算。旧探针两次留下了缺少该 Turn 原始回执时立即结算的记录，实付总额分别为 70 和 105。当前回归同时核对原资金总额、调用计数、重复投递和新版宽限规则。独立的终止协议复测还保存了两次自然缺失累计回执的观测，两次都在宽限后按 800 各估算一次，此前各有 35 的已知回执。[原冷重连记录](acceptance/phase2a/regression/final-v2/batch/suite/native/native-meter-cold.json) · [原重复累计记录](acceptance/phase2a/regression/final-v2/batch/suite/native/native-meter-repeated.json) · [当前回归断言](gx-h/tests/test_v21_metering.py) · [自然缺失记录](acceptance/closure/terminal-cancellation/evidence.json)

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

下列矩阵采用 `SCRIPTED` 输入、产品 RootDelivery 和真实磁盘 SQLite。U07 回放的是历史 `OFFICIAL_HOST_LOOPBACK` 所存的原响应 ID、线程归属与金额，保留原来的冷恢复超时结果。U08 在独立进程和真实时钟上执行。报告中的 token 均无货币单价。[测试用例](acceptance/closure/test_usage.py) · [命令、环境与退出码](acceptance/closure/usage-history/usage-fa8bba9/commands.json)

| 场景 | 观测结果 |
| --- | --- |
| U01 完整回执 | 两份响应分别为 17 和 21。重放完成通知，预算仅结算 38 和一次 API 调用。 |
| U02 中断后等待 | 已收到的 35 在响应账可见。宽限期前运行产品 Driver 后，原预留继续保持 HELD。 |
| U03 到期 | Driver 两次维护只留下一个 100 的估算结算事件，其中已有 35 的凭据，估算敞口为 65。 |
| U04 迟到回执 | 原事件 4 估算 100，事件 5 引用 4 并记录确认为 70，资金差额为 −30。第二次估算与回执均为 100，事件 9 引用 8 并记录零调整。 |
| U05 重复、乱序与争议 | 一笔迟到回执先于匹配快照，两个独立连接并发重放快照后只增加 30。冲突金额和跨 Turn 重用同一响应 ID 均留下争议，第二个 Turn 的预留保持 HELD。 |
| U06 父子线程 | 两个线程使用相同响应 ID，各有独立的 35 和 11。资金账为 46、一次 API 调用。 |
| U07 历史 915 | 子线程可归属 732，差额 183。父线程自身可归属 105。两线程均无重复调整。 |
| U08 跨进程到期 | 进程 2383438 留下原预留。真实的一秒宽限期届满后，进程 2384579 和 2384926 先后打开原库运行 Driver。原预留上仅有一次估算结算。 |
| 无可用 grant | 历史预留没有 token 额度，已见的 35 只支持响应下界。到期后保持 `UNRESOLVED/HELD`，无估算结算和重复的到期唤醒。 |

冻结版统一入口保存 67 份原始文件，每项均有 SQLite 快照、预算事件和状态数据。复核包 SHA-256 为 `d076c0af862d9186b59bc7e64f71b7a49e040b7ec870400d581f28a5071cee57`。另附官方宿主、独立审查和既有 49 项测试的原始回执与校验清单。首版计量提交 `bb58cc8` 的 31 份原始记录仍可复核。[冻结版包清单](acceptance/closure/usage-history/usage-fa8bba9/manifest.sha256) · [67 份文件校验和](acceptance/closure/usage-history/usage-fa8bba9/raw-files.sha256) · [补充回执清单](acceptance/closure/usage-history/funding-fa8bba9/manifest.sha256) · [首版原始包](acceptance/closure/usage-history/usage-bb58cc8/raw.tar.gz)

```sh
bin/gx-acceptance run phase2a --case U01 --case U02 --case U03 --case U04 --case U05 --case U06 --case U07 --case U08
bin/gx-acceptance verify usage-fa8bba9 --runs-dir acceptance/closure/usage-history
(cd acceptance/closure/usage-history/funding-fa8bba9 && sha256sum -c manifest.sha256)
```
