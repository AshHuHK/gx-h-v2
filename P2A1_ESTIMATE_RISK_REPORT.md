# P2A.1 原生 Turn 估算与未观察用量

已观察到 1,000 tokens、预留额度为 800 的 Turn，在宽限期后按 1,000 入账。此时状态显示 `ESTIMATE_POSSIBLY_UNDERSTATED`，供应方完整用量和未观察用量上限均为 `null`。本次父子线程用例随后收到完整的 1,800-token 凭据，同一预算追加 800。再次打开数据库并重放回执后，仍只结算一次 API 调用。[父子线程状态](acceptance/hardening/estimate-risk-800fe58/parent_with_child/status.json) · [原始和更正事件](acceptance/hardening/estimate-risk-800fe58/parent_with_child/events.json)

## 读取操作状态

```sh
bin/gx-p0 status --state DIR --run-id RUN
bin/gx-p0 operator usage --state DIR --run-id RUN
```

`status` 的 `metering.turns[].cost_assessment` 和 `operator usage` 的 `result.metering.turns[].cost_assessment` 提供相同的逐 Turn 读数。查找仍可能低估的预留可读 `metering.cost_report.possibly_understated_estimate_refs`。每个引用可与逐 Turn 记录的 `reservation_ref` 对应。[状态输出](acceptance/hardening/estimate-risk-800fe58/parent/status.json) · [操作员用量输出](acceptance/hardening/estimate-risk-800fe58/parent/usage.json)

| 字段 | 预算与证据含义 |
| --- | --- |
| `estimate_policy` | `ESTIMATED_CONSERVATIVE` 表示发生过宽限期估算。原始规则为 `max(turn_grant_tokens, observed_lower_bound_at_estimate_tokens)`。历史估算与其政策在确认后仍可查询。 |
| `risk` | `ESTIMATE_POSSIBLY_UNDERSTATED` 表示已经估算、完整用量仍待确认。完整且一致的凭据齐备时为 `PROVIDER_CONFIRMED`。尚未估算且缺少完整凭据的记录为 `UNRESOLVED`。 |
| `turn_grant_tokens` | 原 Turn 的 GX 预留额度。原始投递记录缺少额度时读取同一预留，两处均缺失时为 `null`。 |
| `scoped_observed_lower_bound_tokens` | 当前能归属该 Turn 及其已核实子 Turn 的用量下界。各独立响应按线程去重，同一 Turn 的恢复记录与实时观测取已有的较完整值。没有可归属凭据时为 `null`，明确的零用量凭据可显示 0。 |
| `observed_lower_bound_at_estimate_tokens`、`original_estimate_tokens` | 估算落账时冻结的下界与金额。估算规则沿用原计量所有者的有效下界。 |
| `provider_confirmed_total_tokens` | 该预留对应父子 Turn 的完整确认金额。部分已知响应可单独核实，完整总额仍为 `null`。 |
| `unobserved_tokens_upper_bound`、`evidence_gaps` | 上游在途请求及缺失的结束用量没有可证明的有限 token 上限，显示 `null`。原因可包含父或子 Turn 尚无结束记录、结束用量不完整或凭据冲突。完整确认时为 0，原因列表为空。 |

预算的 `SETTLED` 记录可以是估算。计量以原始 `budget.settled` 事件和有符号的后续调整保留其历史。线程累计余量单独列入 `UNATTRIBUTED_DELTA`。资金报告列出已观察或已入账的金额，未来供应方响应的上限保持未知。[报告实现](gx-h/gx_core/p0/metering_report.py) · [估算与调整](gx-h/gx_core/p0/metering.py)

## 验收观测

在 `800fe589d7bdaf99246b02b075a4b755730ef87c` 的独立源码副本中，脚本向真实 GX 投递和 SQLite 预算所有者送入构造的宿主通知。每个阶段均用新的 CLI 进程读取状态与用量。两例都在宽限期第 29 秒保持 `HELD`，第 31 秒执行估算，再从新连接接收迟到回执，最后重新打开数据库并重放。证据类别为 `SCRIPTED`。[执行清单与 SHA-256](acceptance/hardening/estimate-risk-800fe58/provenance.json) · [验收脚本](acceptance/hardening/estimate_risk.py)

| 用例 | 预留额度 | 估算前已观察下界 | 原估算 | 供应方确认 | 更正 | 重开后 API 次数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 父线程 | 1,000 | 800 | 1,000 | 1,800 | +800 | 1 |
| 父线程与子线程 | 800 | 1,000（父 600、子 400） | 1,000 | 1,800（父 600、子 1,200） | +800 | 1 |

在两个独立数据库中，事件 8 保存原估算 1,000，事件 11 引用事件 8 并记录确认差额 +800，事件 12 将差额计入原预算。既有的 21 个用量回归也全部通过。[父线程事件](acceptance/hardening/estimate-risk-800fe58/parent/events.json) · [父子线程事件](acceptance/hardening/estimate-risk-800fe58/parent_with_child/events.json) · [命令结果](acceptance/hardening/estimate-risk-800fe58/checks.log)

```sh
python acceptance/hardening/estimate_risk.py --output /tmp/gx-estimate-risk-run
```

每次使用新的输出目录。脚本将两种 CLI 的逐阶段原始结果、预算事件和 `result.json` 写入该目录，断言失败时返回非零状态码。[本次逐例结果](acceptance/hardening/estimate-risk-800fe58/result.json)
