# 中断回执丢失时的只读恢复

原公开套件 `1f8fc82` 完成了 201 组。第 202 组 `GX_ABORT_0202` 中，GX 已提交 REJECTED，第一次查询返回原 Turn `inProgress`，同一 `gx_submit_product` 已显示 `failed`。当时的夹具又发送一次 interrupt，并等待被故障注入持续丢弃的 `turn/completed`。两个 interrupt ACK 和一个终止通知都被截获，最终报出 App Server pipe IO timed out。在同一源的独立 204 组中，这个场景第一次只读查询已经返回 `interrupted`，因而通过。[同源运行对照](comparison.json) [原始失败及前 201 组](original-1f8fc82.partial.json.gz)

失败运行的原线程是 `01a20ebc-9dac-7b43-9a3a-89e26d89c9b7`，原 Turn 是 `01a20ebc-9dd6-7821-bede-dfbc03e2239f`，调用是 `call-probe-response-202`。实际函数输出记录了精确取消原因。当时未保存最终只读历史或结算回执，该次完整场景仍为 FAIL。[原 trial](original-1f8fc82-trial.json) [原调用栈](original-1f8fc82.log)

## 修正及冻结回放

`3e52d11` 的产品树与 `1f8fc82` 相同。夹具在原有 8 秒恢复窗口内用 `thread/turns/list` 读取同线程、同 Turn 的实际历史，直到出现 `interrupted`。只有出站 interrupt 的原始写入被注入故障丢弃、原 Turn 仍活跃并且调用尚未终结的专门场景，才重发同一中断。精确工具取消原因、唯一提交及结算、防止普通 ToolResponse 和后继模型调用的断言继续执行。

| 官方场景 | GX 请求中断次数 | 到达原生主机次数 | 实际历史与结果 |
|---|---|---|---|
| 原始出站写入丢失 | 2 | 1 | 原 Turn 从 inProgress 到 interrupted，ACCEPTED |
| 原始两个入站回执丢失 | 1 | 1 | 历史已 interrupted，REJECTED |
| 原始两个入站回执延迟 | 1 | 1 | 历史已 interrupted，真实回执回放，ACCEPTED |
| 强制第一次查询时仍在进行 | 1 | 1 | 发出的同一中断确在首次真实 inProgress 后才交付。两个入站回执均丢失，下一次公开历史为 interrupted，REJECTED |

每组均只有一个原模型请求、一次 35 tokens 的原预约结算，普通 ToolResponse 和提交后模型请求均为 0。补充反例一直扣留原始出站中断，累计 51 次真实只读观察均为 inProgress，按原超时上限失败，未产生取消成功判据。[分组结果与身份](results.json) [补充负例](negative.json) [四组原始命令](commands.json) [同一 loopback 与 Clash 回执](network.json)

在本目录执行 `sha256sum -c manifest.sha256` 可核验文件。用例入口是 `acceptance/v31/terminal_protocol_probe.py --only lost_receipts_in_progress`，原 204 组的组成未变。
