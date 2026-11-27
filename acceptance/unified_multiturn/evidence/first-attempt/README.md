# 首次真实双路线研究：B 的报告在 GX 发布时中断

Run `gxr_01m8d44vvx9cda854x` 使用部署候选 `e2a102a` 和官方 Codex OAuth。**两名模块研究者确实同时工作，PI 的定向意见在二者在途时入账，两条路线的 CPU 测量均成功。B 的科研作者报告未获接受，原 B 路线未签收。** 父题随后在事件 214 封存。[原始时段、产物及事件](receipt.json)

| UTC，2026-11-26 | 观察 |
| --- | --- |
| 23:49:06.181–23:49:49.318 | A 的官方模型回合处理 9VGW。 |
| 23:49:06.258–23:49:52.575 | B 的另一官方线程处理 1VYM。两个模型回合有 **43.060 秒**的交集。 |
| 23:49:07.775 | GX 以事件 54 收到针对 A 的 PI 反馈。入账时两个回合均在进行。 |
| 23:52:46.866 | A 的模块作者发布 COMPLETE 报告，事件 151。事件 152 记录其采纳 PI 意见，保留链对差异判断，适用范围收窄到该份坐标。 |
| 23:54:56.820 | B 的模块任务终止，事件 190；GX 发布 PARTIAL，事件 191，随后在 192 唤醒 Lead。B 的已成功 Job 和表格仍可引用。 |

两份 `interface-table.json` 都对沉积坐标的完整 A–B、B–C、C–A 界面给出了接触残基对数。A 的 9VGW 为 **24、27、28**；B 的 1VYM 为 **33、30、36**。同一沉积的三对链共享亚基，这些数值描述各自坐标中的界面。B 的 A–B 具名输出另给出埋藏面积 **1490.49 Å²**。[具名 Job、产物及内容摘要](receipt.json)

B 的两次真实报告提交都被原校验器以 `SCHEMA_INVALID` 拒绝。第一次提交包含来自已读 mmCIF 的实体编号、测量范围中的模型与链对选择，以及已发布表中的 JSON 路径；原校验首先在 finding 的“实体 1”处拦下提交。第二次提交在后续步骤里引用已经送达的读数行，行键 `#7f2a8a8b83c8` 开头的 `7` 被读成了未登记的科学数字。此后 B 到达其模块局部配额，GX 在 PARTIAL 回执中记录 `module tokens allowance exhausted`。原报告作者字段为 `GX`。这一回执保留了科学产物，未提供 B 的模型作者成功提交报告的证据。

父题的 Lead 后来按事先选取的 10% 容差，将每份坐标的极差除以其链对计数中位数。从原表复算，9VGW 为 4/27（14.815%），1VYM 为 6/33（18.182%）。两者均超出容差。Lead 在事件 214 封存父题，建议在这两份沉积的摘要中逐对列数，并说明覆盖、模型与接触定义。这一结题决定与 B 路线的报告作者资格分别记录。

## 原请求的离线回放

在修复提交 `9b36858` 上，对上述两次真实报告请求做同一输入的只读回放。两次请求均可渲染，分别从原具名输出绑定 `33 pairs` 和 `1490.49 Å²`。回放未向原 Run 提交修订，原 PARTIAL 保留。补充的两个回归用例验证已送达行键可引用，并覆盖伪造、错来源、跨 Run、未送达行和正文未登记数值。[受控回放输入标识及结果](receipt.json) · [可重复的正反对照](../../../v623/test_close_metrics.py)

从产品根目录复验两个公开的接口案例。原生宿主的私人会话记录保留在部署端。

```bash
GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a \
  .venv/bin/python -B -m unittest -v \
  acceptance.v623.test_close_metrics.EndpointClose.test_report_accepts_a_delivered_source_row_in_prose_and_keeps_named_metric \
  acceptance.v623.test_close_metrics.EndpointClose.test_report_rejects_unreceived_forged_misbound_rows_and_unregistered_number
```

同一会话的 3TBL 后继研究在事件 327 封存。修复版读取并复核原始两题，14 项联合检查中 13 项通过，唯一未通过的是要求 B 本人完成报告的检查。父题再次导出没有新增计算或模型回合。[两题关闭决定、完整科学理由与逐项核验](closure.json)
